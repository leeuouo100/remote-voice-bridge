# tools/ —— 自检与实测脚本

分两类，别混着用：

- **`check_*` / `smoke_*`** —— **自动化自检**，不需要人在旁边，CI 每次都跑。
  出问题＝代码有 bug，必须修。
- **`test_*` / `apply_*` / `serve_*`** —— **手动工具**，要人看着屏幕操作，
  或者会改动你的配置。CI **不跑**这些。

## 一键

```bat
python tools\check_all.py        :: 把下面所有 check_* 跑一遍，输出 ALL CHECKS PASSED 即通过
```

改完代码、发版前跑它。

> **看到 `[.... ] xxx：疑似机器忙，重跑一次…` 是正常的，不用管。**
> 只有 `check_injection.py` 会真的往系统里注入按键、读系统实时状态，是唯一受
> 机器负载影响的一关。`check_all` 会先等 2 秒再跑它，失败且原因是
> 「钩子没收到」时整步重跑一次。**这一条不要"优化"掉** ——
> 换成固定 sleep 会有假红，去掉重试又会让真回归藏进噪声里。

## 自动化自检（CI 也跑）

| 脚本 | 检查什么 | 为什么要它 |
|---|---|---|
| `check_version.py` | `config.py` 的 `APP_VERSION` == `installer.iss` 的 `MyAppVersion` | 防止"tag 打的是 1.0.3、装出来还写着 1.0.2"这种只能靠重新发版来修的事故 |
| `check_keymap.py` | 每个映射目标里的键名都真的能解析成扫描码 | 选项能选、按下去没反应，而且全程不报错 —— 这类故障肉眼查不出来 |
| `check_injection.py` | **真的把组合键按下去**，再用键盘钩子确认系统收到了 | 唯一能拦住「SendInput 静默失效」和「组合键顺序错」的一关 |
| `smoke_console.py` | 离屏把控制台真的建出来跑几轮刷新 | 控件名写错、变量漏定义，在 CI 阶段就被拦下，而不是等用户点开才炸 |
| `check_ble_callback_thread.py` | 在没有 asyncio 事件循环的 `Dummy-XXXX` 线程里跑完整语音链路 | v1.0.3 的真机事故：BLE 回调线程里 `get_event_loop()` 直接抛异常，`MIC_OPEN` 一次都没发出去，日志还假报成功。纯标准库、不需要真机，所以放进 CI 当回归闸 |
| `check_packaging.py` | `remote-voice-bridge.spec` 里有诊断 EXE 入口、`installer.iss` 里名字与它一致并挂进了开始菜单 | v1.0.5 漏了这一步：诊断工具写好了、版也发了，却没打进 `Setup.exe` —— 安装版用户机器上没有 Python，仓库里那个 `.bat` 对他们等于不存在 |
| `check_levels.py` | 三路电平/波形是否"有消费者、也有生产者" | v1.0.7 真机：「遥控器麦克风」波形在动、状态却永远卡在「等待语音」—— 根因是**没有任何产品代码喂** `state.remote_level_db`（只有演示脚本喂过） |
| `check_mix_persist.py` | 音频页的开关**落盘**了没有 | 「界面上关了、后端还在用」：`/api/mix` 只改内存不写 `config.json`，设置页一动就被悄悄回滚。沙箱 APPDATA，不碰真配置 |
| `check_hidinfo.py` | 遥控器的 HID 判定链：厂商自定义页（`0xFF00+`）Windows 不处理、键盘页会被处理、设备路径与 `cbSize` 偏移无关 | v1.0.8 查到：遥控器暴露**两个厂商自定义集合**（`0xFF01`/`0xFF80`，各 21 字节输入报告），Windows 对它们不做任何事。按键若发在那里，改映射表**永远没用** —— 判定改错，报告就会把用户指向错误的修法 |
| `check_pairing.py` | 蓝牙配对判定链：6 种状态（真机形态必须判成 `STALE_ADDR`、`migrate` 之后的形态必须是 `NODE_PHANTOM`）；分支顺序（`STALE_ADDR` 要排在 `NODE_PHANTOM` 前面）；「幽灵」口径；PnP 实例 ID 带 `USB\` 前缀；搬家保留注册表值类型；「删不动」只在管理员下下结论；ACL 接管开一组特权 + 循环到不动点；提权带全部开关。含**行为级反例**（把 `classify` 改坏 exec 起来必须判错） | v1.0.10 的真机事故：「换过 USB 口就连不上」—— 程序显示未连接、Windows 显示已配对、设置里还删不掉。判定读的是真机注册表，CI 里没有蓝牙棒，所以抽成纯函数 + 假数据锁死 |
| `check_failure_visibility.py` | 故障必须「看得见、说人话」：桥线程异常走 logging（不是 `print`）、连接失败必须给出原因+处置且不只试一条路、托盘必须能说出具体原因、`_open_ble_device` 必须真被 `run_bridge` 调用 | v1.0.9 真机：桥每 3 秒崩一次却**一行报错都没有** —— 因为 `tray_app` 用 `print` 报异常，而主 exe 是 `console=False`（输出流向是空的）；托盘还一直写着「按遥控器任意键唤醒」，把 `OSError: E_INVALIDARG` 捂了两小时。这类「静默 + 误导」是这个项目最反复的一类 bug，所以用反例锁死 |
| `check_remote_hid.py` | 厂商页按键解码：解码表完整（int-usage 的键一个不少、且**不含**语音键）、17 条报告格式、按下→松手配对、遥控器集合能不能打开；**审计日志限流**（用假时钟跑 30 次：既"报过"又"没刷屏"，含 2 条反例） | v1.0.11 起按键映射全靠厂商页，解码表错一位就是「键全串位」；**不用按遥控器**就能验，真机上按一次键再对日志 | 
| `check_audio_watchdog.py` | 音频输出流停摆必须**可见且能自愈**：回调用 `_cb_last_at` 报心跳、心跳写在混合逻辑之前、主循环每轮调 `_supervise_audio`、超时走 `_rebuild_audio`、重建前清积压、混音回调**无论增益是否为 0 都按一比一消费队列**、队列满不再只打 debug。含「不许退回老写法」的反例断言 | v1.0.12 真机：语音输入开着、波形在动，输入法却收不到声音，重启才好。根因是输出流**建一次就没人管** + 静音期间不消费队列导致 `queue.Full` 静默丢帧。纯静态，几毫秒 |
| `check_send_after_voice.py` | 语音说完之后**什么时候发由用户决定**：`send_after_voice` **默认必须是关**（2026-09-17 武哥否掉了"默认开"——会自动把用户没想好的话发出去）、`_migrate` 把旧配置归位成关、`CONFIG_VERSION` 同步、呆瓜配置里也不许打开、读不到配置时兜底是**不发**；开关打开后：延迟 ≥300ms（太小会在文字落进输入框**之前**就回车，等于把刚说的话弄丢一次）、4 条收尾路径各自接对（确认键防连发两下、**超时收尾故意不发**）、开始新一段要取消待发送、零音频帧不发送（误触保护）、帧计数**不在** `audio_start` 顶部清零。含 **9 项行为级**（真 import main + 假时钟 + 假 tap_key，第一条就是「默认配置下到点了也绝不发」）+ **14 条反例自证** | v1.0.13：用户既要"不用去够鼠标"，也要"什么时候发我说了算"——这两件事的平衡点就是**默认不发 + 按键盘回车**。遥控器除语音键以外的键在 Windows 上收不到报告，所以绑按键这条路暂时走不通 |
| `check_takeover_guard.py` | `takeover_hid_reports.py` 是这个项目里**唯一会改系统设备状态**的东西，这道闸钉它三条：**① 默认只读**（`--go` 是 `store_true`，不带就 `return`，而且"不 go"的判断必须在 `Popen` 之前）；**② 目标必须是 `BTHLEDEVICE` 父节点**（`0x1812` 底下是**一父 + 5 个 HID 子集合**，挑到 `COLxx` 空集合就是白测一场 + 错判「软件到头了」）、找不到就 `return 3` 不退而求其次；**③ 禁用与恢复在同一条 PowerShell 命令里**、`Start-Sleep` 夹中间、启用失败输出 `ENABLE_FAIL`、用 `Popen`（用 `run` 会把监听时间睡掉）、收尾回读状态不是 `OK` 就喊人。含 **2 条反例自证**（改回 `Get-PnpDevice \| Select-Object -First 1` 必须报红） | 2026-09-22 真踩过：第一版就是 `Select-Object -First 1`，拿到的是 `COL05`（无服务的空集合）—— 照那样跑会禁错节点、一条报告也收不到，然后拿这个**假阴性**去关掉最后一条纯软件的路。⚠ 这道闸自己第一版也栽在同一个坑上：判据用字面量匹配，而 `BTHLEDEVICE` / `Disable-PnpDevice` 恰好都写在**注释和说明文字**里 → 加 `_ps_code()` / `_code_only()` 剥掉注释与 `say(...)` 再判。不写进闸里，"顺手把 `--go` 去掉方便点"就会让体检动手改设备 |
| `check_voice_session.py` | 语音会话状态机**不许自己把自己关掉**：① 防自激窗口内要能吞下**多个**回响（不许"挡一个就清零 `mic_reopen_at`"——那样第 2 个回响会落进结束分支）② 吞掉的事件必须记数、不许静默 ③ 松手（`audio_stop`）不许出现任何结束语义（没有 `voice_hotkey_up()`、没有 `voice_active = False`）、只结算统计＋补开麦＋置 `stream_active` ④ 帧计数不在 `audio_start` 顶部清零 ⑤ 结束分支接了发送登记。含 **2 条反例自证** | 2026-09-22 真机：22:00 之后 10 次会话里 **4 次在 1.2~1.6 秒内被结束**。日志里两个 `audio_start` 只隔 19ms，第一个被"防自激"吃掉并**清零**，第二个就当成"用户第二次按下" → 注入热键把输入法语音输入关掉 + 结束会话。用户看到的是「按一下、刚开口，一个字都出不来」。代价方向：少吞一个真按键只是"多听一会儿"，多吞一个回响却是"用户当场用不了"——所以窗口内全部吞掉 |
| `check_ui.js` | 控制台截图 + 波形动画（需 Playwright，可选） | 前端改挂了不至于没人发现 |

> `check_keymap.py` 会跳过 `config.VIRTUAL_TARGETS` 里的虚拟目标
> （禁用 / 原样直通 / 语音键 / 按住说话）—— 它们不是组合键，解析必然失败。
> **新增虚拟目标时记得同步登记到 `VIRTUAL_TARGETS`**，否则 CI 会红。

## 手动工具（要人看着屏幕）

| 脚本 | 什么时候用 | 怎么用 |
|---|---|---|
| `test_voice_hotkey.py` | 拿不准输入法认不认这组语音键 | `python tools\test_voice_hotkey.py ctrl win`（5 秒内切到记事本，它按住 6 秒再松开）；加 `--tap` / `-t` 测"按一下开始、再按一下结束" |
| `test_recorder.py` | 改了按键录制逻辑之后 | `python tools\test_recorder.py` |
| `apply_voice_mode.py` | 想一键配好语音（或退回按住模式） | `python tools\apply_voice_mode.py`（推荐配置）/ `--show`（只看）/ `--hold`（退回按住说话） |
| `serve_console.py` | 单独起控制台做前端调试 | `python tools\serve_console.py` |
| `diag_remote.py` | **遥控器/按键出任何问题，先跑它**。让程序把现象测出来，而不是靠人描述 | **安装版**从开始菜单打开**「遥控器诊断」**（就是安装目录里的 `RemoteVoiceBridgeDiag.exe`）；**源码版**双击仓库根目录的 **`diag-remote.bat`**（或 `python tools\diag_remote.py`）。会分 7 段引导你按遥控器和物理键盘，约 90 秒，产出 `%APPDATA%\remote-voice-bridge\remote-diag.txt`。报告开头是【结论 0】硬件身份（**不用按键**）、末尾是【结论 4】按键落点（各 HID 集合收到了多少条原始报告） |
| `diag_remote.py --hid` | 「蓝牙显示连好了，但按键没反应」—— 先跑这个，**2 秒出结果** | `python tools\diag_remote.py --hid`（安装版：`RemoteVoiceBridgeDiag.exe --hid`）。**不用按任何键、也不用先退出桥接程序**。直接报出遥控器的 5 个 HID 集合各自"是什么、Windows 会不会理它"，以及键位映射配置的关键项 |
| `pairing.py` | 「换过 USB 口之后程序显示未连接，Windows 却显示已配对」——**先跑这个** | **安装版**开始菜单 → **「修复蓝牙配对」**（等价于安装目录里的 `修复蓝牙配对.bat`，也等价于 `RemoteVoiceBridgeDiag.exe --fix-pairing`）；**源码版**双击 `修复蓝牙配对.bat` 或 `python pairing.py --fix-pairing`。⚠ **不带 `--fix-pairing` 就是纯只读诊断**（不需要管理员）。修复会弹一次 UAC，动注册表前自动备份到 `%APPDATA%\remote-voice-bridge\backup\`。可用 `--method restore\|migrate\|purge` 指定方案，`purge` 需 `--yes`。报告：`pairing-fix.txt` |
| `check_remote_hid.py` | 「按键没反应」先跑它 —— **不用按键**就能验厂商页解码；`--watch N` 再实时听 N 秒 | `python tools\check_remote_hid.py`（纯单测）/ `--list`（列集合）/ `--watch 20`（监听 20 秒，期间按遥控器） |
| `watch_reports.py` | 想知道**某个键的报告到底落在哪一路**（键盘 / 鼠标 / 厂商自定义页） | `python tools\watch_reports.py`（默认听 40 秒，`--seconds 60` 改时长，`--all` 连非 Google 的集合一起看）。⚠ **跑之前先退出桥接程序**（托盘右键 → 退出），否则它会把遥控器原生按键吞掉、报告显示"一条都没收到"。产出 `%APPDATA%\remote-voice-bridge\remote-hidwatch.txt`（明细里每个键盘事件都带 `[真实]` / `[注入]` / `[分不出]` 标记） |
| `watch_all_channels.py` | **「按键到底走哪条通道」的终极一问** —— HID 层看只有 0 条时，用它把**所有可能的路**一次全挂上 | **源码版直接双击仓库根目录的 `测遥控器按键通道.bat`**（顺带替你处理"桥程序还开着"这件事），或 `python tools\watch_all_channels.py --seconds 45`。⚠ 先退出桥接程序（它占着 ATVV，不关就听不到 CTL，GATT 那一半也会连接失败）；实在要带着它测加 `--force`。同时监听：**键盘钩子（真实 / 注入分开数）** / 5 路 HID 集合 / 私有服务 `AE42` / 私有服务 `D343BFC5` / **ATVV CTL 的每一个字节**（认不出的 opcode 会标【认不出】）。**不用按键也能跑**（静态部分照出）。产出 `%APPDATA%\remote-voice-bridge\watch-all-channels.txt`。⚠ 判读**只看「真实（非注入）」那一行**：本程序每按一次语音键就会注入 `lctrl+lwin+lshift`，那些事件以前被当成了遥控器的按键（见 CHANGELOG v1.0.13 §六）。「订上了但 0 条」的私有服务也会进表 —— 别再分不清"是 0"和"没测" |
| `probe_gatt_hid.py` | 绕开 Windows HID 栈，**直接问蓝牙**：遥控器暴露了哪些服务/特征 | `python tools\probe_gatt_hid.py` / 加 `--listen 40` 订阅并实时听。**不用退出桥程序**。产出 `gatt-probe.txt` |
| `probe_hid_claim.py` | HID 服务被 `ACCESS_DENIED` 挡住时，换三个入口（按 UUID 直接取 / `from_id_async` 重开 / 写 HID Control Point）能不能"要"过来 | `python tools\probe_hid_claim.py` |
| `dump_report_descriptor.py` | 想看遥控器各集合的 HID 报告描述符（**设备自报的权威**） | `python tools\dump_report_descriptor.py`。⚠ 顺带记下一个坑：`IOCTL 0x000B0193` 返回的是 `HidP KDR` 结构（缓冲区开头就是 ASCII `HidPKDR`），**不是**原始描述符 |
| `takeover_hid_reports.py` | **纯软件的最后一条路** —— 把 Windows 那个 HOGP 节点临时停掉，让 `0x1812` 空出来，我们自己订阅 Report(`0x2A4D`) 看遥控器**到底发不发按键报告** | **默认只读**：`python tools\takeover_hid_reports.py`（只列 6 个节点 + 打印"如果执行会动什么"，一个字节都不改）。真做：`python tools\takeover_hid_reports.py --go --seconds 25`（**要管理员**，建议先退出桥程序）。⚠ 只禁 `BTHLEDEVICE\…mshidumdf` 那一个父节点 —— 禁 `HID\…&COLxx` 子集合没用（会话还在父节点手里，收不到报告会错判成"遥控器不发按键"）。恢复写在**同一条** PowerShell 命令里（禁用→睡→启用），Python 这边崩了/Ctrl+C 也会回来，收尾还会回读状态确认。产出 `%APPDATA%\remote-voice-bridge\takeover-hid-reports.txt`。判读是二值的：**收到报告** ⇒ 按键走 `0x1812`，本项目可以纯软件解码；**订上了但 0 条** ⇒ 遥控器确实不发按键数据，软件层没有可做的事了 |
| `probe_devnode_binding.py` | 想知道遥控器每个 devnode **绑没绑驱动**、有没有 `Problem` | `python tools\probe_devnode_binding.py`。⚠ 注意判读口径：HIDClass 子集合（消费类/厂商页）的 `Service=(未绑定)` 是**正常**的（Driver 指向 HIDClass 类 GUID `{745a17a0-…}`）；HID 服务的驱动是 `mshidumdf`（Windows 的 HOGP 驱动） |

> `diag_remote.py` 的**真机部分**必须有人按键，没法自动化；但它的「报告生成器」
> 是纯函数式的，`check_all` 会用假数据把两条分支（能区分设备 / 不能区分）都跑一遍 ——
> 否则那段代码第一次运行就是在用户机器上。跑法：`python tools\diag_remote.py --selftest`。

## 公共小工具

`_utf8.py` —— 钉住控制台编码。Windows 控制台默认 cp936/GBK，
脚本里只要有中文输出，在 CI（或某些机器）上就会 `UnicodeEncodeError` 直接崩。
**每个会打印中文的脚本都要在最开头调用它**，别裸 `print("中文")`。
