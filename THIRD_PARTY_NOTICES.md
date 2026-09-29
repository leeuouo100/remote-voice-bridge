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

---

## VibeMote

`frida_tap.js` 与 `frida_hid.py`（v1.0.20 起：注入蓝牙 HID 驱动宿主
`WUDFHost.exe` 读遥控器按键报告的**按键旁路**）的机制移植自 **VibeMote**：

| 本仓库文件 | 来源 |
|-----------|------|
| `frida_tap.js` | `tap.js`（Frida 钩子：`ntdll!NtDeviceIoControlFile` + IOCTL `0x80018483`） |
| `frida_hid.py` | `keys.py`（宿主定位、注入、usage 解码、降级重试的思路） |

- 项目地址：<https://github.com/Tilkmilk/vibe-mote>
- 许可证：MIT
- 版权：Copyright (c) Tilkmilk

移植时按本项目的报告格式（消费类页 16 位 usage / 厂商页 8 位 usage 两种）、
button_id 命名、日志与降级约定做了改写。**核心机制（注入 WUDFHost、
在 UMDF 复制入口抄 IOCTL 输出缓冲区、原地抹掉已映射键的 usage）来自该项目。**

## Frida

按键旁路依赖 **Frida** 做进程注入（`pip install frida`，版本锁定 `>=17.18,<18`）。

- 项目地址：<https://frida.re>
- 许可证：wxWindows Library Licence（LGPL 派生，允许动态链接使用）
- ⚠ 注入框架在部分杀软/EDR 的特征库里，可能被拦或误报；程序在注入失败时
  会优雅降级（语音不受影响，仅按键映射不可用）。
