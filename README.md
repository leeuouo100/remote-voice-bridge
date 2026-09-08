# remote-voice-bridge（Windows）

把蓝牙语音遥控器（Chromecast Voice Remote / X6 等）的**麦克风语音**桥接到 Windows，
让微信输入法、豆包输入法等把遥控器当成无线麦克风使用。

```
遥控器 ──BLE / ATVV──▶ 本程序 ──IMA-ADPCM 解码──▶ VB-CABLE ──▶ 输入法语音输入
```

启动后常驻系统托盘，图标颜色反映状态：灰=未连接，暖橙=已连接，橙红=语音中。

---

## 移植与致谢

本项目的 **ATVV 协议栈**、`IMA-ADPCM` 解码器、会话状态机均移植自
[vRemoter](https://github.com/VincentKingHsu/vRemoter)（macOS 版，MIT License，
Copyright © 2026 Sima Qingfeng）。原项目为本仓库提供了协议与会话的全部基础，
详见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

---

## 相对 vRemoter 修正的问题

移植到 Windows 时，原实现有几个**会导致完全不工作**的问题，已在本仓库修复：

| # | 问题 | 后果 | 修复 |
|---|------|------|------|
| 1 | CTL opcode 张冠李戴：`0x08` 被当成 `AUDIO_SYNC` | `0x08` 实为 `START_SEARCH`（语音键按下），被丢弃后 **MIC_OPEN 永不被触发，遥控器不推流** | `0x08`=START_SEARCH，`0x0A`=AUDIO_SYNC |
| 2 | `MIC_OPEN` 只构造字节、从不写入 BLE | 即使触发，ATVV 握手也发不出去 | `_send_tx()` 真正写入 TX 特征 |
| 3 | toggle 模式会话死锁 | `audio_stop` 未清按住标记，**第二次按语音键再也开不了麦** | `on_audio_stop` 重置 `_held` |
| 4 | `main()` 中 `cfg` 未定义 | 首次重连即 `NameError` 崩溃 | 显式 `Config.load()` |
| 5 | 依赖版本号不存在 | `winrt==2.5.0`、`winrt-Windows.UI==10.0.22621.1` 均无法安装 | 正确包名 `winrt-runtime==3.2.1` |
| 6 | VB-CABLE 设备名文案反了 | 按说明配置永远配不通 | 见下方表格 |

> 关于 #5：PyPI 上的 `winrt` 包只有 1.0.x（2021 年停更），**没有 2.x**。
> 现代 WinRT 绑定由 `winrt-runtime` 提供 `winrt.*` 命名空间。

---

## 安装前提

1. 安装 [VB-CABLE](https://vb-audio.com/Cable/)
2. Windows 蓝牙设置里配对遥控器（Chromecast Remote：同时按住 **Home + Back** 进入配对模式）
3. ⚠️ **VB-CABLE 的命名是反的**，这是最常踩的坑：

| 设备名 | 实际身份 | 用途 |
|--------|----------|------|
| `CABLE Input`  | **播放**设备 | 本程序把语音「播放」到这里 |
| `CABLE Output` | **录音**设备 | 要收声的 App（微信等）麦克风选**这个** |

即：本程序 → `CABLE Input`；微信/输入法的麦克风 → `CABLE Output`。

---

## 使用

**方式一：直接下载安装包**
到 [Releases](../../releases) 下载 `RemoteVoiceBridge-Setup-x.x.x.exe`，双击安装。

**方式二：从源码运行**

```bat
run.bat
```

或：

```bash
pip install -r requirements.txt
python tray_app.py
```

托盘右键菜单：状态 / 输入法（微信·豆包·自定义）/ 重新连接 / 开机启动 / 控制台 / 日志 / 退出。

按遥控器语音键即开始说话。唤醒后先按方向键或 Home，等 1–2 秒再按语音键。

---

## 构建安装包

### 本地构建

```bat
pip install -r requirements.txt -r requirements-build.txt
pyinstaller remote-voice-bridge.spec --noconfirm
iscc installer.iss            :: 需安装 Inno Setup 6
```

产物：`installer\RemoteVoiceBridge-Setup-1.0.0.exe`

### GitHub Actions 自动构建（推荐）

推送 tag 即自动在 `windows-latest` 上跑 PyInstaller + Inno Setup，并把
`Setup.exe` 挂到 Release：

```bash
git tag v1.0.0 && git push origin v1.0.0
```

也可在 Actions 页面手动 `Run workflow`。

---

## 目录结构

```
adpcm.py        IMA-ADPCM 解码器（移植自 vRemoter）
atvv.py         ATVV 协议栈 v0.4 / v1.0
session.py      会话状态机 closed→opening→open→closing
buttons.py      按键映射
keys.py         合成键发送
config.py       配置管理（输入法定向等）
state.py        桥 ↔ UI 的共享状态
tray_app.py     托盘 GUI 入口（打包入口）
main.py         BLE 连接 + ATVV 握手 + VB-CABLE 音频管道
remote-voice-bridge.spec   PyInstaller 配置
installer.iss              Inno Setup 脚本
```

配置文件与日志位于 `%APPDATA%\remote-voice-bridge\`。
