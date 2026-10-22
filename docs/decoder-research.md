# REA 研究结论：缓存就绪时可去掉 Frida

当前交付状态：第18阶段已完成 6 首真实 MGG、2 首 MFLAC、2 首 NCM 与 3 种普通音频验收，并接入其他常见格式的已知离线算法。当前支持范围见 [格式支持表](format-support.md)。下文保留第16阶段的原始定位、取证和验收边界；其中“真实 NCM 未验收”、19 项回归等是当时状态，不代表当前交付。

日期：2026-10-22。研究基线 main@63f0fc6；客户端 QQMusic 21.11 x86。本轮已证明，从当前用户、本机 QQ 的持久缓存独立取得各歌曲自己的材料，再静态解码，能替代这类样本的 Frida 取材步骤。

这不是“一把密钥解所有歌曲”。此前 A 的下游材料不能直接用于 B 的结论仍有效；新路线根据 B 自己的文件标识查 B 自己的缓存 ekey。缓存中没有对应条目时，仍需准备客户端状态，不能保证全新文件首次纯离线可解。B 的未播放历史也未在本轮重新建立，不能冒称完成了原始“未播放 B”判据。

## 定位与派生链

静态分析使用 REA/Ghidra 12.1.4；对固定摘要 CommonFunction.dll 进行反编译及静态字节核对，没有执行或加载该 DLL。

| 定位（x86 首选基址 0x10000000） | 作用 | 本轮依据 |
| --- | --- | --- |
| 0x10005110、0x100076c0、0x10003510、0x10002100 | 本机硬件/网卡输入及磁盘字符串规范化 | REA 反编译、只读 Windows API 对照 |
| 0x1000b9a0、0x1000aeff | 摘要生成与 MD5 初始化 | 常量及调用链核对 |
| 0x10005fa0 | 缓存文件名派生 | 静态前后缀表及两份实际文件命中 |
| 0x10005750 | KeyDec::SetKey：base64、静态 tan 字节、交织和材料解析 | 反编译、静态浮点数据及五首真实 ekey 对照 |
| 0x1000acc5、0x1000a9aa | QQ-TEA 链式解包，16 轮、大端字序 | 反编译及独立完整音频结果 |
| 0x1000a570 | EncV2 固定包装字解码 | 静态推导；真实 EncV2 歌曲尚未验收 |

地址只适用于记录中的客户端二进制，不用作运行时注入或硬编码挂钩位置。

脱敏伪代码：

```text
identifier = read_musicex_v1_file_tail(input)
local_inputs = read_mac_and_disk_descriptors_without_process_access()
cache_key = md5_then_native_guid_text(normalize(local_inputs) + STATIC_SALT)
cache_name = native_filename_tables(cache_key)
map = parse_mmkv(aes_cfb128(cache_key_ascii_prefix, cached_iv, cached_payload))
ekey = map[identifier]
raw = strict_base64(ekey)
tea_key = interleave(STATIC_TAN_BYTES, raw.first_8_bytes)
plain = qq_tea_16_be_chained(raw.remaining_bytes, tea_key)
material = raw.first_8_bytes + plain[(plain[0] & 7) + 3 : -7]
require seven_zero_trailer and material.length == 512
audio = independent_long_material_transform(input_without_musicex_tail, material)
publish_only_after_full_pcm_and_container_validation(audio)
```

MMKV 的文件偏移、CRC、IV、CFB128 和映射结构同时对照了腾讯的 [元数据定义](https://raw.githubusercontent.com/Tencent/MMKV/master/Core/MMKVMetaInfo.hpp)、[IO 实现](https://raw.githubusercontent.com/Tencent/MMKV/master/Core/MMKV_IO.cpp)、[AES 实现](https://raw.githubusercontent.com/Tencent/MMKV/master/Core/aes/AESCrypt.cpp) 与 [映射解析](https://raw.githubusercontent.com/Tencent/MMKV/master/Core/MiniPBCoder.cpp)。开头的 size holder 可是随机占位值，不能误当作受百万字节限制的长度；实际边界由文件尺寸、varint、字段长度和 CRC 共同约束。

## 输入来源分类

| 输入 | 来源 | 意义 |
| --- | --- | --- |
| musicex 标识、封装跨度、版本 | 音乐文件自身 | 确定要查哪首歌及受支持的容器 |
| MAC、磁盘序列号/型号/固件字符串 | Windows 本机状态 | 独立打开当前机器的缓存；不读 QQ 进程 |
| 每歌 ekey、MMKV IV/CRC/内容 | QQ 当前用户的持久缓存文件 | 是逐曲输入，不能由另一个歌曲材料替代 |
| 固定字节、TEA 参数、缓存命名规则 | 固定版本二进制的程序数据 | 可静态重现，不是用户密钥 |
| ekey 首次产生或下发所需的服务端输入 | 本轮未观察网络来源 | 不声称能为缓存缺失的新歌凭空生成材料 |

QQ/账号/网络用于最初准备下载及本地缓存的角色没有被抹去。本轮消除的是脚本执行时对 Frida、qqd、QQ DLL 和客户端进程的依赖。

## 第16阶段实际验收与限制

五首真实 MFLAC 均从本机缓存解析出各自 512 字节材料并得到正确 FLAC 文件头。其中 A《孤雏》（16-bit/44.1 kHz）和 L1 中岛美嘉《僕が死のうと思ったのは》（24-bit/96 kHz）通过完整解码：完整 PCM MD5、时长、输出 SHA-256 一致；两份研究副本前后 SHA-256 未变。两首共 45.375 秒。

验证使用新建便携运行库、空逐曲材料缓存，阻止 Frida 导入、QQ DLL 加载、进程读写 API、历史截取材料及旧缓存访问，禁用项触发次数为零。输入来自当前 QQ 持久缓存与当次只读硬件枚举；没有关闭、启动或播放 QQ。未声称进行了客户端关闭后的现场实验。

真实普通 FLAC、MP3、OGG、改错后缀的普通 FLAC 完整校验与字节保持通过。WAV/NCM 的合成样本通过；NCM 结果与仓库原 Node 实现一致。损坏 FLAC 不发布输出，缺材料保持等待；当时候选对 MGG 不处理，本次修正阶段已加入通用 QMC 路线与重复尾部处理，；第18阶段六首真实 MGG 和两首真实 NCM 已完成全长验收，具体见格式支持表。缓存命名的五种后缀均有完整提供器合成回归；真实缓存只有本机命中的两份。

新目录自动配置在 PATH 无 Python/Node 的条件下通过，使用固定哈希下载包复用。本轮是同一 Windows 的隔离模拟，不是新机器或虚拟机；另一台机器、其他客户端版本、真实 NCM/EncV2、其他 QQ 封装仍在验证边界之外。详见 [使用与验收说明](agent-decoder.md) 和 [脱敏验收摘要](decoder-validation.json)。

## 第16阶段 Bridge 与审查

DSH 经 Bridge 1.0.2、DSH 0.2.1-rc.2 实现解析器和提供器；Codex 做 Windows 取证、真实样本验收和代码修正。每阶段有效预算为 32768 tokens、20 分钟、medium。固定路由为 `commandcode-goat/deepseek/deepseek-v4.1-flash`；没有响应级证据把它进一步认定为 1017 修订。

不能把 Bridge 的正常结束当成任务完成：一阶段出现空报告且无代码变化；另一次 verbose 只保留前 60 条工具观察，虽然统计有 91 次调用。补取缺失段并核对完整源码 JSON SHA-256 后才接收。最终 Windows 回归为 17 项解析/缓存测试及 2 项源变化发布测试。

审查修正了 QQ-TEA 填充区、网卡枚举的 111 返回码、缓存后缀覆盖、AES 依赖一致性、MMKV 随机占位值与发布前源变化检查。跳过项不能算成功；校验不匹配的依赖包保留并停止，不自动忽略校验。没有修改 Bridge 全局源码或上游模型配置。
