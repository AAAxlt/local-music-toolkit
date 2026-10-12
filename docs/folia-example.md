# 实例：解码后的曲库与 M3U8 歌单导入 Folia

回到 [README](../README.md)。Folia 是独立播放器，本仓库负责准备音频和歌单。我们通过文件夹和 M3U8 衔接，没有安装 Folia 插件或调用它的私有数据库接口。

## 1. 先看我们实际采用的结构

本机最终只保留一个正式曲库入口，歌单在旁边：

```text
本地音乐/
├─ 全部歌曲/                       ← Folia 先导入这里
│  ├─ AGA - 孤雏.flac
│  ├─ 爱乐团王超 - 天涯.ogg
│  ├─ toe_土岐麻子 - グッドバイ -album version-.mp3
│  └─ …
└─ 歌单/
   └─ Axlt.m3u8                    ← 然后导入这个引用列表
```

这三个文件名代表 QQ MFLAC、QQ MGG 和 NCM 的实际产物；音频不在 GitHub 仓库里。新用户使用默认配置时，对应目录名是 `library/` 与 `playlists/`，不需要复制本机的盘符。

2026-10-12 的检查结果：正式曲库 174 首，Axlt 歌单 110 条引用，文件存在性检查全部通过；按本机已安装 Folia 的路径匹配函数复核，匹配 110、缺失 0、歧义 0。174 首的此前完整音频审查记录也没有解码失败。

这些是本机文件和匹配逻辑的结果，**不把它们写成这次重新操作 Folia 窗口并逐首播放的证明**。公开仓库也不上传完整私人歌单。

## 2. 普通使用：先导入歌曲，再导入歌单

完成[环境准备](environment.md)并设置自己的下载路径后：

```powershell
# 扫描预览
.\.venv\Scripts\python.exe src/refresh-library.py --preview

# 普通音频与 NCM
.\.venv\Scripts\python.exe src/refresh-library.py

# 若已经另外准备好 QQ 辅助工具并满足客户端前提，更新时使用这个开关
# .\.venv\Scripts\python.exe src/refresh-library.py --allow-qqd
```

接着双击 `playlist.cmd`。左边搜索和选择歌曲，点“加入 →”或双击加入，在右边调整顺序，填写歌单名并保存。也能通过命令行选歌：

```powershell
.\.venv\Scripts\python.exe src/make-playlist.py --name "试听精选" --match "孤雏,天涯,グッドバイ"
```

命令中的歌名只是示例，必须换成自己曲库里已有的歌曲。关键词可能选中多个版本，运行结果里的歌曲数和未匹配项应核对。

然后在 Folia 中按以下顺序操作：

| 顺序 | 操作 | 默认项目里的选择 |
| --- | --- | --- |
| 1 | 进入本地音乐区域，点“导入文件夹” | 选择解压项目中的 `library` |
| 2 | 等待扫描和元数据读取完成 | 先确认曲库能看到歌曲 |
| 3 | 点“导入歌单” | 选择 `playlists/试听精选.m3u8` |
| 4 | 查看导入结果，再点歌播放 | 核对歌曲数量及未匹配／歧义提示 |

Folia 的歌单导入在已经建立的曲库索引里匹配路径；M3U8 本身不会替你导入歌曲或授予文件夹权限。界面名称可能随版本变化，功能依据见 [Folia 官方本地曲库说明](https://github.com/chthollyphile/folia-major/blob/main/docs/local-library-management.md#导入与导出-m3u-歌单)。

## 3. 不用真实歌曲也能演示：两段原创测试音

仓库提供 [`examples/create-folia-demo.py`](../examples/create-folia-demo.py)，用 Python 标准库生成两段两秒的低音量正弦测试音。它不会下载音乐、播放声音、操作 QQ 或修改正式曲库。

这段演示用于验证**普通音频导入 → 正式曲库 → 路径歌单 → Folia 操作步骤**。测试音不是 NCM／QQ 加密文件，不能证明那些解密路线可用。

在项目目录执行：

```powershell
# 1. 准备独立下载样本和配置
.\.venv\Scripts\python.exe examples/create-folia-demo.py

# 2. 用同一个正式入口检查并整理样本
.\.venv\Scripts\python.exe src/refresh-library.py --config .work/folia-demo/config.json --preview
.\.venv\Scripts\python.exe src/refresh-library.py --config .work/folia-demo/config.json

# 3. 创建只引用两段音频的歌单
.\.venv\Scripts\python.exe src/make-playlist.py --library .work/folia-demo/library --out-dir .work/folia-demo/playlists --name "Folia Demo" --match "440Hz,660Hz"
```

要求基础环境可用，尤其是 FFmpeg 在 PATH 中；或在演示自己的 `.work/folia-demo/config.json` 中修改 `ffmpegPath`。生成器拒绝覆盖已有目录；重做时使用 `--out-dir .work/folia-demo-2`，后续命令也相应换目录。

运行后应得到：

```text
.work/folia-demo/
├─ config.json
├─ downloads/                       ← 两个原始 WAV，保留不动
│  ├─ Demo - Tone 440Hz.wav
│  └─ Demo - Tone 660Hz.wav
├─ library/                         ← 检查和整理后的两个 WAV
│  ├─ Demo - Tone 440Hz.wav
│  └─ Demo - Tone 660Hz.wav
├─ playlists/
│  └─ Folia Demo.m3u8
└─ .reports/                        ← 另有缓存、清单与工作目录
```

生成的歌单正文如下（UTF-8，使用 `/` 分隔路径）：

```m3u
#EXTM3U
#PLAYLIST:Folia Demo

#EXTINF:-1,Demo - Tone 440Hz
../library/Demo - Tone 440Hz.wav
#EXTINF:-1,Demo - Tone 660Hz
../library/Demo - Tone 660Hz.wav
```

在 Folia **先导入 `.work/folia-demo/library`，再导入 `Folia Demo.m3u8`**。应出现含两项的歌单；点击播放，听到两段不同频率的测试音。下面是操作示意，不是伪造的 Folia 截图：

```mermaid
flowchart LR
    A[导入文件夹：demo/library] --> B[曲库里先出现两个 WAV]
    B --> C[导入歌单：Folia Demo.m3u8]
    C --> D[按顺序显示两项：440Hz、660Hz]
    D --> E[手动点播放并确认声音]
```

歌单文件只有少量文本；建第二个歌单不会再复制两个 WAV。保留原下载与处理后曲库、缓存的空间占用是另一件事。

本机已验证这段演示：2 个输入导入成 2 个正式音频，歌单含 2 条有效相对引用；再次更新没有新增副本，原始 WAV 哈希未变。用已安装 Folia 的匹配函数核对两种导入根目录形态，均为 2 匹配、0 缺失、0 歧义。窗口内的点播步骤仍需使用者按上面顺序操作确认。

如果更喜欢点击选歌，可以用以下命令直接打开演示曲库的编辑器：

```powershell
.\.venv\Scripts\python.exe src/playlist-editor.py .work/folia-demo/library .work/folia-demo/playlists
```

## 4. 实际遇到的“81 条路径找不到或有歧义”

这个提示首先表示**歌单文本与当前曲库索引没有唯一对应**，不能仅凭数量就认定 81 首音频全部损坏。我们整理后曾改过目录和文件名，原曲库与整理曲库又同时存在，必须一起审查引用与已导入范围。

这次整理采取了几个具体动作：统一到 `全部歌曲`，旧输出审查后归档；Axlt 引用更新为正式文件的完整路径；对已安装 Folia 的匹配函数验证了“导入父目录”和“直接导入曲库”两种路径形态。两种都得到 110 条匹配、0 缺失、0 歧义。

排查时按下面的顺序，避免重新解密本来已经正常的歌曲：

| 检查 | 做法 |
| --- | --- |
| 歌曲是否先进入 Folia 曲库 | 先导入实际音乐文件夹，等扫描完成 |
| 歌单是否仍指向旧文件名或旧目录 | 用文本编辑器打开 M3U8，核对非 `#` 行与磁盘实际文件 |
| 同名歌曲是否出现多个候选 | 检查重复导入的根目录、旧曲库和备份是否也进入了索引；只保留需要的索引入口 |
| 路径是相对还是绝对 | 默认相对歌单文件；遇到播放器差异，可以在编辑器勾“使用绝对路径”后另存再测试 |
| 目录或文件改名后 Folia 索引是否已更新 | 对实际曲库执行重新导入，再重新导入更新后的歌单 |

需要移除旧入口时，在 Folia 中移除旧的曲库索引即可；不要为了让索引清爽直接删除磁盘音乐。备份也不应作为第二个日常曲库重复导入。

## 5. 新下载一首歌以后怎么做

再次运行曲库更新入口，核对新增／失败报告；在 Folia 对该曲库执行重新导入。已有歌单只含当时选中的歌曲，不会自动吸收新增歌曲：打开歌单编辑器把新歌加入、保存，再将更新后的 M3U8 导入 Folia。

不要为了做歌单而新建一套装满歌曲副本的文件夹。音频存一份正式曲库，多个 M3U8 可以引用同一首歌。
