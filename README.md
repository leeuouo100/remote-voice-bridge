# Remote Voice Bridge（remote-voice-bridge · Windows）

把蓝牙语音遥控器（Chromecast Voice Remote / X6 等）的**麦克风语音**桥接到 Windows，
让微信输入法、豆包输入法等把遥控器当成无线麦克风使用。

**一句话**：按一下遥控器的语音键就能长篇说话，松开手也一直在听，说完再按一下就结束。

```
遥控器 ──BLE / ATVV──▶ 本程序 ──IMA-ADPCM 解码──▶ VB-CABLE ──▶ 输入法语音输入
```

> **命名对照**（仓库里出现的所有名字指的都是同一个东西，别被写法搞混）：
>
> | 场合 | 名字 |
> |---|---|
> | 仓库 / 命令行 / 配置目录 | `remote-voice-bridge`（全小写连字符） |
> | 安装包标题、窗口标题、托盘 | `Remote Voice Bridge`（首字母大写，带空格） |
> | 主程序 | `RemoteVoiceBridge.exe`（无空格） |
> | 安装包 | `RemoteVoiceBridge-Setup-<版本>.exe` |
> | 配置与日志 | `%APPDATA%\remote-voice-bridge\` |
> | 简称（日志前缀等） | `rvb` |

启动后常驻系统托盘，图标颜色反映状态：灰=未连接，暖橙=已连接，橙红=语音中。

托盘右键 → **控制台**，是一个分页窗口（对标 vRemoter 的控制台）：

| 页 | 内容 |
|---|---|
| **音频** | 实时语音波形、**分段电平表**、**三路音量（电脑麦克风 / 遥控器麦克风 / 混合输出）**、增益滑块、输出设备、输入法、**语音键自测** |
| **按键映射** | 逐个遥控器按键挑目标动作（下拉选择，也可**录制任意组合键**），改动即时生效 |
| **设置** | 上半：音频（增益、混合输出设备、电脑麦克风）· 语音（触发方式、触发键、拦截开关、开机启动）；下半：连接 · 关于（版本、配置目录） |
| **日志** | 实时日志 —— 出问题时先看这里 |

> 🚀 **第一次使用？请直接看 → [使用教程（分步图文）](docs/使用教程.md)**
> 装虚拟声卡 → 配对遥控器 → 设置麦克风 → 安装程序 → 开始说话，约 10 分钟。

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

## 更新日志

完整历史见 **[CHANGELOG.md](CHANGELOG.md)**。下面是最近一版的要点：

### v1.0.4（当前）

| | 遥控器按键 | 行为 | 转发给输入法的键 |
|---|---|---|---|
| 🎙️ | **语音键**（麦克风图标） | **按一下就开始，松手也一直听**；再按一下 / 按确认键结束；10 分钟自动收尾 | 左Ctrl+左Win+左Shift（微信输入法「启动语音输入」） |
| 🔇 | **静音键** | **按住**说话，松手结束（对讲机式） | Ctrl+Win（微信输入法「按住说话」） |

- 🔴 **修掉 v1.0.3 的断流 bug**：Windows 蓝牙回调跑在没有事件循环的线程池线程上，
  导致 `MIC_OPEN` / `MIC_CLOSE` **一次都没真正发出去**（日志还假报"已重新开麦"）。
  表现就是松手后混音显示"等待录音"、转文字一个字一个字往外冒
- 现在改用线程安全的方式回投给主事件循环，并且**发不出去就报错**，不再假成功
- 顺带把重开麦的定时器也换成线程安全实现（同一个坑）
- 语音会话进行中**不再做 GATT 读**（读会占住通道，把 `audio_stop` 通知挤丢，
  表现为「说着说着自己断了」）
- 多虚拟声卡时不再选错输出设备（`CABLE Input` 曾可能匹配到 `CABLE 2 Input`）
- 静音键连按在驱动异常时不再把 BACKSPACE 卡在按下状态
- 「确认键吞 Enter」改成可关闭：Windows 分不出遥控器和物理键盘，
  想一边说话一边敲键盘的，去「设置 → 语音」取消勾选

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

**👉 完整分步教程：[docs/使用教程.md](docs/使用教程.md)**
（VB-CABLE 安装 / 遥控器配对 / 麦克风设置 / 常见问题排查，全程约 10 分钟）

**快速开始（5 步）：**

1. 安装 [VB-CABLE](https://vb-audio.com/Cable/) 并**重启电脑**
2. 蓝牙配对遥控器（Chromecast：同时按住 **Home + Back**）
3. 把输入设备（微信麦克风）设为 **`CABLE Output`**
4. 到 [Releases](../../releases) 下载 `RemoteVoiceBridge-Setup-x.x.x.exe` 安装
5. 按遥控器任意键**唤醒** → 等 1–2 秒 → **按一下语音键**开始说话
   （**松手也在听**；说完再按一下语音键，或按确认键结束。想按住说话就用**静音键**）

**从源码运行：**

```bat
run.bat
```

或：

```bash
pip install -r requirements.txt
python tray_app.py
```

托盘右键菜单：状态 / 输入法（微信·豆包·自定义）/ 重新连接 / 开机启动 / 控制台 / 日志 / 退出。

---

## 构建安装包

### 本地构建

```bat
pip install -r requirements.txt -r requirements-build.txt
pyinstaller remote-voice-bridge.spec --noconfirm
iscc installer.iss            :: 需安装 Inno Setup 6
```

产物：`installer\RemoteVoiceBridge-Setup-1.0.4.exe`

改版本号时记得**两个文件一起改**（`config.py` 的 `APP_VERSION` 和 `installer.iss`
的 `MyAppVersion`），然后跑一次：

```bat
python tools\check_version.py     :: 应输出 OK 1.0.4
python tools\smoke_console.py     :: 应输出 SMOKE OK
```

### GitHub Actions 自动构建（推荐）

推送 tag 即自动在 `windows-latest` 上跑 PyInstaller + Inno Setup，并把
`Setup.exe` 挂到 Release：

```bash
git tag v1.0.4 && git push origin v1.0.4
```

构建前会先校验 `tag` 与 `config.py` 的 `APP_VERSION` 是否一致，不一致会直接失败。

> ⚠️ Actions 产出的 Release **默认是 Draft**，要对外可见需手动改：
> `gh release edit v1.0.4 --draft=false`

也可在 Actions 页面手动 `Run workflow`。

---

## 目录结构

```
README.md          是什么 / 怎么装 / 怎么用（先看这个）
CHANGELOG.md       每个版本改了什么、为什么改
docs/使用教程.md    分步图文教程（第一次用看这个）
LICENSE / THIRD_PARTY_NOTICES.md   许可证与第三方声明

adpcm.py        IMA-ADPCM 解码器（移植自 vRemoter）
atvv.py         ATVV 协议栈 v0.4 / v1.0
session.py      会话状态机 closed→opening→open→closing
buttons.py      按键映射分发（查 config 的 keymap）
keys.py         合成键发送（SendInput / 拦截回声 / 注入顺序）
mixer.py        三路混音：电脑麦克风 × 遥控器麦克风 → 混合输出
config.py       配置管理（输入法、映射目标表、增益等）
state.py        桥 ↔ UI 的共享状态（含实时增益与电平）
tray_app.py     托盘 GUI（打包入口）
main.py         BLE 连接 + ATVV 握手 + VB-CABLE 音频管道
console_server.py  控制台后端（本地 HTTP，只用标准库，仅监听 127.0.0.1）
ui/                控制台前端（index.html / style.css / app.js）
tools/          自检与实测脚本（每个的用途见 tools/README.md）
remote-voice-bridge.spec   PyInstaller 配置
installer.iss              Inno Setup 脚本
run.bat                    从源码启动
restart.bat                从源码重启（只结束本程序自己的进程）
```

配置文件与日志位于 `%APPDATA%\remote-voice-bridge\`。
