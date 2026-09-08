# Third-Party Notices

## vRemoter

本项目（`remote-voice-bridge`）的以下部分移植自 **vRemoter**：

| 本仓库文件 | 来源 |
|-----------|------|
| `atvv.py`    | `Sources/vRemote/ATVV/ATVVProtocol.swift` |
| `adpcm.py`   | `Sources/vRemote/ATVV/ADPCMDecoder.swift` |
| `session.py` | `Sources/vRemote/X6SessionCoordinator.swift` |

- 项目地址：<https://github.com/VincentKingHsu/vRemoter>
- 许可证：MIT License
- 版权：Copyright (c) 2026 Sima Qingfeng

MIT 许可证允许修改与再分发，条件是保留版权声明与许可声明。完整条款见 [`LICENSE`](LICENSE)。

## 移植过程中的修改

listed in README「相对 vRemoter 修正的问题」，主要包括：

- 修正 CTL opcode：`0x08` = START_SEARCH、`0x0A` = AUDIO_SYNC
- 补全 `MIC_OPEN` 的 BLE 实际写入（原实现只构造字节）
- 修复 toggle 模式会话死锁
- 修复 `main()` 中 `cfg` 未定义导致的 NameError
- 依赖包名更正：`winrt` → `winrt-runtime`
