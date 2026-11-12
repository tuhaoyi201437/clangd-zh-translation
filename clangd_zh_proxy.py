# -*- coding: utf-8 -*-
"""clangd 诊断中文化代理 —— 本地、离线、零联网。

链路：VS Code <-> 本代理 <-> clangd.exe(优先本包 bin/，可用 CLANGD_EXE 覆盖)
只改写 server -> client 的 textDocument/publishDiagnostics 里每条诊断的 message，
其余 LSP 消息按帧原样双向转发；Content-Length 按 UTF-8 字节数重算。
任何异常都降级为透传英文原文，绝不影响 clangd 本身工作。

翻译范围：
  * 诊断主句(原有能力)
  * 多行 message 里的 note 行、候选提示、note:/error:/warning: 位置标签
  * relatedInformation 里的补充说明
  逐行翻译，未命中的行保留英文原文，绝不吞掉信息。

翻译优先级：
  1. CODE_EXACT   按诊断码 + 精确正则(可提取动态内容)
  2. CODE_RULES   按诊断码 + 中文模板，{1} {2} 自动填英文里的 '单引号内容'
  3. NOTE_RULES   note / 候选 / 声明位置专用({N} 表示递归翻译子句)
  4. RULES        按英文整句正则(会先剥离 "(fix available)" 等尾部标注)
  5. WRAPPERS     "In template: ..." 之类的前缀包装，递归翻译内层

开关：
  KEEP_ORIGINAL = True            中文后附英文原文
  环境变量 CLANGD_ZH_DEBUG=1      写 proxy.log，记录命中/未命中，方便补词典
词典：zh_codes.py(按诊断码)、zh_diagnostics.py(按文本)，均可直接编辑
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)  # 嵌入式 Python 里也能 import 同目录词典

# 优先用本包自带的 clangd；也允许 CLANGD_EXE 指向机器上已有的 clangd
_env_clangd = os.environ.get('CLANGD_EXE')
CLANGD = next(
    (p for p in (_env_clangd,
                 os.path.join(BASE, 'bin', 'clangd.exe'),
                 shutil.which('clangd'))
     if p and os.path.isfile(p)),
    os.path.join(BASE, 'bin', 'clangd.exe'))
KEEP_ORIGINAL = False
# 设环境变量 CLANGD_ZH_NOTES=0 可回到旧行为：只翻主句，note 保留英文
TRANSLATE_NOTES = os.environ.get('CLANGD_ZH_NOTES', '1') != '0'
DEBUG = os.environ.get('CLANGD_ZH_DEBUG') == '1'
LOG_PATH = os.path.join(BASE, 'proxy.log')
FLAGS = re.IGNORECASE | re.DOTALL
FIX_TAG = re.compile(r'\s*\((?:fix|fixes) available[^)]*\)\s*$', re.I)
QUOTED = re.compile(r"'([^']*)'")
LOC_LINE = re.compile(r'^(.+?:\d+:\d+:)\s*(.*)$')
TAG_LINE = re.compile(r'^(note|error|warning|remark):\s*(.*)$', re.I)
TAG_ZH = {'note': '提示', 'error': '错误', 'warning': '警告', 'remark': '说明'}
AKA = re.compile(r"\(aka '([^']*)'\)")


def log(msg):
    if DEBUG:
        try:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(str(msg) + '\n')
        except Exception:
            pass


def load_rules():
    try:
        import zh_diagnostics
        import zh_codes
        raw = (list(zh_diagnostics.RULES)
               + list(getattr(zh_diagnostics, 'EXTRA_RULES', [])))
        rules = [(re.compile(p, FLAGS), t) for p, t in raw]
        wrappers = [(re.compile(p, FLAGS), t)
                    for p, t in getattr(zh_diagnostics, 'WRAPPERS', [])]
        note_rules = [(re.compile(p, FLAGS), t)
                      for p, t in getattr(zh_diagnostics, 'NOTE_RULES', [])]
        code_rules = dict(zh_codes.CODE_RULES)
        code_rules.update(getattr(zh_codes, 'EXTRA_CODE_RULES', {}))
        code_exact = {k: (re.compile(p, FLAGS), t)
                      for k, (p, t) in zh_codes.CODE_EXACT.items()}
        log('词典载入：文本 %d 条，note %d 条，包装 %d 条，诊断码 %d 条，精确码 %d 条'
            % (len(rules), len(note_rules), len(wrappers),
               len(code_rules), len(code_exact)))
        return rules, wrappers, code_rules, code_exact, note_rules
    except Exception as e:
        log('词典载入失败，降级为英文：%r' % e)
        return [], [], {}, {}, []


RULES, WRAPPERS, CODE_RULES, CODE_EXACT, NOTE_RULES = load_rules()


def translate(text, code=None, depth=0):
    if not isinstance(text, str) or not text or not (RULES or CODE_RULES):
        return text
    s = text.strip()
    m_fix = FIX_TAG.search(s)
    base = s[:m_fix.start()].strip() if m_fix else s
    suffix = '(有可用的快速修复)' if m_fix else ''

    def finish(zh):
        zh = (zh + suffix).strip()
        if KEEP_ORIGINAL:
            zh += '(原文：%s)' % s
        return zh

    if code and code in CODE_EXACT:
        pat, tpl = CODE_EXACT[code]
        m = pat.search(base)
        if m:
            try:
                zh = m.expand(tpl)
                log('精确码命中：%s [%s] -> %s' % (s, code, zh))
                return finish(zh)
            except Exception:
                pass

    if code and code in CODE_RULES:
        tpl = CODE_RULES[code]
        needed = max([int(n) for n in re.findall(r'\{(\d+)\}', tpl)] or [0])
        vals = QUOTED.findall(base)
        if len(vals) >= needed:
            out = tpl
            for i, v in enumerate(vals[:8], 1):
                out = out.replace('{%d}' % i, "'%s'" % v)
            out = re.sub(r'\{\d+\}', '', out).strip()
            if out:
                log('诊断码命中：%s [%s] -> %s' % (s, code, out))
                return finish(out)

    for pat, tpl in RULES:
        m = pat.fullmatch(base)
        if m:
            try:
                zh = m.expand(tpl)
            except Exception:
                continue
            log('文本命中：%s -> %s' % (base, zh))
            return finish(zh)

    if depth < 2:
        for pat, tpl in WRAPPERS:
            m = pat.fullmatch(base)
            if m:
                inner = m.group(1) if m.lastindex else ''
                return finish(tpl.replace('{0}', translate(inner, None, depth + 1)))

    log('未命中：%s [%s]' % (s, code))
    return text


def translate_note(s, depth=0):
    """翻译一行 note：专用词典优先，再退回普通文本规则。

    模板里的 {N} 表示「把第 N 个捕获组的内容再翻译一次」，
    用于 "candidate ... not viable: <原因>" 这类嵌套长句。
    """
    if not isinstance(s, str) or not s:
        return s
    for pat, tpl in NOTE_RULES:
        m = pat.fullmatch(s.strip())
        if not m:
            continue
        try:
            zh = m.expand(tpl)
        except Exception:
            continue
        if '{' in zh and depth < 3:
            def _sub(mo):
                n = int(mo.group(1))
                if n <= (m.lastindex or 0):
                    return translate_note(m.group(n) or '', depth + 1)
                return ''
            zh = re.sub(r'\{(\d+)\}', _sub, zh)
        else:
            zh = re.sub(r'\{(\d+)\}', '', zh)
        zh = AKA.sub(r"(即 '\1')", zh)
        log('note 命中：%s -> %s' % (s, zh))
        return zh
    out = translate(s, None)
    return AKA.sub(r"(即 '\1')", out) if out != s else out


def translate_line(line, code=None):
    """翻译单行，保留缩进、文件路径前缀，并把 note:/error: 标签中文化。"""
    if not line.strip():
        return line
    lead = line[:len(line) - len(line.lstrip())]
    core = line.strip()
    m = LOC_LINE.match(core)
    prefix = ''
    if m:
        prefix = m.group(1) + ' '
        core = m.group(2).strip()
        code = None  # 位置行后面的内容不是主诊断，不能套用主诊断码
    if not core:
        return line
    t = TAG_LINE.match(core)
    if t:
        tag = TAG_ZH.get(t.group(1).lower(), t.group(1))
        body = t.group(2).strip()
        zh = translate_note(body) if body else ''
        core = tag + '：' + (zh or body)
    elif code is None:
        core = translate_note(core)
    else:
        core = AKA.sub(r"(即 '\1')", translate(core, code))
    return lead + prefix + core


def translate_message(text, code=None):
    """整条诊断按行翻译；只有首行带诊断码，其余 note / 位置行不带码。"""
    if not isinstance(text, str) or not text:
        return text
    if not TRANSLATE_NOTES:
        return translate(text, code)  # 旧行为：整条一起翻，note 保持英文
    lines = text.split('\n')
    if len(lines) == 1:
        return translate_line(lines[0], code)
    return '\n'.join(translate_line(l, code if i == 0 else None)
                     for i, l in enumerate(lines))


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


def write_frame(stream, body):
    stream.write(b'Content-Length: ' + str(len(body)).encode('ascii') + b'\r\n\r\n')
    stream.write(body)
    stream.flush()


def translate_frame(body):
    try:
        msg = json.loads(body.decode('utf-8'))
    except Exception:
        return body
    if not isinstance(msg, dict) or msg.get('method') != 'textDocument/publishDiagnostics':
        return body
    params = msg.get('params') or {}
    diags = params.get('diagnostics')
    if not isinstance(diags, list):
        return body
    changed = False
    for d in diags:
        if not isinstance(d, dict):
            continue
        code = d.get('code')
        if not isinstance(code, str):
            code = str(code) if isinstance(code, (int, float)) else None
        diag_msg = d.get('message')
        if isinstance(diag_msg, str):
            new = translate_message(diag_msg, code)
            if new != diag_msg:
                d['message'] = new
                changed = True
        rel = d.get('relatedInformation')
        if TRANSLATE_NOTES and isinstance(rel, list):
            for r in rel:
                if isinstance(r, dict) and isinstance(r.get('message'), str):
                    old = r['message']
                    new = translate_message(old, None)
                    if new != old:
                        r['message'] = new
                        changed = True
    if not changed:
        return body
    return json.dumps(msg, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def main():
    args = sys.argv[1:]
    if not os.path.isfile(CLANGD):
        print('clangd not found:', CLANGD, file=sys.stderr)
        sys.exit(1)
    if any(a in ('--version', '-version') for a in args):
        sys.exit(subprocess.call([CLANGD] + args))
    try:
        server = subprocess.Popen([CLANGD] + args,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    except Exception as e:
        log('clangd 启动失败：%r' % e)
        sys.exit(1)
    log('代理启动，转发参数：%r' % (args,))

    def client_to_server():
        try:
            while True:
                body = read_frame(sys.stdin.buffer)
                if body is None:
                    break
                write_frame(server.stdin, body)
        except Exception as e:
            log('client->server 异常：%r' % e)
        finally:
            try:
                server.stdin.close()
            except Exception:
                pass

    def server_to_client():
        try:
            while True:
                body = read_frame(server.stdout)
                if body is None:
                    break
                write_frame(sys.stdout.buffer, translate_frame(body))
        except Exception as e:
            log('server->client 异常：%r' % e)

    threading.Thread(target=client_to_server, daemon=True).start()
    server_to_client()
    try:
        server.terminate()
    except Exception:
        pass


if __name__ == '__main__':
    main()
