# -*- coding: utf-8 -*-
"""clangd 中文诊断「纯翻译层」—— 换机器后的一键接入。

本包不含 Python 运行时，也不含 clangd 本体；两者都用新机器上已装好的。
用法：双击「安装.cmd」，或 python install.py
可选参数：
  --test          只探测环境并跑验证，不写启动器、不改 VS Code 配置、不装扩展
  --std=c++14     指定 C++ 标准（默认 c++23）
环境变量（自动探测不到时手动指定）：
  CLANGD_PY       python.exe 的完整路径
  CLANGD_EXE      clangd.exe 的完整路径
  CLANGD_GXX      g++.exe 的完整路径
"""
import glob
import json
import os
import pathlib
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

EXT_ID = 'llvm-vs-code-extensions.vscode-clangd'
PROXY = os.path.join(BASE, 'clangd_zh_proxy.py')
LAUNCHER = os.path.join(BASE, 'clangd-zh.cmd')
CPP_STD = '-std=c++23'
for _arg in sys.argv[1:]:
    if _arg.startswith('--std='):
        CPP_STD = '-std=' + _arg.split('=', 1)[1]
TEST_ONLY = '--test' in sys.argv
PY = ''


def say(msg=''):
    print(msg, flush=True)


def pos(p):
    return os.path.abspath(p).replace('\\', '/')


def run(cmd, **kw):
    kw.setdefault('stdout', subprocess.PIPE)
    kw.setdefault('stderr', subprocess.PIPE)
    kw.setdefault('text', True)
    kw.setdefault('encoding', 'utf-8')
    kw.setdefault('errors', 'replace')
    return subprocess.run(cmd, **kw)


# ---------------------------------------------------------------- 探测依赖
def find_python():
    env = os.environ.get('CLANGD_PY')
    if env and os.path.isfile(env) and 'WindowsApps' not in env:
        return os.path.abspath(env)
    # py launcher 是官方安装器自带的，最可靠，也不会命中商店占位程序
    try:
        r = run(['py', '-3', '-c', 'import sys; print(sys.executable)'])
        if r.returncode == 0 and r.stdout.strip():
            p = r.stdout.strip().splitlines()[-1].strip()
            if p and os.path.isfile(p) and 'WindowsApps' not in p:
                return p
    except Exception:
        pass
    for name in ('python', 'python3'):
        p = shutil.which(name)
        if p and os.path.isfile(p) and 'WindowsApps' not in p:
            return os.path.abspath(p)
    la = os.environ.get('LOCALAPPDATA', '')
    pats = (glob.glob(os.path.join(la, 'Programs', 'Python', '*', 'python.exe'))
            if la else [])
    pats += glob.glob('C:/Python3*/python.exe')
    for p in sorted(pats, reverse=True):
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None


def find_clangd():
    env = os.environ.get('CLANGD_EXE')
    if env and os.path.isfile(env):
        return os.path.abspath(env)
    p = shutil.which('clangd')
    if p and os.path.isfile(p):
        return os.path.abspath(p)
    cands = ['C:/Program Files/LLVM/bin/clangd.exe',
             'C:/Program Files (x86)/LLVM/bin/clangd.exe']
    la = os.environ.get('LOCALAPPDATA', '')
    if la:
        cands.append(os.path.join(la, 'Programs', 'LLVM', 'bin', 'clangd.exe'))
    for d in 'CDEFGHIJ':
        for rel in ('clangd/bin/clangd.exe', 'LLVM/bin/clangd.exe',
                    'tools/clangd/bin/clangd.exe', 'dev/clangd/bin/clangd.exe'):
            cands.append(os.path.join(d + ':/', rel))
    for p in cands:
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None


def find_gpp():
    env = os.environ.get('CLANGD_GXX')
    if env and os.path.isfile(env):
        return os.path.abspath(env)
    p = shutil.which('g++')
    if p and os.path.isfile(p):
        return os.path.abspath(p)
    rels = [
        'gcc/bin/g++.exe', 'TDM-GCC-64/bin/g++.exe', 'mingw64/bin/g++.exe',
        'msys64/mingw64/bin/g++.exe', 'MinGW/bin/g++.exe',
        'mingw-w64/bin/g++.exe', 'dev/gcc/bin/g++.exe', 'tools/gcc/bin/g++.exe',
    ]
    for d in 'CDEFGHIJ':
        for rel in rels:
            cand = os.path.join(d + ':/', rel)
            if os.path.isfile(cand):
                return os.path.abspath(cand)
    return None


def compiler_flags(gpp):
    """从 g++ 自身问出 target 和标准库头文件路径，不写死任何版本号。"""
    target = ''
    if gpp:
        r = run([gpp, '-dumpmachine'])
        if r.returncode == 0 and r.stdout.strip():
            target = r.stdout.strip().splitlines()[0].strip()
    if not target:
        target = 'x86_64-w64-mingw32'

    paths = []
    if gpp:
        r = run([gpp, '-E', '-x', 'c++', '-v', '-'], input='')
        text = (r.stderr or '') + '\n' + (r.stdout or '')
        m = re.search(
            r'#include <\.\.\.> search starts here:(.*?)End of search list\.',
            text, re.S)
        if m:
            for line in m.group(1).splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                norm = pos(line)
                if os.path.isdir(norm) and norm not in paths:
                    paths.append(norm)

    flags = ['--target=' + target, CPP_STD]
    for p in paths:
        flags.extend(['-isystem', p])
    return flags, paths, target


# ---------------------------------------------------------------- 生成启动器
def write_launcher(py, clangd):
    """把探测到的 Python / clangd 绝对路径写进启动器，运行时不再依赖 PATH。"""
    content = (
        '@echo off\r\n'
        'setlocal\r\n'
        'set "HERE=%~dp0"\r\n'
        'set "CLANGD_EXE=' + clangd + '"\r\n'
        '"' + py + '" "%HERE%clangd_zh_proxy.py" %*\r\n'
        'exit /b %ERRORLEVEL%\r\n'
    )
    try:
        data = content.encode('mbcs')  # cmd.exe 按系统 ANSI 代码页读取
    except Exception:
        data = content.encode('utf-8')
    with open(LAUNCHER, 'wb') as f:
        f.write(data)


# ---------------------------------------------------------------- VS Code 配置
def settings_paths():
    appdata = os.environ.get('APPDATA') or ''
    cands = [
        os.path.join(appdata, 'Code', 'User', 'settings.json'),
        os.path.join(appdata, 'Code - Insiders', 'User', 'settings.json'),
        os.path.join(appdata, 'VSCodium', 'User', 'settings.json'),
    ]
    exists = [p for p in cands if os.path.isfile(p)]
    if exists:
        return exists
    os.makedirs(os.path.dirname(cands[0]), exist_ok=True)
    return [cands[0]]


def strip_jsonc(s):
    """保守地去掉 // 与 /* */ 注释和尾逗号，保留字符串里的内容。"""
    out = []
    i, n = 0, len(s)
    in_str = False
    quote = ''
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(s[i + 1])
                i += 2
                continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in ('"', "'"):
            in_str, quote = True, c
            out.append(c)
            i += 1
            continue
        if c == '/' and i + 1 < n and s[i + 1] == '/':
            j = s.find('\n', i)
            if j < 0:
                break
            i = j
            continue
        if c == '/' and i + 1 < n and s[i + 1] == '*':
            j = s.find('*/', i + 2)
            i = (j + 2) if j >= 0 else n
            continue
        out.append(c)
        i += 1
    return re.sub(r',\s*([}\]])', r'\1', ''.join(out))


def load_settings(p):
    if not os.path.isfile(p) or os.path.getsize(p) == 0:
        return {}
    raw = open(p, encoding='utf-8-sig').read()
    try:
        return json.loads(raw)
    except Exception:
        return json.loads(strip_jsonc(raw))


def apply_settings(flags, gpp):
    arguments = ['--clang-tidy=false', '--header-insertion=never']
    if gpp:
        arguments.insert(0, '--query-driver=' + pos(os.path.dirname(gpp)) + '/*')
    patch = {
        'clangd.path': pos(LAUNCHER),
        'clangd.useScriptAsExecutable': True,
        'clangd.checkUpdates': False,
        'clangd.detectExtensionConflicts': False,
        'clangd.arguments': arguments,
        'clangd.fallbackFlags': flags,
        'C_Cpp.intelliSenseEngine': 'disabled',
    }
    for p in settings_paths():
        cfg = load_settings(p)
        if os.path.isfile(p):
            bak = p + '.bak-zh-' + time.strftime('%Y%m%d-%H%M%S')
            shutil.copy2(p, bak)
        else:
            bak = '（原本不存在，无需备份）'
        cfg.update(patch)
        with open(p, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
        say('  已写入 ' + p)
        say('  备份   ' + bak)
    return patch


def find_code():
    c = shutil.which('code') or shutil.which('code.cmd')
    if c:
        return c
    roots = [os.environ.get('LOCALAPPDATA', ''),
             'C:/Program Files', 'C:/Program Files (x86)']
    for root in roots:
        for rel in ['Programs/Microsoft VS Code/bin/code.cmd',
                    'Microsoft VS Code/bin/code.cmd']:
            p = os.path.join(root, rel)
            if os.path.isfile(p):
                return p
    return None


def install_extension():
    code = find_code()
    if not code:
        say('  ! 未找到 VS Code 命令行，请手动安装扩展：' + EXT_ID)
        return
    try:
        rc = subprocess.call('"%s" --install-extension %s --force'
                             % (code, EXT_ID), shell=True)
        say('  扩展安装返回码：%s（0 或已安装均属正常）' % rc)
    except Exception as e:
        say('  ! 扩展安装失败，请手动执行 code --install-extension %s：%r'
            % (EXT_ID, e))


# ---------------------------------------------------------------- 验证
def read_frame(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b'\r\n', b'\n'):
            break
        if b':' in line:
            k, v = line.decode('ascii', 'replace').split(':', 1)
            headers[k.strip().lower()] = v.strip()
    length = int(headers.get('content-length', '0') or '0')
    if length <= 0:
        return b''
    body = stream.read(length)
    return body if len(body) == length else None


def lsp_smoke(flags, timeout=25):
    """真实起一次 LSP，确认诊断确实被翻译成中文。"""
    tmp = tempfile.mkdtemp(prefix='clangd_zh_smoke_')
    proc = None
    try:
        src = os.path.join(tmp, 'smoke.cpp')
        text = 'int main(){ int x = "abc"; return undeclared_thing; }\n'
        with open(src, 'w', encoding='utf-8') as f:
            f.write(text)
        uri = pathlib.Path(src).as_uri()

        # 冒烟测试只验证翻译能力,强制开启,不受用户 tasks.json 开关影响
        smoke_env = dict(os.environ)
        smoke_env['CLANGD_ZH'] = '1'
        proc = subprocess.Popen([PY, PROXY, '--log=error'],
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                env=smoke_env)
        q = queue.Queue()

        def reader():
            while True:
                body = read_frame(proc.stdout)
                if body is None:
                    q.put(None)
                    return
                try:
                    q.put(json.loads(body.decode('utf-8')))
                except Exception:
                    pass

        threading.Thread(target=reader, daemon=True).start()

        def send(obj):
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            proc.stdin.write(b'Content-Length: %d\r\n\r\n' % len(body) + body)
            proc.stdin.flush()

        def wait(pred, secs):
            end = time.time() + secs
            while True:
                left = end - time.time()
                if left <= 0:
                    return None
                try:
                    m = q.get(timeout=left)
                except queue.Empty:
                    return None
                if m is None:
                    return None
                if pred(m):
                    return m

        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
              'params': {'processId': os.getpid(),
                         'rootUri': pathlib.Path(tmp).as_uri(),
                         'capabilities': {},
                         'initializationOptions': {'fallbackFlags': flags,
                                                   'clangdFileStatus': True}}})
        if wait(lambda m: m.get('id') == 1, 15) is None:
            say('  ! LSP initialize 超时')
            return False
        send({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})
        send({'jsonrpc': '2.0', 'method': 'textDocument/didOpen',
              'params': {'textDocument': {'uri': uri, 'languageId': 'cpp',
                                         'version': 1, 'text': text}}})
        diag = wait(
            lambda m: m.get('method') == 'textDocument/publishDiagnostics'
            and bool((m.get('params') or {}).get('diagnostics')), timeout)
        if diag is None:
            say('  ! 未收到诊断')
            return False
        msgs = [d.get('message', '') for d in diag['params']['diagnostics']]
        zh = [m for m in msgs if re.search(r'[\u4e00-\u9fff]', m)]
        say('  收到诊断 %d 条，其中中文 %d 条' % (len(msgs), len(zh)))
        for m in msgs[:3]:
            say('    - ' + m[:110])
        return len(zh) > 0
    except Exception as e:
        say('  ! 冒烟测试异常：%r' % e)
        return False
    finally:
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def verify_switch():
    """验证 tasks.json 的 clang-zh 开关:只在临时目录造文件,不碰用户配置。"""
    tmp = tempfile.mkdtemp(prefix='clangd_zh_switch_')
    env = dict(os.environ)
    env['CLANGD_ZH_TASKS_DIR'] = tmp
    env.pop('CLANGD_ZH', None)
    code = ("import sys; sys.path.insert(0, %r); import clangd_zh_proxy as p; "
            "print(1 if p.translation_enabled() else 0)" % BASE)
    results = []
    try:
        for content, expect, label in (
            ('{"version":"2.0.0","clang-zh":null,"tasks":[]}', '0', 'null'),
            ('{"version":"2.0.0","clang-zh":"on","tasks":[]}', '1', 'on'),
        ):
            with open(os.path.join(tmp, 'tasks.json'), 'w', encoding='utf-8') as f:
                f.write(content)
            r = run([PY, '-c', code], env=env)
            results.append(((r.stdout or '').strip() == expect, label))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok = all(a for a, b in results)
    say('  tasks.json 开关 : ' + ('OK  (null 关闭 / on 开启)' if ok
                                 else 'FAIL  ' + str(results)))
    return ok


def verify(flags):
    ok = True
    if not PY or not os.path.isfile(PY):
        say('  ! 缺少 Python：' + str(PY))
        return False
    r = run([PY, PROXY, '--version'])
    good = 'clangd version' in (r.stdout or '')
    say('  clangd --version : ' + ('OK  ' + (r.stdout or '').strip().splitlines()[0]
                                   if good else 'FAIL'))
    ok &= good

    probe = ("import sys; sys.path.insert(0, %r); import clangd_zh_proxy as p; "
             "print(len(p.RULES)+len(p.CODE_RULES)+len(p.CODE_EXACT)+len(p.WRAPPERS)"
             "+len(getattr(p,'NOTE_RULES',[])))"
             % BASE)
    r = run([PY, '-c', probe])
    n = (r.stdout or '').strip()
    say('  翻译规则总数   : ' + (n + ' 条' if n.isdigit() else 'FAIL'))
    ok &= n.isdigit() and int(n) > 0

    ok &= verify_switch()

    good = lsp_smoke(flags)
    say('  中文诊断端到端 : ' + ('OK' if good else 'FAIL'))
    ok &= good
    return bool(ok)


# ---------------------------------------------------------------- 主流程
def main():
    global PY
    say('=' * 60)
    say(' clangd 中文诊断（纯翻译层）—— 新机一键接入')
    say('=' * 60)
    say('翻译层目录：' + BASE)
    say('运行模式：' + ('仅测试（不写任何配置）' if TEST_ONLY else '正式安装'))
    say()

    say('[1/4] 探测本机已有的 Python / clangd / g++')
    PY = find_python()
    if not PY:
        say('  ! 没找到 Python 3。')
        say('    请先安装 Python 3.8+（安装时勾选 Add python.exe to PATH），')
        say('    或设置环境变量 CLANGD_PY 指向 python.exe 后重跑本脚本。')
        return 1
    say('  Python : ' + PY)

    clangd = find_clangd()
    if not clangd:
        say('  ! 没找到 clangd.exe。本包不含 clangd，请先在新机装好 clangd，')
        say('    或设置环境变量 CLANGD_EXE 指向 clangd.exe 后重跑本脚本。')
        return 1
    say('  clangd : ' + clangd)
    os.environ['CLANGD_EXE'] = clangd  # 传给代理及所有子进程

    gpp = find_gpp()
    if gpp:
        say('  g++    : ' + gpp)
    else:
        say('  ! 没找到 g++，标准库头文件会报找不到；装好后重跑本脚本')
        say('    （或设置环境变量 CLANGD_GXX 指向 g++.exe）。')
    flags, paths, target = compiler_flags(gpp)
    say('  target : ' + target + ' / ' + CPP_STD)
    say('  标准库 : %d 条路径（自动读取，不含写死的版本号）' % len(paths))
    say()

    if not TEST_ONLY:
        say('[2/4] 生成启动器与 compile_flags.txt')
        write_launcher(PY, clangd)
        say('  启动器 ' + LAUNCHER + '（写入了上面两个绝对路径）')
        with open(os.path.join(BASE, 'compile_flags.txt'), 'w',
                  encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(flags) + '\n')
        say('  已写入 ' + pos(os.path.join(BASE, 'compile_flags.txt')))
        say()
        say('[3/4] 配置 VS Code 并安装 clangd 扩展')
        apply_settings(flags, gpp)
        install_extension()
        say()
    else:
        say('[2/4] [3/4] 测试模式，跳过')
        say()

    say('[4/4] 验证')
    ok = verify(flags)
    say()
    say('=' * 60)
    if ok:
        say(' 验证通过：翻译层已接上本机的 clangd。')
    else:
        say(' 验证有失败项，请看上面的 FAIL / ! 提示。')
    if not TEST_ONLY and ok:
        say(' 最后一步：VS Code 里 Ctrl+Shift+P -> Reload Window')
    say('=' * 60)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
