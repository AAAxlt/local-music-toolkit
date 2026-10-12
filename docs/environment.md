# 环境安装与自检

回到 [README](../README.md)。本文以 Windows 为主；解压出的项目目录可以自行选择，路径不固定在某个盘符。

## 1. 各组件负责什么

- **Python**：运行曲库维护和歌单编辑器；`py` 是 Windows 上用来启动、选择 Python 版本的命令，不是另一种编程语言。
- **Node.js**：运行 `.mjs` 扫描导入和 NCM 处理脚本；本仓库 Node 部分使用内置模块，通常不需要执行 `npm install`。
- **FFmpeg**：将候选音频从头到尾解码检查，避免只看扩展名或文件头就把坏文件发布。
- **ffprobe**：读取容器、流和音频包信息；必须与 FFmpeg 一起准备。
- **Mutagen / Pillow**：Python 的音频标签和图片库，由项目的虚拟环境安装。
- **Frida 与 QQ 辅助工具**：仅用于可选的 QQ 客户端辅助路线，见[专门说明](qq-decoding.md)。普通音频、NCM 和歌单生成不需要安装它们。

## 2. Python

建议使用 **Python 3.12**，这是本项目本机与自动测试使用的版本。不要把未经本项目测试的新版本理解为已验证兼容。

从 [Python 官方下载入口](https://www.python.org/downloads/windows/)安装 Windows 版本。传统安装程序应保留 pip、Python Launcher 和 Tcl/Tk；如果提供“Add Python to PATH”，可勾选。新版 Python Install Manager 的使用方式见 [Windows 官方文档](https://docs.python.org/3/using/windows.html)。

安装完成后重新打开 PowerShell，检查：

```powershell
py --version
py -3.12 --version
py -3.12 -c "import tkinter; print('Tk:', tkinter.TkVersion)"
```

第二条用于确认 3.12 是否已安装；第三条确认图形选歌需要的 Tkinter 可导入。只看到命令别名、但命令报错或跳转商店，不算 Python 已安装成功。

## 3. Node.js

从 [Node.js 官网](https://nodejs.org/en/download)选择 **LTS**。本项目在 Node.js 24 本机运行、在 Node.js 22 自动测试；新安装建议选 22 或 24 LTS，而不是已结束支持的 Node.js 20。

Windows 可使用官网安装程序，并保留 PATH 设置。安装后重新打开终端：

```powershell
node --version
```

只需确认 `node` 能执行；本仓库没有必须由 npm 安装的 Node 依赖。

## 4. FFmpeg 与 ffprobe

到 [FFmpeg 官方下载页](https://ffmpeg.org/download.html)，从 **Windows EXE Files** 下提供的编译版入口选择下载。官方页主要发布源码；Windows 用户需要包含可执行文件的编译版。

解压后应同时找到：

```text
bin/ffmpeg.exe
bin/ffprobe.exe
```

本机验证版本是 FFmpeg 9.0.2。可以选当前稳定编译版，但其他版本仍应运行本项目测试；不要只安装一个名叫 `ffmpeg` 的 Python 包来代替这两个程序。

两种配置方法任选其一：

**方法 A：将 `bin` 目录加入自己账户的 PATH。** 重新打开终端后检查：

```powershell
ffmpeg -version
ffprobe -version
```

**方法 B：保留独立工具目录，在 `config.json` 中写路径。** 例如把工具放到项目下的 `external/ffmpeg/bin`：

```json
"ffmpegPath": "./external/ffmpeg/bin/ffmpeg.exe"
```

`ffprobe.exe` 必须放在同一个 `bin` 目录。相对路径以配置文件的位置为基准；也可使用实际绝对路径。配置只填 `ffmpeg.exe` 的文件路径，不要把 `-version` 等参数拼进去。

若采用方法 B，PowerShell 中可直接检查：

```powershell
& .\external\ffmpeg\bin\ffmpeg.exe -version
& .\external\ffmpeg\bin\ffprobe.exe -version
```

`external/` 已被 Git 忽略，放入的第三方工具不随代码上传。

## 5. 创建项目自己的 Python 环境

在项目目录双击 `setup.cmd`。它会创建 `.venv`，安装 `requirements.txt` 中的 Mutagen 与 Pillow，并在没有个人配置时复制 `config.example.json` 为 `config.json`。

`setup.cmd` 目前使用 `py -3` 选择默认 Python。如果你的电脑同时安装多个版本，想明确使用 3.12，可以手动执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 仅在还没有个人配置时复制，保留已有设置
if (!(Test-Path -LiteralPath config.json)) { Copy-Item config.example.json config.json }
```

不用先激活虚拟环境，直接指定 `.venv` 内的 Python 即可。自检：

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -c "import mutagen, PIL, tkinter; print('Python dependencies OK')"
node --version
```

然后按照前一节检查 FFmpeg 与 ffprobe。只运行 `setup.cmd` 不会替你安装 Node.js、FFmpeg 或 QQ 辅助工具。

## 6. 使用前最后检查

1. `sourceDirs` 指向自己的下载目录。
2. `outputDir`、`libraryDir`、`organizedDir` 指向同一个输出目录，首次应为空。
3. 输出目录与原下载目录分开；原文件保持只读。
4. 先执行扫描预览，再执行更新：

```powershell
.\.venv\Scripts\python.exe src\refresh-library.py --preview
.\.venv\Scripts\python.exe src\refresh-library.py
```

下载尚未结束的文件先等待完成。工具有未完成扩展名判断和完整解码校验，但没有对所有平台的下载状态做通用检测。

## 7. 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 找不到 `py` 或 Python 命令跳转商店 | 确认安装了实际 Python 运行时及 Launcher／Install Manager，重新打开终端 |
| `No module named mutagen` 或 `PIL` | 使用项目 `.venv` 的 Python，并用同一个解释器运行 `-m pip install -r requirements.txt` |
| 歌单窗口打不开，缺少 `tkinter` | 检查 Python 的 Tcl/Tk 组件；非 Windows 系统可能需要单独安装 Tkinter |
| `node` 无法识别 | 检查 Node.js 安装及 PATH，重新打开终端 |
| `FFmpeg not found` | 检查 `ffmpegPath` 或 PATH；必要时改成可执行文件的实际路径 |
| 整理 FLAC 时找不到 ffprobe | 确认 ffprobe 与当前配置的 ffmpeg 位于同一目录 |
| QQ 文件显示“等待” | 基础环境不一定有错，继续查看 [QQ／Frida 说明](qq-decoding.md) |

参考：[Python Windows 文档](https://docs.python.org/3/using/windows.html)、[Node.js 官方下载](https://nodejs.org/en/download)、[FFmpeg 官方下载](https://ffmpeg.org/download.html)。
