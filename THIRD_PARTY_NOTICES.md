# Third-Party Notices

本文件列出 `remote-voice-bridge` 用到的第三方代码与二进制。
安装包会把它和 [`LICENSE`](LICENSE) 一起装进 `{app}\licenses\`。

> ⚠ **新增依赖必须同时更新这里** —— `tools/check_licenses.py` 会拿
> `requirements.txt` 逐项比对，漏一项就报红。

---

## 一、运行时分发的 Python 包

这些包会被 PyInstaller 打进 `RemoteVoiceBridge.exe`，**随安装包分发**：

| 包 | 版本 | 许可证 | 上游 |
|---|---|---|---|
| `winrt-runtime` | 3.2.1 | MIT | <https://github.com/pywinrt/pywinrt> |
| `winrt-Windows.UI` | 3.2.1 | MIT | 同上 |
| `winrt-Windows.Devices.Bluetooth` | 3.2.1 | MIT | 同上 |
| `winrt-Windows.Devices.Bluetooth.GenericAttributeProfile` | 3.2.1 | MIT | 同上 |
| `winrt-Windows.Devices.Enumeration` | 3.2.1 | MIT | 同上 |
| `winrt-Windows.Devices.Radios` | 3.2.1 | MIT | 同上（仅 `tools\radio_cycle.py` 用） |
| `winrt-Windows.Foundation` | 3.2.1 | MIT | 同上 |
| `winrt-Windows.Foundation.Collections` | 3.2.1 | MIT | 同上 |
| `winrt-Windows.Storage.Streams` | 3.2.1 | MIT | 同上 |
| `typing_extensions` | ≥4.12.2 | PSF-2.0 | <https://github.com/python/typing_extensions>（`winrt-runtime` 的依赖） |
| `keyboard` | 0.13.5 | MIT | <https://github.com/boppreh/keyboard> |
| `frida` | ≥17.18,<18 | wxWindows Library Licence 3.1 | <https://frida.re> |
| `sounddevice` | 0.4.6 | MIT | <https://github.com/spatialaudio/python-sounddevice> |
| `numpy` | 1.24.3 | BSD-3-Clause | <https://numpy.org> |
| `pystray` | 0.19.5 | LGPL-3.0 | <https://github.com/moses-palmer/pystray> |
| `Pillow` | 12.3.0 | MIT-CMU | <https://python-pillow.org> |

许可证原文随各包的 `dist-info` 一起分发在 exe 内（PyInstaller 会收集
`*.dist-info` 里的许可证文件）；本表是给人看的索引。

### `frida`（wxWindows Library Licence 3.1）

按键旁路依赖 Frida 做进程注入。wxWindows 许可证是 LGPL 的派生版，
**允许动态链接使用**。

> ⚠ 注入框架在部分杀软 / EDR 的特征库里，可能被拦或误报。程序在注入失败时
> **优雅降级**：语音完全不受影响，只是方向键 / OK / 音量这些映射不了。

### `pystray`（LGPL-3.0）

只用于托盘图标。LGPL 的要求是"允许替换该库"—— 本项目是**未修改地**
以独立包形式使用它，且用户可以自行用同版本替换后重新打包（源码与
`requirements.txt` 都在仓库里）。

---

## 二、移植来源（源码级）

### vRemoter

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

#### 移植过程中的修改

listed in README「相对 vRemoter 修正的问题」，主要包括：

- 修正 CTL opcode：`0x08` = START_SEARCH、`0x0A` = AUDIO_SYNC
- 补全 `MIC_OPEN` 的 BLE 实际写入（原实现只构造字节）
- 修复 toggle 模式会话死锁
- 修复 `main()` 中 `cfg` 未定义导致的 NameError
- 依赖包名更正：`winrt` → `winrt-runtime`

### VibeMote

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

---

## 三、外部前提（**不**随本包分发）

| 组件 | 用途 | 许可 / 说明 |
|---|---|---|
| [VB-CABLE](https://vb-audio.com/Cable/) | 虚拟声卡：本程序把语音写进 `CABLE Input`，输入法从 `CABLE Output` 读 | VB-Audio 的 **Donationware**，由用户自行安装；本仓库不分发它的任何文件 |
| [Inno Setup](https://jrsoftware.org/isinfo.php) | 构建安装包（仅构建期） | 自由使用（含商业用途） |
| `PyInstaller` 6.22.2 | 打包成 exe（仅构建期） | GPL-2.0-or-later **附带特殊例外**：明确允许用它构建并分发非自由程序。见 `requirements-build.txt` 与 <https://pyinstaller.org> |
| 遥控器固件 / Google Home 应用 | 设备侧 | 与本项目无关 |

---

## 四、维护约定

1. `requirements.txt` / `requirements-build.txt` 里**每加一个包**，
   都要在本文件第一节（运行时）或第三节（构建期）补一行。
2. 版本号要写实际钉的那个；用区间（如 `frida>=17.18,<18`）就照写。
3. `tools/check_licenses.py` 会做这件事的机械校验（含反例自证）。
