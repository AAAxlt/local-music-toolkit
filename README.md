# Local Music Toolkit · 本地音乐工具

**作者：AAAxlt、Codex（OpenAI）、DeepSeek。** 项目账号与维护者：AAAxlt。

扫描自己的音乐下载目录，将可处理的文件加入一个本地曲库；点击选歌并生成 M3U8 歌单。歌单只记录歌曲位置，**不会复制音频**。

这是脚本项目，当前提供 Windows 双击入口。Python/Node 核心脚本包含跨平台回归测试；QQ 客户端辅助流程仅适用于 Windows。

## 可以做什么

- 扫描一个或多个配置目录，识别普通 MP3、FLAC、OGG、M4A、WAV 和 NCM。
- NCM 在本机离线处理；解出来的容器保持原格式，不做有损转码。
- 发布前用 FFmpeg 完整解码校验，处理失败的文件不会加入正式曲库。
- 保留原下载文件，记录来源及输出哈希，重复运行跳过未变化的下载。
- 管理一个输出目录，规范化经过校验的 FLAC/OGG 尾部，并归档陈旧输出。
- 搜索、点击、多选歌曲，调整歌单顺序，保存、重新打开 M3U8。
- 默认生成**相对于歌单文件**的歌曲路径；同时移动曲库与歌单目录后仍可使用。可勾选“使用绝对路径”适配需要完整路径的播放器。跨 Windows 盘符时自动退回绝对路径。

QQ 的 `.mflac/.mgg` **不承诺全部支持**。仓库不附带 QQ 辅助程序；未配置或未显式启用时报告为等待。现有普通音频与 NCM 不依赖 QQ 客户端。

## 第一次使用（Windows）

1. 从仓库页面点 **Code → Download ZIP**，解压到自己的目录。
2. 安装 Python 3.12 或更新版本、Node.js 20 或更新版本，以及 FFmpeg（包含 `ffprobe`）。令 `node` 和 `ffmpeg` 可在终端执行；FFmpeg 也可在配置里填写可执行文件路径。
3. 双击 `setup.cmd`，Python 依赖安装在项目自己的 `.venv` 目录。成功后生成个人 `config.json`。
4. 编辑 `config.json` 的 `sourceDirs`，填入自己的下载目录。所有相对路径**以配置文件所在目录为基准**，不会随着启动位置改变。
5. 双击 `refresh.cmd`。保留的歌曲在 `library`，报告在 `.reports`。初次输出目录应为空。
6. 双击 `playlist.cmd`，搜索并点击加入歌曲，填写歌单名后保存。生成的文件在 `playlists`。也能选择自己已有的本地曲库。

示例配置默认读取项目下的 `downloads`。也可以设置自己的绝对路径，例如：

```json
"sourceDirs": ["E:/MusicDownloads", "./downloads"]
```

`outputDir`、`libraryDir` 和 `organizedDir` 应保持指向同一个受管理目录。原下载目录与输出目录分开设置。

## 命令行

```powershell
# 查看扫描计划，不解码、不写曲库
.venv\Scripts\python.exe src\refresh-library.py --preview

# 默认离线更新曲库
.venv\Scripts\python.exe src\refresh-library.py

# 列出可选歌曲
.venv\Scripts\python.exe src\make-playlist.py --list

# 按歌手或歌名关键词建单
.venv\Scripts\python.exe src\make-playlist.py --name "精选" --match "歌手名字,歌名"

# 需要绝对歌曲路径时
.venv\Scripts\python.exe src\make-playlist.py --name "精选" --match "歌名" --absolute
```

封面歌词使用已有本地资料。只有明确传入 `--online` 才进行元数据查询，不上传音频。QQ 辅助处理需要自行配置 `qqd` 并明确传入 `--allow-qqd`；不要把未配置的兼容性当作已经通过。

## 目录

```text
src/                  扫描导入、NCM 处理、曲库维护、歌单编辑器
tests/                自动生成音频的回归测试
config.example.json   可分享的示例配置
config.json           自己的配置，不上传 Git
downloads/            原下载文件，不上传 Git
library/              处理后的曲库，不上传 Git
playlists/            自己的歌单，不上传 Git
.work/ .cache/        临时文件与缓存，不上传 Git
.reports/ .backups/   报告与可恢复备份，不上传 Git
```

## 更新到自己的 GitHub

首次上传可在 GitHub 点右上角 **+ → New repository**，填写名称并选择可见性。上传的是此项目的代码和示例配置，个人配置、歌曲与运行数据由 `.gitignore` 排除。

已克隆或关联仓库后，在项目目录执行：

```bash
git status
git add src tests README.md config.example.json
git commit -m "Update music tools"
git push
```

先看 `git status`，确认提交内容是预期的代码文件。也可以使用 GitHub Desktop 查看改动，点击 **Commit to main → Push origin**。

公开仓库可以直接给朋友分享仓库链接或下载 ZIP 链接，下载无需 GitHub 账号。修改和提交代码仍需要对应权限。

## 测试

```bash
python -m unittest discover -s tests -p "test_*.py"
node --test tests/test-import.mjs
```

测试生成临时音频，检查相对路径、重名保护、增量导入、原文件不变、受管理目录改名后的引用以及音频校验。测试数据不包含真实歌曲。

## 项目状态与依赖

从本机已使用的脚本拆分，当前为第一版。Python 依赖为 Mutagen 与 Pillow；Node.js 侧使用内置模块。FFmpeg 和可选 QQ 辅助程序由使用者自行安装。

仓库目前未指定开源许可证。公开可查看与下载，并不代表自动采用某个开源许可证；后续如需开放代码复用，可再添加明确的许可证。第三方依赖各自遵循其原许可证。
