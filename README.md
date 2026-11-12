# 让 clangd 的报错说中文：一个只改写 LSP 诊断文本的本地代理

> 本文面向在 VS Code 里用 clangd 写 C++、却被英文报错拦住的同学。方案不更换编译器、不修改 clangd 本体、不改动任何一行自己的代码，也不影响补全、跳转与格式化；它只在语言服务器与编辑器之间，把「诊断的文字」翻译成中文。

## 一、问题从哪里来

### 先厘清三个容易混为一谈的东西

| 名称 | 它是什么 | 会不会生成可执行文件 |
| :--- | :--- | :--- |
| Clang | LLVM 的 C/C++ 编译器前端 | 会 |
| clangd | 基于 Clang 的**语言服务器**，为编辑器提供补全、跳转、诊断 | 不会 |
| MinGW-w64 / TDM-GCC | Windows 上的 GNU 工具链发行版，含 `g++.exe` 与标准库头文件 | 会 |

很多同学把「装 clangd」理解成「把编译器换成 Clang」，其实两者互不替代：本方案里 `g++` 依然负责编译，clangd 只负责在编辑器里做语义分析。这一点决定了后面所有设计——**报错是 clangd 吐给编辑器的一份结构化数据，不是编译器的终端输出**。

按 [Language Server Protocol 3.17 规范](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/)，诊断由 `textDocument/publishDiagnostics` 通知承载，其中 `diagnostics[].message` 是给人看的字符串，`code` 是稳定的机器可读标识，`range` 是位置，`relatedInformation` 是补充说明。既然文字只是数据里的一个字段，就可以在**不改变语义**的前提下替换它。

### 为什么常见做法都不太合适

- **换编译器 / 换成中文报错的 IDE**：工具链、评测环境和习惯都要跟着改，竞赛场景下代价过高。
- **装汉化插件**：汉化的是界面，诊断文本由语言服务器产生，插件管不到。
- **复制报错到在线翻译**：断网不可用，且会丢掉 `'标识符'`、`aka '类型'` 这类需要保留的结构。
- **改 clangd 源码重新编译**：LLVM 体积巨大，且每次升级都要重来。

于是把约束写清楚：**本地、离线、零外部依赖、可迁移到另一台电脑、未命中的诊断必须原样保留英文**。最后一条尤其重要——宁可不翻，也不能翻错而误导读者。

> **获取方式**：本文配套文件是仓库根目录下的 [clangd-zh-translation.zip](./clangd-zh-translation.zip)（8 个文件、约 35 KB）。它**不含** Python 运行时，也**不含** clangd 本体，两者都用读者机器上已装好的。第 2 至 4 节给出了全部关键实现，读者也可据此自行复现，不依赖该附件。

## 二、整体设计：在标准输入输出上做一层只改文本的转发

VS Code 的 [vscode-clangd 扩展](https://marketplace.visualstudio.com/items?itemName=llvm-vs-code-extensions.vscode-clangd) 通过标准输入输出与语言服务器通信。把扩展的 `clangd.path` 指向一个启动脚本，由脚本拉起 Python 代理，代理再拉起真正的 `clangd.exe`：

```text
 VS Code (vscode-clangd 扩展)
        |   ^   LSP / JSON-RPC，双向 stdio 字节流
        v   |
 clangd-zh.cmd  ->  clangd_zh_proxy.py  (本方案唯一新增的运行环节)
                         |   ^
                         v   |
                    clangd.exe  (官方原版，二进制未修改)
                         |
                    读取源码、解析、产生诊断
```

代理的职责边界非常窄，这也是它安全、稳定的原因：

| 帧的种类 | 代理的行为 |
| :--- | :--- |
| `initialize`、`completion`、`definition`、`formatting` 等全部请求与响应 | **原字节转发**，不解析、不改写 |
| `textDocument/publishDiagnostics` 通知 | 解析 JSON，只改 `message` 与 `relatedInformation[].message` |
| 词典未命中的诊断 | 保持英文原文，整帧原字节转发 |
| 任何解析或翻译异常 | 降级为原帧转发，绝不因翻译失败让语言服务器失联 |

因此补全、跳转、悬停提示、代码格式化、快速修复（Quick Fix）都走原通道，行为与不装代理时完全一致。

### 关键实现之一：必须重算 Content-Length

LSP 的每一帧形如 `Content-Length: N\r\n\r\n<JSON>`，其中 `N` 是**消息体的 UTF-8 字节数**，不是字符数。英文是单字节 ASCII，常用汉字在 UTF-8 下占 3 字节，翻译之后帧长必然改变。举个实际例子：

```text
"Expected ';' at end of declaration"   34 字节
"声明末尾缺少分号 ';'"                 28 字节
```

若沿用旧的 `Content-Length: 34`，接收方会多读 6 个字节，把下一帧的头部当成消息体，后续所有通信错位——表现为「补全突然全挂」。所以写出时必须用**编码后的字节串长度**重新生成头部：

```python
def read_frame(stream):
    headers = {}
    while True:
        line = stream.readline()
        if line in (b'\r\n', b'\n'):
            break
        if b':' in line:
            k, v = line.decode('ascii', 'replace').split(':', 1)
            headers[k.strip().lower()] = v.strip()
    return stream.read(int(headers.get('content-length', '0') or '0'))

def write_frame(stream, body):
    # body 是 UTF-8 字节串，len(body) 就是正确的 Content-Length
    stream.write(b'Content-Length: %d\r\n\r\n' % len(body))
    stream.write(body)
    stream.flush()
```

（以上为节选，实际程序还处理了流结束、长度不足与异常。）配套要求是：全程使用二进制流、无缓冲，序列化时用 `json.dumps(..., ensure_ascii=False)` 让中文以真实 UTF-8 输出，而不是被转义成 `\uXXXX`——后者虽然也能用，但会显著放大帧长且不利于日志排查。

### 关键实现之二：只认一种通知

```python
def translate_frame(body):
    try:
        msg = json.loads(body.decode('utf-8'))
    except Exception:
        return body                       # 解析失败：降级
    if not isinstance(msg, dict) or msg.get('method') != 'textDocument/publishDiagnostics':
        return body                       # 其它帧：一个字节都不动
    diags = (msg.get('params') or {}).get('diagnostics')
    if not isinstance(diags, list):
        return body
    changed = False
    for d in diags:
        code = d.get('code')
        new = translate_message(d['message'], code)
        if new != d['message']:
            d['message'] = new
            changed = True
        for r in d.get('relatedInformation', []):
            new = translate_message(r['message'], None)
            if new != r['message']:
                r['message'] = new
                changed = True
    if not changed:
        return body                       # 没命中词典，连重编码都省了
    return json.dumps(msg, ensure_ascii=False).encode('utf-8')
```

两个容易被忽略的工程细节：其一，扩展启动时会执行 `clangd --version` 来探测版本，代理必须把命令行参数**原样透传**给真正的 clangd，否则扩展会认为语言服务器不可用；其二，代理不参与请求与响应的 `id` 配对，因此不会破坏 LSP 的状态机。

## 三、词典设计：诊断码优先，文本兜底

### 匹配优先级

| 优先级 | 规则集 | 匹配依据 | 作用 |
| :--- | :--- | :--- | :--- |
| 1 | `CODE_EXACT` | `code` + 正则 | 需要从句中抽取动态内容，如构造函数初始化的类型名 |
| 2 | `CODE_RULES` | `code` + 中文模板 | 主力；`{1}`、`{2}` 自动填入原文里单引号括起的符号 |
| 3 | `NOTE_RULES` | note 行专用正则 | 候选提示、声明位置、`note:` 标签 |
| 4 | `RULES` | 英文整句正则 `fullmatch` | 无 `code` 或新版本诊断的兜底 |
| 5 | `WRAPPERS` | `In template: ...` 之类前缀 | 剥掉外壳后递归翻译内层 |

把 `code` 放在最前面，是因为 clangd 的 `diagnostic.code` 比英文措辞稳定得多：错误码是内部诊断名（如 `undeclared_var_use`、`expected_semi_declaration`），而**警告码是 `-W` 开头的开关名**（如 `-Wunused-value`、`-Wmissing-declarations`）。这一点很反直觉——`Expression result unused` 若按错误码去找会一无所获，必须按 `-Wunused-value` 登记。

模板里只翻译说明性文字，**标识符、类型名、文件名一律保留原样**：

```python
"undeclared_var_use": "使用了未声明的标识符 {1}(检查拼写、声明或头文件)",
"-Wunused-value": "表达式的结果没有被使用(这条语句没有产生任何效果)",
"invalid_decl_spec_combination": "不能与前面的 {1} 类型说明符组合使用(是否重复写了类型？)",
```

于是 `Use of undeclared identifier 'cout'` 变成 `使用了未声明的标识符 'cout'(检查拼写、声明或头文件)`，读者仍能一眼看出是哪个名字写错了。

### 三个实际踩到的坑

**其一，`(fix available)` 后缀。** VS Code 会在可快速修复的诊断末尾追加这个标注，导致整句 `fullmatch` 失败。处理方式是先剥离尾部标注、翻译主句，再补回 `(有可用的快速修复)`。

**其二，多行 `message` 里混着 note。** clangd 在构造失败、变长数组等诊断里，会把候选说明和位置引用拼进同一个 `message` 的多行文本，形如：

```text
No matching constructor for initialization of 'std::vector<int>'

D:\...\bits\stl_vector.h:497:7:
note: candidate constructor not viable: no known conversion from 'const char[4]' ...
```

如果整条消息套用主诊断码，后面的 note 会被主句模板吞掉或翻错。正确做法是**按行翻译**：只有首行携带 `code`，其余行一律走 note 专用词典，并把 `note:` / `warning:` / `error:` 标签译为 `提示：` / `警告：` / `错误：`。像 `candidate constructor not viable: <原因>` 这种嵌套长句，再对 `<原因>` 子句做一次递归翻译。

**其三，`relatedInformation` 是独立字段。** 它不在 `message` 里，属于诊断对象自己的数组，必须单独遍历。否则「在此处声明」「上一个定义在这里」这类定位提示仍是英文。

此外，译文风格上统一使用半角括号 `()`，避免中英混排时括号全半角不一致；`aka 'T'` 统一转成 `(即 'T')`；文件路径与行号原样保留，保证能直接点回源码。若只想翻译主句、让 note 保持英文，可设环境变量 `CLANGD_ZH_NOTES=0` 回到旧行为。

截至本文写作时，词典共 **547 条生效规则**（重复键已合并）：

| 类别 | 数量 | 说明 |
| :--- | :--- | :--- |
| 文本规则 | 257 | 按英文整句匹配，含 9 条后补规则 |
| note 专用规则 | 65 | 候选提示、位置说明、原因子句 |
| 诊断码规则 | 214 | 主力，含警告 `-W*` 码 |
| 精确码规则 | 8 | 带正则捕获，处理动态内容 |
| 包装规则 | 3 | 模板实例化等前缀 |
| **合计** | **547** | 未命中的一律保留英文 |

Clang 的诊断总数以千计，547 条覆盖的是学习阶段最常遇到的错误与警告；这是**有意的设计取舍**，而不是遗漏。

## 四、Windows + MinGW 的必备环节：fallbackFlags

这一节与翻译无关，却是在 Windows 上让 clangd 正常工作的前提，也是本方案里最「工程」的部分。

clangd 需要知道源码的编译参数才能找到标准库头文件。官方推荐 `compile_commands.json`，若所有文件共用一套参数也可在项目根目录放 `compile_flags.txt`（见 [clangd 官方安装与项目配置说明](https://clangd.llvm.org/installation)）。但竞赛场景下常是**单个 `.cpp` 文件、没有构建系统、两个配置文件都不存在**，此时 clangd 才会退回到编辑器通过初始化选项传入的 `fallbackFlags`。若不配置，`#include <bits/stdc++.h>` 直接报 `'bits/stdc++.h' file not found`，随后一连串类型都解析不出来。

一个很自然的想法是用 `--query-driver` 让 clangd 自己去问 `g++` 的系统头文件路径。但在本文实测环境（clangd 23.1.0、无编译数据库、单文件直接打开）下，**fallback 模式里 `--query-driver` 不生效**；这属于该版本、该场景下的观察，不作为其它版本的普遍结论（安装脚本仍保留该参数，它在存在编译数据库的场景中有用）。可靠的办法是安装时主动执行一次：

```text
g++ -E -x c++ -v -
```

从输出中解析出 `#include <...> search starts here` 与 `End of search list` 之间的路径，生成 7 条 `-isystem`，连同 `--target=x86_64-w64-mingw32` 与 `-std=c++14` 一起写入 `clangd.fallbackFlags`（共 16 个数组元素）。路径是**运行时问出来的**，不写死 GCC 版本号，因此换机、升级工具链后重跑安装脚本即可刷新。

本机对照实验（源码仅 `#include <bits/stdc++.h>` 与空的 `main`）：

| 配置 | 诊断条数 | 内容 |
| :--- | :--- | :--- |
| 无 `fallbackFlags` | 1 | `'bits/stdc++.h' file not found`，码 `pp_file_not_found` |
| 按上法生成 `fallbackFlags` | 0 | 无错误 |

写入 VS Code 配置的核心两行是：

```json
{
  "clangd.path": "D:/clangd/clangd-zh.cmd",
  "clangd.useScriptAsExecutable": true
}
```

`clangd.useScriptAsExecutable` 必须为真，否则扩展会拒绝把 `.cmd` 当作可执行文件。另外建议把微软 C/C++ 扩展的 `C_Cpp.intelliSenseEngine` 设为 `disabled`，避免两套补全互相打架。

## 五、安装、验证与回滚

### 实测环境

本文全部数据与输出均来自下表环境，其它平台与 clangd 版本未做验证，结论请勿直接外推。

| 项目 | 版本 / 值 |
| :--- | :--- |
| 操作系统 | Windows |
| clangd | 23.1.0 (windows+grpc, x86_64-pc-windows-msvc) |
| 编译器 | g++ (tdm64-1) 10.3.0，即 TDM-GCC |
| Python | 3.13.14 |
| vscode-clangd 扩展 | 0.6.0 |
| C++ 标准 | `-std=c++14`（可选 17 / 20） |

### 依赖与三步安装

必需：[Python](https://www.python.org/) 3.8 及以上、clangd 本体（[官方安装说明](https://clangd.llvm.org/installation) 提供了独立 zip 发布渠道）、VS Code 与 vscode-clangd 扩展。建议安装 [MinGW-w64](https://www.mingw-w64.org/) 系工具链，否则第 4 节无法自动获得标准库路径。

1. 解压压缩包到任意目录，**整个文件夹一起移动**，中文路径、空格路径均实测可用。
2. 双击 `安装.cmd`：依次完成「探测 Python / clangd / g++ → 把绝对路径写进启动器并生成编译参数 → 备份并更新 `settings.json`、安装扩展 → 起一次真实语言服务验证中文诊断」。
3. 在 VS Code 里执行 `Ctrl+Shift+P` → `Reload Window`。**这一步不能省**：已经运行的旧代理进程不会热更新。

自动探测失败时，可用 `CLANGD_PY`、`CLANGD_EXE`、`CLANGD_GXX` 三个环境变量手动指定。`安装.cmd --test` 只做探测与验证、不写任何配置；`安装.cmd --std=c++17` 可切换标准。修改过任何 `.py` 后同样需要 `Reload Window`。

### 文件清单

| 文件 | 作用 |
| :--- | :--- |
| `安装.cmd` | 双击入口，纯 ASCII 以避免 cmd 中文乱码 |
| `install.py` | 探测、写启动器、合并配置、端到端验证 |
| `clangd-zh.cmd` | VS Code 实际调用的启动器，内含本机绝对路径 |
| `clangd_zh_proxy.py` | LSP 代理本体，只改写诊断文本 |
| `zh_codes.py` | 诊断码词典 |
| `zh_diagnostics.py` | 文本词典、包装规则、note 专用规则 |
| `compile_flags.txt` | 编译参数模板，供换编辑器或做项目级配置时复制 |
| `使用说明.txt` | 面向使用者的完整说明 |

### 可复现的验证

把下面这段存成 `demo.cpp`，在装好代理的 VS Code 里打开：

```cpp
int main() {
    int a = 1
    cout << x;
    1 + 2;
    return 0;
}
```

英文原文与代理输出的实际对照（取自本机的真实语言服务会话，方括号为诊断码）：

```text
[expected_semi_declaration]
EN  Expected ';' at end of declaration (fix available)
ZH  声明末尾缺少分号 ';'(有可用的快速修复)

[undeclared_var_use]
EN  Use of undeclared identifier 'cout'
ZH  使用了未声明的标识符 'cout'(检查拼写、声明或头文件)

[undeclared_var_use]
EN  Use of undeclared identifier 'x'
ZH  使用了未声明的标识符 'x'(检查拼写、声明或头文件)

[-Wunused-value]
EN  Expression result unused
ZH  表达式的结果没有被使用(这条语句没有产生任何效果)
```

再看一条多行诊断。`std::vector<int> v("abc");` 会触发构造函数不匹配，主句与候选 note 一并中文化：

```text
EN
No matching constructor for initialization of 'std::vector<int>'

D:\gcc\lib\gcc\x86_64-w64-mingw32\10.3.0\include\c++\bits\stl_vector.h:497:7:
note: candidate constructor not viable: no known conversion from 'const char[4]'
      to 'const allocator_type' (aka 'const std::allocator<int>') for 1st argument

ZH
没有可用于初始化 'std::vector<int>' 的构造函数

D:\gcc\lib\gcc\x86_64-w64-mingw32\10.3.0\include\c++\bits\stl_vector.h:497:7:
提示：候选构造函数不可用：第 1 个参数无法从 'const char[4]' 转换为
'const allocator_type'(即 'const std::allocator<int>')
```

（英文原文中的候选说明在真实输出里是单行，此处为排版做了折行；中文译文中的换行同样仅为排版。）文件路径、行号与代码符号保持原样，仍可点击定位。

### 按项目临时关闭翻译

需要核对英文原文（例如查官方文档、报告问题）时，不必卸载或改配置。代理会在处理每一帧前读取 VS Code 的 `tasks.json`，查找 `clang-zh` 这一项；值为 `null` 时，诊断帧**原字节透传，不做任何解析和翻译**，与没装代理时完全一致：

```json
{
    "version": "2.0.0",
    "clang-zh": null,
    "tasks": []
}
```

写 `"None"`、`"none"`、`"off"`、`"false"`、`"disabled"`、`false` 或 `0` 同样表示关闭；改成 `"on"`（或任意其它非禁用值）即恢复。键名不区分大小写，也不区分 `-`、`_`、`.` 分隔符，`Clang_ZH` 一样能识别；开关可以写在顶层、`options`、`inputs` 或某个 task 里，文件允许 JSONC 注释与尾逗号。这一项对 VS Code 的任务系统没有任何影响，只是借 `tasks.json` 这个现成的位置放开关。

查找顺序（靠前的优先）：环境变量 `CLANGD_ZH` > `CLANGD_ZH_TASKS_DIR` 指定目录 > 工作区 `.vscode/tasks.json` > 工作区根目录 `tasks.json` > 用户级 `tasks.json`。用户级路径在 Windows 是 `%APPDATA%\Code\User\tasks.json`，macOS 是 `~/Library/Application Support/Code/User/tasks.json`，Linux 是 `~/.config/Code/User/tasks.json`，并同时覆盖 Code、Insiders 与 VSCodium。工作区目录从 LSP `initialize` 的 `rootUri` / `workspaceFolders` 只读获取，不改写任何字节。

改完 `tasks.json` **立刻生效，不需要 `Reload Window`**：代理按文件的 mtime 与大小做缓存，每帧检查一次（0.8 s 内复用结果）。需要强制覆盖时设 `CLANGD_ZH=1`（强制翻译）或 `CLANGD_ZH=0`（强制关闭）。安装脚本的 `--test` 自检会临时强制开启翻译，因此不受读者本机开关影响。

### 回滚

安装时会把原 `settings.json` 备份为 `settings.json.bak-zh-年月日-时分秒`。想彻底关闭翻译，把 `clangd.path` 改回真实 `clangd.exe` 并将 `clangd.useScriptAsExecutable` 设为 `false` 即可。想查哪些诊断没命中，设 `CLANGD_ZH_DEBUG=1`，未命中原文会写入同目录的 `proxy.log`，照着补词典最快。

## 六、边界、风险与诚实声明

- **覆盖有限**：547 条规则，冷门诊断保留英文；clangd 升级后措辞变化可能导致个别规则失配，后果是「显示英文」而非「显示错译」。
- **只改显示层**：不改 `severity`、`range`、诊断码与 Quick Fix，不改变编译行为，不修改源码；编译与评测仍由原来的 `g++` 完成。
- **可随时关闭**：`tasks.json` 中 `clang-zh` 为禁用值时诊断帧原样转发；开关只影响显示文本，不改变编译、补全与跳转，也不写入任何配置。
- **开销**：仅 `publishDiagnostics` 帧做一次 JSON 解析与序列化，其余帧原样转发；本机使用未观察到可感知延迟，但未做量化基准测试。
- **无网络行为**：全程本地 stdio，不联网、不上传代码、不需要密钥；不修改系统 `hosts`、不写注册表、不注入其它进程，也不替换任何编译器或 clangd 的二进制文件。对 `settings.json` 的唯一写入会先备份、可回滚。
- **扩展冲突**：微软 C/C++ 扩展的 IntelliSense 与 clangd 同时开启会重复补全，建议只留一个。
- **适用范围**：目前只在第 5 节所列 Windows 环境验证；Linux / macOS、其它 clangd 版本、带 `compile_commands.json` 的工程未做完整回归。

## 七、与常见做法的对比

| 维度 | 换编译器 / 换 IDE | 界面汉化插件 | 复制报错到在线翻译 | 本方案 |
| :--- | :--- | :--- | :--- | :--- |
| 是否动工具链 | 是 | 否 | 否 | 否 |
| 是否覆盖诊断文本 | 不一定 | 否 | 是，但需手动 | 是，自动 |
| 离线可用 | 是 | 是 | 否 | 是 |
| 影响补全 / 跳转 | 可能 | 否 | 无 | 否 |
| 保留符号与路径 | 视实现 | 不适用 | 常被译坏 | 是 |
| 可迁移 / 可回滚 | 困难 | 容易 | 不适用 | 8 文件压缩包，一键回滚 |
| 未覆盖时的行为 | — | — | — | 保留英文 |

它的定位不是「更准的诊断」，而是**把已有的英文诊断无损地换一种语言呈现**：诊断内容仍完全来自官方 clangd，翻译层不参与语义判断。

## 八、小结

这个方案能成立，归根结底是因为 LSP 把「诊断」设计成了结构化数据：文字只是其中一个字段。只要守住三条边界——按 UTF-8 字节重算帧长、只改写 `publishDiagnostics`、未命中就原样放行——就能在不碰编译器、不碰源码、不联网的前提下，把报错变成中文。对刚开始学 C++ 的同学，少查一次词典，就能多看一眼代码本身。

## 附录 A：生成式 AI 使用说明

依据[洛谷《生成式人工智能（GenAI）辅助文章写作规范》](https://help.luogu.com.cn/rules/academic/ai-assisted-article-writing)，本文披露如下：

- **项目代码**：代理程序、词典条目与安装脚本由作者在 GenAI 辅助下编写，并逐项审阅、修正与实测；需求定义、方案取舍、参数取值与最终定稿由作者确定。
- **本文写作**：GenAI 用于 Markdown 结构编排、文字润色、格式规范化及部分说明性段落的初稿；**未**用于产生实测结论。文中全部版本号、规则数量、配置项、命令行与诊断输出，均由作者在第 5 节所列环境中逐项复核。
- **责任**：作者对署名文章的全部内容承担完全责任。
- 本文排版依据[洛谷 Markdown 格式手册](https://help.luogu.com.cn/rules/academic/handbook/markdown)，板块要求依据[洛谷专栏全站推荐规范](https://help.luogu.com.cn/rules/academic/article-standard)。

## 附录 B：参考资料

- [Language Server Protocol Specification — 3.17](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/)：帧格式、`publishDiagnostics` 与诊断对象结构。
- [clangd 官方 Getting started / Installation](https://clangd.llvm.org/installation)：`compile_commands.json`、`compile_flags.txt`、`fallbackFlags` 与编辑器扩展配置。
- [clangd 官网](https://clangd.llvm.org/)：clangd 的功能与定位。
- [vscode-clangd 扩展（Visual Studio Marketplace）](https://marketplace.visualstudio.com/items?itemName=llvm-vs-code-extensions.vscode-clangd)：`clangd.path`、`clangd.arguments`、`clangd.fallbackFlags` 等设置项。
- [MinGW-w64 官网](https://www.mingw-w64.org/) 与 [Python 官网](https://www.python.org/)：本方案的外部依赖。
- [洛谷 Markdown 格式手册](https://help.luogu.com.cn/rules/academic/handbook/markdown)、[洛谷专栏全站推荐规范](https://help.luogu.com.cn/rules/academic/article-standard)、[洛谷 GenAI 辅助文章写作规范](https://help.luogu.com.cn/rules/academic/ai-assisted-article-writing)：本文的排版与学术规范依据。
