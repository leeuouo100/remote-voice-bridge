# remote-voice-bridge（Windows）

把蓝牙语音遥控器（Chromecast Voice Remote / X6 等）的**麦克风语音**桥接到 Windows，
让微信输入法、豆包输入法等把遥控器当成无线麦克风使用。

```
遥控器 ──BLE / ATVV──▶ 本程序 ──IMA-ADPCM 解码──▶ VB-CABLE ──▶ 输入法语音输入
```

启动后常驻系统托盘，图标颜色反映状态：灰=未连接，暖橙=已连接，橙红=语音中。

托盘右键 → **控制台**，是一个分页窗口（对标 vRemoter 的控制台）：

| 页 | 内容 |
|---|---|
| **音频** | 实时语音波形、**分段电平表**、**三路音量（电脑麦克风 / 遥控器麦克风 / 混合输出）**、增益滑块、输出设备、输入法、**语音键自测** |
| **按键映射** | 逐个遥控器按键挑目标动作（下拉选择，也可**录制任意组合键**），改动即时生效 |
| **设置** | 上半：音频（增益、混合输出设备、电脑麦克风）· 语音（触发方式、触发键、拦截开关、开机启动）；下半：连接 · 关于（版本、配置目录） |
| **日志** | 实时日志 —— 出问题时先看这里 |

> 🚀 **第一次使用？请直接看 → [使用教程（分步图文）](使用教程.md)**
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

## v1.0.1：真机实测抓到的四个问题

v1.0.0 发布后在真机上跑，**遥控器这一侧完全正常**（配对、ATVV 握手、MIC_OPEN、
遥控器推流全都对），但**按着说话输入法一个字都不出**。日志把范围锁死到了音频之后：

> ⚠️ **最容易搞混的一点：微信输入法 ≠ 微信 PC 客户端。**
>
> | 产品 | 语音唤起键 | 生效范围 |
> |---|---|---|
> | **微信输入法**（本程序对接的） | **长按右 Alt** | **全局**——微信、豆包、记事本、浏览器都能用 |
> | 微信 PC 客户端 4.1.8+ | 按住 Ctrl+Win | **仅微信自己的窗口内** |
>
> 所以默认键位是 `ralt`（右 Alt），不是 `Ctrl+Win`。

| # | 问题 | 后果 | 修复 |
|---|------|------|------|
| 1 | 把「按住说话」做成了「点一下」，且键位写的是 `Alt+Shift+M` | 输入法的语音输入都是**长按说话、松开结束识别**，点一下只录到约 50ms 空气；`Alt+Shift` 还是 Windows 切换输入法的系统热键 | `keys.py` 改用 **SendInput** 的 key-down/key-up，语音开始=按住、结束=松开；`hotkey_mode` 支持 `hold`/`tap` |
| 2 | **每个采样被播放两遍**：`on_audio` 既直接塞进播放缓冲、又入队，而播放回调两条路都会取到同一批数据 | 音频变成断续、倍速的碎片，就算触发了输入法，喂给 ASR 的也是垃圾 | 删掉直接 extend，音频只走队列一条路 |
| 3 | watchdog 每 **180 秒**无条件重连：它以为「ATVV 静默 = 掉线」，但**遥控器按键走 HID 通道、不产生 ATVV 通知** | 每次重连约 4 秒内遥控器完全不可用 | ATVV 静默只当「疑似」，必须再做一次 GATT 读确认失败才断开；HID 按键也刷新活动时间 |
| 4 | 默认触发键写成了 `Ctrl+Win` | 那是**微信 PC 客户端**（4.1.8+）的语音键，**只在微信自己的窗口里生效**——在豆包、记事本、浏览器里按了毫无反应。而微信输入法 / 豆包输入法用的都是**长按右 Alt**，全局生效 | 默认改为 `ralt`。右 Alt 必须用 `KEYEVENTF_SCANCODE` 下发硬件扫描码（`0x38` + 扩展位），否则只发通用 `VK_MENU(0x12)` 会被输入法当成**左** Alt 直接忽略 |

同时补齐了**音频可见性**（此前完全没有）：控制台现在有
**实时语音波形 + 电平 + 本次帧数/峰值 + 「最后音频 N 秒前」**，
`Audio STOP` 时日志会写明本次收到多少帧。

> 这一整套的意义：把「没收到音频」和「收到了但没触发输入法」**一刀切开**，
> 不用再靠猜。控制台里出现「⚠ 本次 0 帧」就是前者。

---

## v1.0.2：补齐界面（此前是「盲操」）+ 修掉持续 0 帧

v1.0.1 装到 HP 笔记本上以后暴露了两件事：**语音依旧一帧都收不到**，
以及**整个程序没有界面** —— 只能在托盘右键里点来点去，按键映射只能改 `config.json`
文件，等于盲操。本版针对这两点。

### 一、持续 0 帧的真正原因：MIC_OPEN 从来没发出去

日志里一直是 `▶ Audio START` + `本次共收到 0 个音频帧`。对照上游客端源码后确认：

| | Chromecast 遥控器实际行为 | 本程序原来的假设 |
|---|---|---|
| 按语音键时上报的 ATVV 控制事件 | 只有 `AUDIO_START (0x04)` | 期待 `START_SEARCH (0x08)` |
| 结果 | 开麦命令 `MIC_OPEN` **一次都没发出去** | —— |

`MIC_OPEN` 原本只挂在 `start_search` 分支上，而这个遥控器**从不发** `start_search`
（上游客端 `BLEBridge.swift` 里也是同一个现象）。遥控器必须先收到 `MIC_OPEN`
才会真正上传麦克风数据 —— 所以它一帧都不推。

**修复**：`AUDIO_START` 到达时补发一次 `MIC_OPEN`（`SessionCoordinator.ensure_mic_open()`），
幂等、不重复开麦；并修正了 `mic_open_sent` 标记的生命周期 —— 它原先在会话关闭时
不复位，会导致**只有第一次能用、之后每次都 0 帧**。

> 顺带修掉一个潜伏的自激隐患：遥控器走 HID 键盘通道，与物理键盘在 Windows 上
> **无法区分**，键盘钩子既能看见用户按的键、也能看见程序自己注入的键。
> 映射目标一旦落回同一个键名（确认键 → Enter 就是典型）就会无限递归注入。
> 现在注入时会登记"这个键是我发的"，钩子识别到回声直接丢弃。

### 二、补上控制台与按键映射界面

对标 [vRemoter](https://github.com/VincentKingHsu/vRemoter) 的控制台：

- **音频页** —— **三路实时波形 + 分段电平表**（电脑麦克风 / 遥控器麦克风 / 混合输出，
  每路可静音、独奏、开关是否参与混合）、**增益滑块**（拖完立即生效，不用重启）、
  **混合输出设备下拉框**（自动列出所有输出设备，VB-CABLE 排最前）、**电脑麦克风选择**、
  输入法 / 触发方式（按住 / 点按）、**「测试语音键」按钮**（模拟长按 1 秒，
  直接验证输入法认不认这组键）
- **按键映射页** —— 每个遥控器按键一行，下拉框挑目标动作。目标集对齐 vRemoter 的
  `RemoteMappingTarget` 并换成 Windows 语义：方向上/下/左/右、回车、Esc、退格、
  Delete、Tab、空格、PgUp/PgDn、Home/End、系统音量±、静音、播放暂停、
  显示桌面、搜索、截图、任务管理器、复制/粘贴/撤销/全选…… 也可以
  **「录制任意按键…」** 自己按一个组合键进去。改动**即时保存、无需重启**。
- **设置页** —— 上半：音频（两路增益、混合输出设备、电脑麦克风）与语音
  （触发方式、触发键、拦截开关、开机启动）；下半：连接与关于（版本、配置目录）
- **日志页** —— 实时日志

> **关于「原样直通」**：遥控器走 HID 键盘通道，它自己的按键 Windows 本来就收得到。
> 所以映射目标里有一项 **`原样直通`**（默认用于方向键）＝ 不做任何额外动作。
> 改成其它动作时会「原生键 + 映射键」一起发出 —— Windows 无法单独拦掉遥控器原生键，
> 要完全接管需在「设置」页打开「拦截遥控器原生按键」（代价：物理键盘的同名键也会被吞）。

### 三、工程防呆

- 版本号在 `config.py` 与 `installer.iss` 两处，新增 `tools/check_version.py` 做一致性校验；
  CI 打 tag 时也会校验 tag 与 `APP_VERSION` 是否一致，不一致直接**构建失败**，
  避免"tag 是 1.0.3、装出来还写着 1.0.2"这种只能靠重新发版来修的事故
- 新增 `tools/smoke_console.py`：离屏把控制台真的建出来跑几轮刷新，
  控件名写错 / 变量漏定义这类问题在 CI 阶段就被拦下，而不是等用户点开才炸
- 新增 `tools/check_injection.py`：**真的把组合键按下去**，再用键盘钩子确认系统收到了。
  这是唯一能拦住「SendInput 静默失效」和「组合键顺序错」的一关 ——
  这两种故障的共同点是**日志里一切正常、输入法毫无反应**，肉眼查不出来。
  下面那个 Win 键的坑就是它抓出来的
- `tools/check_all.py` 一键跑完全部校验，输出 `ALL CHECKS PASSED` 即通过

#### 顺带查清的一个系统级坑：注入 `Ctrl+Win` 时 Win 会被换成 `0xFC`

排查「按住 `Ctrl+Win` 唤不起输入法」时发现，问题不在我们发得对不对，
而在**系统收到之后把内容改了**：

| 注入顺序 | 键盘钩子实际看到的事件 | 结果 |
|---|---|---|
| 先 Ctrl 再 Win | `vk=0xA2`、**`vk=0xFC`**（Win 被顶替掉了） | 输入法认不出 Win，组合键失效 |
| **先 Win 再 Ctrl**（现在的做法） | **`vk=0x5B` / `scan=0x5B`**、`vk=0xA2` | 钩子收到货真价实的 Win ✅ |

原因是 `Ctrl+Win` 属于 Windows 保留的**系统组合键**：Ctrl 已经按下时，
系统会把后到的 Win 改写成 `0xFC`，而且 Ctrl 与 Win **不会同时**出现在
`GetAsyncKeyState` 里。所以现在 `keys.py` 的按下顺序固定为 **Win 最先落下**
（`order_press()` 按修饰键优先级排序），`check_injection.py` 对带 Win 的组合
只认钩子事件、不拿 `GetAsyncKeyState` 当判据。

> 结论：**右 Alt 那条路干净可靠、是推荐值**；`Ctrl+Win` 现在也能发出去了，
> 但那已经是系统限制下能做到的最好结果 —— 输入法认不认，请按
> [使用教程](使用教程.md) 第六步实测一次（控制台「音频」页也有「测试语音键」按钮）。

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

**👉 完整分步教程：[使用教程.md](使用教程.md)**
（VB-CABLE 安装 / 遥控器配对 / 麦克风设置 / 常见问题排查，全程约 10 分钟）

**快速开始（5 步）：**

1. 安装 [VB-CABLE](https://vb-audio.com/Cable/) 并**重启电脑**
2. 蓝牙配对遥控器（Chromecast：同时按住 **Home + Back**）
3. 把输入设备（微信麦克风）设为 **`CABLE Output`**
4. 到 [Releases](../../releases) 下载 `RemoteVoiceBridge-Setup-x.x.x.exe` 安装
5. 按遥控器任意键**唤醒** → 等 1–2 秒 → 按**语音键**说话

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

产物：`installer\RemoteVoiceBridge-Setup-1.0.2.exe`

改版本号时记得**两个文件一起改**（`config.py` 的 `APP_VERSION` 和 `installer.iss`
的 `MyAppVersion`），然后跑一次：

```bat
python tools\check_version.py     :: 应输出 OK 1.0.2
python tools\smoke_console.py     :: 应输出 SMOKE OK
```

### GitHub Actions 自动构建（推荐）

推送 tag 即自动在 `windows-latest` 上跑 PyInstaller + Inno Setup，并把
`Setup.exe` 挂到 Release：

```bash
git tag v1.0.2 && git push origin v1.0.2
```

构建前会先校验 `tag` 与 `config.py` 的 `APP_VERSION` 是否一致，不一致会直接失败。

> ⚠️ Actions 产出的 Release **默认是 Draft**，要对外可见需手动改：
> `gh release edit v1.0.2 --draft=false`

也可在 Actions 页面手动 `Run workflow`。

---

## 目录结构

```
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
tools/
  check_all.py        一键跑完全部校验
  check_version.py    版本号一致性校验
  check_keymap.py     按键映射表校验
  check_injection.py  按键注入自检（真按下去 + 钩子确认收到）
  check_ui.js         控制台截图与波形动画校验（Playwright，可选）
  smoke_console.py    控制台离屏冒烟测试
  test_recorder.py    录制器逻辑测试
  test_voice_hotkey.py  语音快捷键实测工具（只用标准库，不用装依赖）
  serve_console.py    单独起控制台（开发调试用）
remote-voice-bridge.spec   PyInstaller 配置
installer.iss              Inno Setup 脚本
```

配置文件与日志位于 `%APPDATA%\remote-voice-bridge\`。
