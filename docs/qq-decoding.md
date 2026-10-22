# QQ 音乐解码思路：Frida 客户端辅助路线

当前新增的独立入口为 `decode-music.cmd`：无需 Frida、qqd 或 QQ DLL；musicex 仍需对应文件的本机缓存。自动配置、手动播放准备及格式边界见 [使用说明](agent-decoder.md) 与 [支持表](format-support.md)。下文保留原有导入／客户端辅助流程的说明。

回到 [README](../README.md)。本路线仅处理使用者自己已下载到本机的歌曲文件；不代替客户端的登录、内容购买或下载流程。

我们为何选择这条路线、实际样本和成功／失败范围见[解码与整理全过程](decoding-pipeline.md)；处理后的文件如何使用，见[Folia 实例](folia-example.md)。

## 1. 为什么和 NCM 分开

普通 FLAC/OGG/MP3 可以直接验证；本仓库内置了 NCM 的离线处理实现。QQ 加密容器存在不同版本，本项目现有路线依赖 **Windows QQ 音乐客户端 + 外部 Frida 辅助工具**，不承诺任意 `.mflac/.mgg` 都能离线处理。

Frida 是动态插桩框架：它可以让工具观察运行中原生程序的函数与数据。参见 [Frida 官方介绍](https://frida.re/docs/home/)。QQ 辅助工具可以利用客户端运行时的播放/解密信息处理下载文件，这通常被概括成“客户端取钥”。

本仓库只有调用外部程序的适配层，**没有提供 QQ 的 Hook 脚本、固定地址、通用密钥算法或 QQ 辅助工具源码**。因此不能把某个特定密钥位置、数据库结构或客户端版本兼容性写成已验证事实。

## 2. 流程

1. 安装并打开与辅助工具兼容的 QQ 音乐 PC 客户端，使用自己的账号登录。
2. **实际播放一首歌曲**，让播放/解密模块加载。只打开窗口、停留在登录页或只看到进程运行，不等于这个前提已满足。
3. 导入脚本扫描配置中的本地下载文件，把待处理文件复制到临时输入目录。
4. 外部 Frida 辅助工具与客户端交互，输出候选普通音频。
5. 本仓库对输出进行完整音频解码校验，合格文件才发布到曲库；没有产物、非法容器和坏音频分别记录。

本机已有部分 MFLAC/MGG 样本成功恢复的记录，也存在独立 MGG 输出不合法而被拒绝的记录。这说明该路线有条件可用，不能据此承诺朋友电脑上的客户端、辅助工具和全部下载文件都兼容。GitHub 自动测试也没有登录 QQ 或执行这条客户端路线。

## 3. Frida、frida-tools 和 QQ 辅助程序的区别

| 名称 | 作用 | 安装后能否直接处理 QQ 文件 |
| --- | --- | --- |
| Frida | 动态插桩框架及其绑定 | 不能，仍需要适合目标程序的逻辑 |
| `frida-tools` | Frida 的通用命令行工具 | 不能，不含本项目所需 QQ 解码逻辑 |
| QQ 辅助程序 | 利用客户端运行时信息处理本地文件的具体工具 | 取决于工具实现、客户端版本、运行状态和文件变体 |
| 本仓库适配器 | 调用外部辅助程序、检查产物、整理发布 | 本身不实现 QQ 运行时 Hook |

若使用作者提供的打包 EXE，它可能自带运行依赖；若使用源码版，应按那个工具的说明安装匹配的 Python/Frida 版本。**不要为了运行打包 EXE 就盲目全局安装或更换 Frida。**

仅在学习 Frida 或工具作者要求时，可创建独立环境安装通用 CLI：

```powershell
py -3.12 -m venv .venv-frida
.\.venv-frida\Scripts\python.exe -m pip install frida-tools
.\.venv-frida\Scripts\frida.exe --version
```

这只验证通用 Frida CLI，不会获得 QQ 辅助工具或证明它与 QQ 兼容。若工具要求固定 Frida 版本，以其经过验证的依赖说明为准。官方安装说明：[Frida Installation](https://frida.re/docs/installation/)。

## 4. 当前适配器的接口

配置中的 `qqd` 是本项目使用的字段名，不是需要 `pip install qqd` 的 Python 包。

当前 `src/import-cli.mjs` 按已有 `qqmusic_des.exe` 的交互方式工作：以 `qqd.workdir` 为工作目录启动 EXE，依次向标准输入发送 `n` 和临时输入目录，从 `qqd.outputDir` 找到与暂存文件同名主体的输出，再移动到暂存区进行完整解码校验。

程序同时读取标准输出和错误输出，并设置运行超时。暂存文件会改成不冲突的名称，产物必须能与这些名称对应。若你的辅助工具有不同参数、提示顺序或输出命名，**不能只把 EXE 路径换掉就认为兼容**，需要调整适配器。

辅助工具已有输出会先归档到其输出目录旁的 `output-archive`。因此应使用专门的工具输出目录，不要把音乐收藏目录配置成 `qqd.outputDir`。

仓库没有附带或指定一个已经核验、可供所有人下载的第三方 QQ 工具发行地址；使用者需自行准备可信、兼容的辅助工具。示例 EXE 名称是接口约定，不代表仓库已经包含该文件。

## 5. 配置与启用

准备基础环境后，把 [`config.qq.example.json`](../config.qq.example.json) 作为配置样例。首次可以复制为 `config.json`；已有个人配置时，把其中的 `qqd` 字段合并进去，保留自己的下载路径。

样例中的关键字段：

```json
{
  "sourceDirs": ["./downloads"],
  "allowQqd": false,
  "qqd": {
    "exe": "./external/qqd/qqmusic_des.exe",
    "workdir": "./external/qqd",
    "outputDir": "./external/qqd/output"
  }
}
```

上面是字段摘录，完整可运行配置见样例文件。所有相对路径以配置文件所在目录为基准。`external/` 和 `.venv-frida/` 已被 Git 忽略。

确认辅助工具存在、QQ 客户端已登录且实际播放过歌曲，再显式启用：

```powershell
.\.venv\Scripts\python.exe src\refresh-library.py --config config.json --allow-qqd
# 或者在项目目录运行
.\refresh.cmd --allow-qqd
```

高层入口 `refresh-library.py` 以 `--allow-qqd` 为准：它会在本次运行的临时配置里设置 `allowQqd`，仅修改个人 JSON 里的布尔值不会替代这个开关。默认不操作 QQ 客户端。

## 6. 失败排查

| 现象 | 先检查 |
| --- | --- |
| QQ 文件显示“等待”，辅助处理未启用 | 是否提供了 `qqd` 字段，并在高层入口传入 `--allow-qqd` |
| 提示 QQ 客户端未运行 | 当前适配器查找 `QQMusic.exe`；不是安装完成就算已经运行 |
| 有进程但辅助工具没有产物 | 登录是否完成、是否真正播放过歌曲、客户端与辅助工具是否兼容 |
| 提示工具缺失 | `qqd.exe` 路径是否指向实际 EXE；工作目录是否正确 |
| 工具等待输入或超时 | 交互协议是否仍是 `n` + 输入目录；提示或输出命名是否改变 |
| 产物为非法 OGG 或完整解码失败 | 文件变体可能不兼容；不要把它改扩展名后强行加入曲库 |

首次只在专门输入目录中放少量自己下载的样本，核对报告后再考虑批量处理。记录客户端版本、辅助工具版本、Frida 版本（如工具使用外置依赖）、输入与输出哈希以及 FFmpeg 结果，便于区分环境问题与不支持的文件变体。

报告在 `.reports/import` 与 `.reports/organize`，原下载文件保持不动。
