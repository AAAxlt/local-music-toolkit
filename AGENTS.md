# Agent 使用入口

本仓库的新解码入口是 `decode-music.cmd`。在 Windows x64 上，先运行 `bootstrap-decoder.cmd`，再运行 `bootstrap-decoder.cmd --doctor`。首次配置只向项目内 `.runtime` 下载固定版本并核对 SHA-256；不要求已有 Python、Node、Go、Frida、REA 或 Bridge，也不改全局 PATH、执行策略和安全设置。Bridge 是开发协作工具，不是成品运行依赖。

处理用户指定的音乐文件或文件夹：

```cmd
decode-music.cmd "输入目录" --output "独立输出目录" --report "输入目录之外的新报告.json"
```

输入和正式曲库只读。输出、报告和私有缓存不得覆盖原件或既有不同内容，不得放进输入目录。默认按真实文件内容识别普通音频、NCM 与 QMC（MFLAC/MGG 的 static/map/RC4、musicex v1）。MGG 不再按后缀默认跳过，而是走通用 QMC 路线；普通音频只校验和复制，不读取 QQ 缓存。

MFLAC/MGG 的 musicex v1 默认从当前用户的 QQ 本地缓存读取各文件自己的材料，不附加进程，不加载 QQ DLL，不自动退回 Frida。缺少材料时必须如实返回 `waiting/local-material-missing`；可以提醒用户从桌面打开 QQ、登录并手动播放对应的下载曲目后重跑，不能声称任意新文件均可离线解码。

手动准备应引导用户在 QQ 的下载/本地列表里播放报告对应的已下载原件，确认声音和进度后重跑；不要把在线播放同名其他版本当作同一文件。报告路径需换新文件名，具体步骤见 `docs/agent-decoder.md`。MGG 走同一 QMC/逐曲材料边界，来源与音质说明见 `docs/mgg-origin.md`。

验收应检查退出码、每个文件的状态、完整 FFmpeg 校验及原件前后哈希。FLAC 必须通过 STREAMINFO 声明的完整 PCM MD5，不能只看“能播放”或 FFmpeg 退出码。失败、等待与跳过不能计入成功。本轮 13 个真实输入、10 个独立合成全长容器、51 项 Windows 回归已通过；其他平台真实下载样本、KGM v5、STag 和跨机器仍有边界。具体见 `docs/agent-decoder.md`、`docs/format-support.md`、`docs/decoder-validation.json`。

禁止把 `.runtime`、`.cache`、`.work`、歌曲、客户端二进制、MMKV、原始 ekey、机器标识、逐曲材料、个人配置放进提交或上传包。用户的音乐、`src/playlist-editor.py` 和 `DESIGN.md` 不得为解码任务重写或清理。旧的客户端辅助代码属于单独流程，不是新入口的自动兜底；本次授权标志和许可确认要求继续适用。
