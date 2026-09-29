# tools/ —— 自检与实测脚本

分三类，别混着用：

- **`check_*` / `smoke_*`** —— **自动化自检**，不需要人在旁边，CI 每次都跑。
  出问题＝代码有 bug，必须修。
- **`test_*` / `apply_*` / `serve_*`** —— **手动工具**，要人看着屏幕操作，
  或者会改动你的配置。CI **不跑**这些。
- **`probe_*`** —— **现场探针**，要**人在遥控器上按键**才出结论。
  CI **绝对不能**跑（会把构建挂在一件需要人手的事上），
  每个探针自带 `--selftest` 先证明"判读逻辑可信"，再去现场采数据。

## 一键

```bat
python tools\check_all.py        :: 本地全量（含真实按键注入），输出 ALL CHECKS PASSED 即通过
python tools\check_all.py --ci   :: CI 安全集：摘掉需要真机/交互桌面的那几道，其余全部必跑
```

改完代码、发版前跑它。

**`--ci` 与本地全量的唯一区别**：把标了 `hardware: True` 的闸**显式摘掉**
（目前只有 `check_injection.py` —— 它真的往系统里注入按键，需要交互桌面会话），
并在结尾**点名报出没跑的是哪几道**（"没跑"不许被读成"过了"）。

> ⚠ **"控制台 XSS 真实渲染"那道要自己找得到 playwright。** Node 的 `require`
> 解析不到 npm 的全局目录，只能靠 `NODE_PATH`；而**全局目录不是固定的**
> —— 本机是 `%APPDATA%\npm\node_modules`，GitHub 的 windows runner 是
> `C:\npm\prefix\node_modules`。写死会让这道闸在 CI 上打 `SKIPPED`
> ⇒ 被「必需项不许跳过」判 FAIL ⇒ **整轮构建挂掉**（v1.0.23 的第一次 CI
> 就是这样红的）。现在先问 `npm root -g`，再退回几个已知位置，
> 并**优先选目录里真有 `playwright` 的那个**（PATH 上第一个存在的
> `node_modules` 可能是个空壳 —— 本机那个托管的 node 就是）。

> ⚠ **必需项 SKIP 一律算失败。** 一条闸打印 `SKIPPED` 却退出码 0，以前会被当成
> "过了" —— 于是环境一坏，整道闸就静默变成**永远绿的摆设**。
> 现在只有显式标了 `required: False` 的闸才允许 SKIP。
> 这条规则由 `check_ci_workflow.py` 行为级钉住（拿假 STEPS + 假 `_run_step`
> 真跑 `main()`，含"摘掉这条规则就变绿"的反例）。

> **本表只列"踩过坑、值得说清为什么"的那几道，不是全部闸门清单。**
> **唯一权威清单是 `check_all.py` 顶部的 `STEPS`** —— CI 跑的是同一份
> （`.github/workflows/ci.yml` 的 checks job 就一句 `python tools/check_all.py --ci`）。
> 以前 CI 是"手工挑十来道列一遍"，和本地这份各自漂，结果"本地绿、CI 不跑"的
> 东西发出去没人验 —— 那是审查报告 P1-10 的洞，现在只有一份清单。

> **看到 `[.... ] xxx：疑似机器忙，重跑一次…` 是正常的，不用管。**
> 只有 `check_injection.py` 会真的往系统里注入按键、读系统实时状态，是唯一受
> 机器负载影响的一关。`check_all` 会先等 2 秒再跑它，失败且原因是
> 「钩子没收到」时整步重跑一次。**这一条不要"优化"掉** ——
> 换成固定 sleep 会有假红，去掉重试又会让真回归藏进噪声里。

## 自动化自检（CI 也跑）

| 脚本 | 检查什么 | 为什么要它 |
|---|---|---|
| `check_version.py` | `config.py` 的 `APP_VERSION` == `installer.iss` 的 `MyAppVersion` | 防止"tag 打的是 1.0.3、装出来还写着 1.0.2"这种只能靠重新发版来修的事故 |
| `check_ci_workflow.py` | CI 工作流与"项目声称的闸门"是否一致：唯一一份工作流、PR/main/tag 都触发、只跑 `check_all.py --ci`、build 依赖 checks、release **只下载 artifact 不重新构建**、`draft: true` 显式、权限最小化；外加 `--ci` 的判定机制本身（必需项 SKIP 算失败）；**外加供应链 D 组（P2-7）**：每个 `uses:` 都钉 40 位 commit SHA + 行尾版本注释、装依赖走带哈希的锁文件、有 SBOM（生成 + 真解析 + 上传）、有构建证明（`attest-build-provenance` + `id-token`/`attestations` 权限 + `subject-path`）、Authenticode 是"有条件执行 + 没证书时**明确说未签名**"。含 **20 条反例自证** | 老工作流只在 tag 触发（PR 不跑）、闸门手工列（和本地各自漂）、构建与发布同一个 job（发出去的包是**又构建一次**的，不是验过的那个文件）、没写 `draft` 靠 Action 默认值 —— v1.0.21 事实上就是**直接公开**发布的，而 README 声称默认 Draft。⚠ **D 组治的是另一类**：`@v4` 是**可变引用**，上游一指，我们下次构建跑的就是没审过的新代码，而这件事在 diff 里**看不见**（本仓库一个字节都没变）；签名那条判据第一版只在整份文件里搜「未签名」，结果**文件头说明注释**里也有这个词 —— 把摘要那行改成含糊的「已跳过」照样判合格（反例当场报绿），所以改成盯**真的会被看到的那一行** |
| `check_teardown_scope.py` | `run_bridge` 的**收尾范围**：薄壳结构（`try/finally` 里调 `teardown`）、`_run_bridge_inner` 里没有第二套收尾、`_BridgeResources` 声明的每个句柄都**有登记点**、`teardown` 逐个 None 判断且**覆盖到每一个**、句柄全 None 时跑完不抛；**F 组（2026-09-29 新增）**：`teardown` 引用的**全局名**在模块顶层必须真的有定义。含 **5 条反例自证**（其中一条是**拿 main.py 真实源码**把 `self.end_voice_session` 改回裸名） | P1-3 拆壳时埋了一个**真 NameError**：`teardown`（模块级类的方法）调用了 `_run_bridge_inner` 里的**局部函数** `end_voice_session` —— 在类方法里它被解析成**全局名**，而模块顶层没有这个名字 ⇒ 运行期 NameError ⇒ 被 teardown 自己那句 `except Exception: logger.warning(...)` 吞掉 ⇒ **退出/断连那条收尾整条没跑**（相位、UI 状态、待发送登记都没归位）。⚠ 为什么 A~E 全绿却漏了它：动态探针用的是"句柄全为 None"的场景，而那句调用正好包在 `if self.atvv is not None and self.coord is not None:` 里 —— 两个都是 None ⇒ **那一行根本不执行** ⇒ 探不到。**判据得能走到那一行** |
| `check_pairing_backup.py` | 配对修复的备份/还原是**可信事务**（P1-7）：备份落在 `%ProgramData%`（不是用户可写的 `%APPDATA%`）且建目录时**禁用继承**收紧 DACL；只导**最小子树**（`Keys\<本地地址>` + 目标设备的 `Devices\<remote>`，不再整棵抄 `Keys`/`Devices`）；关键项导出失败 → `backup()` 返回空列表并**在动注册表之前停下**；还原**只认 manifest**（逐文件 SHA-256 校验 + `.reg` 里的键路径必须落在白名单前缀内 + 不存在的键才跳过、**打不开的照样导**）。含 **15 条反例自证**，其中「键状态」全程用 `state_of=` 注入 | ⚠ 这道闸**自己**踩过一次"判据依赖跑闸那台机器"：`_backup_jobs` 原本直接读真注册表判"这个键在不在"，而 CI 的 runner 上**没有蓝牙适配器** —— `BTHPORT\Parameters\Keys` / `Devices` 两棵树都不存在，于是每条都被判成 `absent`、清单恒为空，**D8c/D8d 在 runner 上必红、在开发机上必绿**（v1.0.23 的第一次 CI 就栽在这）。修法：`_backup_jobs(d, state_of=...)` 留注入点，闸门传假状态；另加 A7b 钉住**默认值必须仍读真注册表**（不然生产路径也变成"离线的"，用户机器上"明确不存在的键"会被列进清单、导出必失败、备份永远中止）。同一类坑 `check_pairing.py` 早踩过一次（"判定读真机注册表，CI 里没有蓝牙棒"）—— **闸门本身必须是离线的** |
| `check_run_bat.py` | 源码用户唯一会双击的入口：**最后启动的是 `tray_app.py`**（不是 `main.py`）、系统默认麦克风写的是 `CABLE Output`（不是 Input）、pip 输出落盘且失败即停。含 **5 条反例自证** | 三处都**不报错**：走 `main.py` 没有托盘、只能关窗口硬杀进程（P1-3/P1-4 做的优雅收尾根本到不了）；麦克风写成 `CABLE Input` 就是读"没人写的那只端点"，症状是一点声音都没有；pip 输出 `>nul` 全吞 + 不看退出码 ⇒ 依赖没装上也照样往下跑，最后崩在一个和"依赖没装"毫无关系的 ImportError 上 |
| `check_licenses.py` | `THIRD_PARTY_NOTICES.md` 覆盖 `requirements*.txt` 里的**每一个**包、每个都写了许可证名、且 `installer.iss` 真的把 `LICENSE` + NOTICES 装进 `{app}\licenses\`。含 **4 条反例自证** | 安装包里分发着 15 个第三方包，其中 `pystray` 是 **LGPL-3.0**、`frida` 是 **wxWindows**（LGPL 派生）—— 都要求随分发附上许可证；`PyInstaller` 的 GPL 特殊例外也得写出来。而 NOTICES 原先只写了三个"移植来源"，实际依赖一个没列，安装包里也没有 `licenses\` |
| `check_logging.py` | 日志三件事：**轮转**（真写满 5 MB 触发，出现 `bridge.log.1`、份数封顶）、**脱敏**（MAC / 12 位裸 hex / `DeviceAddressCache=` 都打掉，且**落盘文件里**就已打码）、**raw HID 默认不记**；外加接线（`main.py` 走 `logsetup.install` 而非 `basicConfig`、且在**第一行日志之前**应用配置；`frida_hid` / `remote_hid` 的按键日志不再裸打 `raw.hex`；`console_server` 白名单里有这两个字段）。含 **5 条反例自证** | 坏掉的现象是「`bridge.log` 涨到几十 MB，没人敢删也不敢清」和「用户把日志贴到群里，里面是他**完整的蓝牙地址**」—— 后者最要命：排错的第一步就是把日志发出来。另外 `basicConfig` 在 root 已有 handler 时**什么都不做**，"轮转"会悄悄失效（现象只是"日志又变大了"） |
| `check_keymap.py` | 每个映射目标里的键名都真的能解析成扫描码 | 选项能选、按下去没反应，而且全程不报错 —— 这类故障肉眼查不出来 |
| `check_injection.py` | **真的把组合键按下去**，再用键盘钩子确认系统收到了 | 唯一能拦住「SendInput 静默失效」和「组合键顺序错」的一关 |
| `smoke_console.py` | 离屏把控制台真的建出来跑几轮刷新 | 控件名写错、变量漏定义，在 CI 阶段就被拦下，而不是等用户点开才炸 |
| `check_ble_callback_thread.py` | 在没有 asyncio 事件循环的 `Dummy-XXXX` 线程里跑完整语音链路；**外加会话协调器的线程纪律（P2-3）**：静态核「每个会碰状态的入口都持 `RLock`」「每个 `self._schedule(...)` 都收下了返回的定时器」（用 ast 判，不是正则 —— 这些调用常跨行）；行为上跑两条 —— 「**对照**：不 `close()` 时重试定时器确实会到点开麦（证明测的不是空气）」+「**判据**：`close()` 之后它必须闭嘴」、以及「`shutdown()` 之后所有入口变 no-op」 | v1.0.3 的真机事故：BLE 回调线程里 `get_event_loop()` 直接抛异常，`MIC_OPEN` 一次都没发出去，日志还假报成功。纯标准库、不需要真机，所以放进 CI 当回归闸。⚠ P2-3 那条治的是：`_retry_open` 的定时器原先**返回值直接丢了**，`close()` 取消不到它 —— 会话已经收尾，1 秒后它到点照样把相位推回 OPENING 并**再发一次 MIC_OPEN**（链路可能已经重连过了）。另一头是 `state.phase` / `mic_open_sent` 被两个线程同时读写，没有锁就可能留下自相矛盾的状态 —— 表现是"下一次按语音键没反应" |
| `check_packaging.py` | `remote-voice-bridge.spec` 里有诊断 EXE 入口、`installer.iss` 里名字与它一致并挂进了开始菜单；**v1.0.20 起还管按键旁路**：`frida_tap.js` 作为 data 进主 EXE、`frida_hid` 进 hiddenimports（比对用带引号的 `'frida_hid'` —— 不加引号会连 spec 注释里的 `frida_hid.py` 一起匹配上，漏收也照样绿）、`[Files]` 递归拷整个 bundle。含 **`--selftest`（4 条反例）** | v1.0.5 漏了这一步：诊断工具写好了、版也发了，却没打进 `Setup.exe` —— 安装版用户机器上没有 Python，仓库里那个 `.bat` 对他们等于不存在。**v1.0.20 是同一类洞、更隐蔽**：旁路少一个文件的现象是「语音完全正常、除语音键外的按键一个都不灵」，用户只会以为"按键又坏了" |
| `check_levels.py` | 三路电平/波形是否"有消费者、也有生产者" | v1.0.7 真机：「遥控器麦克风」波形在动、状态却永远卡在「等待语音」—— 根因是**没有任何产品代码喂** `state.remote_level_db`（只有演示脚本喂过） |
| `check_mix_persist.py` | 音频页的开关**落盘**了没有 | 「界面上关了、后端还在用」：`/api/mix` 只改内存不写 `config.json`，设置页一动就被悄悄回滚。沙箱 APPDATA，不碰真配置 |
| `check_hidinfo.py` | 遥控器的 HID 判定链：厂商自定义页（`0xFF00+`）Windows 不处理、键盘页会被处理、设备路径与 `cbSize` 偏移无关 | v1.0.8 查到：遥控器暴露**两个厂商自定义集合**（`0xFF01`/`0xFF80`，各 21 字节输入报告），Windows 对它们不做任何事。按键若发在那里，改映射表**永远没用** —— 判定改错，报告就会把用户指向错误的修法 |
| `check_pairing.py` | 蓝牙配对判定链：6 种状态（真机形态必须判成 `STALE_ADDR`、`migrate` 之后的形态必须是 `NODE_PHANTOM`）；分支顺序（`STALE_ADDR` 要排在 `NODE_PHANTOM` 前面）；「幽灵」口径；PnP 实例 ID 带 `USB\` 前缀；搬家保留注册表值类型；「删不动」只在管理员下下结论；ACL 接管开一组特权 + 循环到不动点；提权带全部开关。含**行为级反例**（把 `classify` 改坏 exec 起来必须判错） | v1.0.10 的真机事故：「换过 USB 口就连不上」—— 程序显示未连接、Windows 显示已配对、设置里还删不掉。判定读的是真机注册表，CI 里没有蓝牙棒，所以抽成纯函数 + 假数据锁死 |
| `check_failure_visibility.py` | 故障必须「看得见、说人话」：桥线程异常走 logging（不是 `print`）、连接失败必须给出原因+处置且不只试一条路、托盘必须能说出具体原因、`_open_ble_device` 必须真被 `run_bridge` 调用；**外加（2026-09-29）所有 `text=True` 的子进程调用必须带 `encoding=` 或 `errors=`**（用 ast 判，不是正则 —— 这些调用几乎都跨行） | v1.0.9 真机：桥每 3 秒崩一次却**一行报错都没有** —— 因为 `tray_app` 用 `print` 报异常，而主 exe 是 `console=False`（输出流向是空的）；托盘还一直写着「按遥控器任意键唤醒」，把 `OSError: E_INVALIDARG` 捂了两小时。这类「静默 + 误导」是这个项目最反复的一类 bug，所以用反例锁死。⚠ 新加的那条是同一个家系：`subprocess(text=True)` 按 **locale 编码**解子进程输出，而 Windows 工具（`tasklist`/`powershell`）按**控制台代码页**吐字节 —— 解错会抛在 subprocess 的**读取线程**里，主流程只看到 `p.stdout` 为空，于是把「桥程序正在跑」判成「没在跑」（实测环境带 `PYTHONUTF8=1` 时就是这样）。**崩溃看得见，静默的空输出才致命** |
| `check_remote_hid.py` | 厂商页按键解码：解码表完整（int-usage 的键一个不少、且**不含**语音键）、17 条报告格式、按下→松手配对、遥控器集合能不能打开；**审计日志限流**（用假时钟跑 30 次：既"报过"又"没刷屏"，含 2 条反例） | v1.0.11 起按键映射全靠厂商页，解码表错一位就是「键全串位」；**不用按遥控器**就能验，真机上按一次键再对日志 | 
| `check_frida_tap.py` | 按键旁路（v1.0.20：注入 `WUDFHost` 从 HID 驱动内部抄报告 —— **真机上唯一能拿到遥控器按键的那一路**）：消费类页 usage 表与 `config.CHROMECAST_BUTTONS` 一一对齐、**不含**语音键；两种报告格式的解码（消费类页 3 字节**小端 16 位** / 厂商页 8 位）；按下→松手配对；「只抹已映射的键」算法；**`.js` 与 `.py` 的 IOCTL 常量不许漂移**；含 **2 条反例自证**（把字节序写反、把语音键塞进表里，检查必须变红） | v1.0.20 定案：报告不是没发，是在 `WUDFHost.exe` 内部就被 UMDF 驱动消费掉了 —— 用户态 HID 接口 / Raw Input / 键盘钩子全都看不见（自开厂商页那条路真机实测 0 条）。这一路的失效**全是静默的**（注入失败 / 挂错宿主 / 格式不对，现象都是"按了没反应"），所以先把能脱离硬件验证的那一半钉死 |
| `check_audio_rate.py` | 采样率必须**先跟遥控器协商、再建音频链**（P2-1）：能力响应信号用 `threading.Event`（置位发生在 BLE 回调线程上，`asyncio.Event.set()` 不是线程安全的）；等待**夹在**「CAPS 请求发出」与「找 CABLE / 建链」之间（比位置，不是比存在）；`caps_ready.set()` 在 `if caps:` **里面**（解析失败不能放行）；`_start_stream()` 读 `_audio_rate["sr"]` 而不是自己再读一次协商值；**响应迟到且速率不同要重建整条链**（含电脑麦克风 —— `SystemMic` 按 `out_rate` 重采样，只换输出流同样不对）。含 **6 条反例自证** | 采样率是**遥控器**在 CAPS 里给的（8k/16k），而 `ATVVState` 默认就是 16000。老代码「发完请求就建流」⇒ 8k 的遥控器被**按 16k 建流**：**不报错、不崩、不刷日志**，只是声音变调变速 —— 用户只会说"声音怪怪的"，排查时日志里一个字都没有 |
| `make_deps_lock.py` | 依赖锁：`--check`（离线，进 CI 清单）核「锁与 `requirements*.txt` 版本一致」「每行都带 `--hash=sha256:`」「requirements 里不许再出现 `>=` / `<` / `~=` 这种范围写法」；不带参数则按目标平台（win_amd64 / CPython 3.11）重新生成 `requirements.lock.txt` | `requirements.txt` 里**传递依赖没锁版本**（pyinstaller 拉 altgraph/pefile/pywin32-ctypes/setuptools、frida 拉 cffi/pycparser/six）⇒ 同一份代码在不同时间构建出的产物**不一样**。而按键旁路靠的正是 frida 的原生扩展，它一变就是"语音正常、按键全不灵"的静默故障。⚠ 这个锁**被真的用来安装**（CI 里 `pip install --require-hashes -r requirements.lock.txt`）—— 哈希对不上就装不上、构建当场失败，而不是产出一个没人验过的包 |
| `check_audio_watchdog.py` | 音频输出流停摆必须**可见且能自愈**：回调用 `_cb_last_at` 报心跳、心跳写在混合逻辑之前、主循环每轮调 `_supervise_audio`、超时走 `_rebuild_audio`、重建前清积压、混音回调**无论增益是否为 0 都按一比一消费队列**、队列满不再只打 debug。含「不许退回老写法」的反例断言 | v1.0.12 真机：语音输入开着、波形在动，输入法却收不到声音，重启才好。根因是输出流**建一次就没人管** + 静音期间不消费队列导致 `queue.Full` 静默丢帧。纯静态，几毫秒 |
| `check_send_after_voice.py` | 语音说完之后**什么时候发由用户决定**：`send_after_voice` **默认必须是关**（2026-09-17 武哥否掉了"默认开"——会自动把用户没想好的话发出去）、`_migrate` 把旧配置归位成关、`CONFIG_VERSION` 同步、呆瓜配置里也不许打开、读不到配置时兜底是**不发**；开关打开后：延迟 ≥300ms（太小会在文字落进输入框**之前**就回车，等于把刚说的话弄丢一次）、4 条收尾路径各自接对（确认键防连发两下、**超时收尾故意不发**）、开始新一段要取消待发送、零音频帧不发送（误触保护）、帧计数**不在** `audio_start` 顶部清零。含 **9 项行为级**（真 import main + 假时钟 + 假 tap_key，第一条就是「默认配置下到点了也绝不发」）+ **14 条反例自证** | v1.0.13：用户既要"不用去够鼠标"，也要"什么时候发我说了算"——这两件事的平衡点就是**默认不发 + 按键盘回车**。遥控器除语音键以外的键在 Windows 上收不到报告，所以绑按键这条路暂时走不通 |
| `check_takeover_guard.py` | `takeover_hid_reports.py` 是这个项目里**唯一会改系统设备状态**的东西，这道闸钉它三条：**① 默认只读**（`--go` 是 `store_true`，不带就 `return`，而且"不 go"的判断必须在 `Popen` 之前）；**② 目标必须是 `BTHLEDEVICE` 父节点**（`0x1812` 底下是**一父 + 5 个 HID 子集合**，挑到 `COLxx` 空集合就是白测一场 + 错判「软件到头了」）、找不到就 `return 3` 不退而求其次；**③ 禁用与恢复在同一条 PowerShell 命令里**、`Start-Sleep` 夹中间、启用失败输出 `ENABLE_FAIL`、用 `Popen`（用 `run` 会把监听时间睡掉）、收尾回读状态不是 `OK` 就喊人；**④ 一键入口 `run-0x1812-takeover.bat` 已退役**（不再提权 / 不再 `taskkill` / 不再传 `--go`，只剩"已退役 + 为什么"的说明）。含 **3 条反例自证**（改回 `Get-PnpDevice \| Select-Object -First 1`、去掉默认只读、把一键入口改回老样子，都必须报红） | 2026-09-22 真踩过：第一版就是 `Select-Object -First 1`，拿到的是 `COL05`（无服务的空集合）—— 照那样跑会禁错节点、一条报告也收不到，然后拿这个**假阴性**去关掉最后一条纯软件的路。⚠ 这道闸自己第一版也栽在同一个坑上：判据用字面量匹配，而 `BTHLEDEVICE` / `Disable-PnpDevice` 恰好都写在**注释和说明文字**里 → 加 `_ps_code()` / `_code_only()` 剥掉注释与 `say(...)` 再判。不写进闸里，"顺手把 `--go` 去掉方便点"就会让体检动手改设备 |
| `check_voice_session.py` | 语音会话状态机**不许自己把自己关掉**、也**不许永远启动不了**：① 防自激窗口内要能吞下**多个**回响（不许"挡一个就清零 `mic_reopen_at`"——那样第 2 个回响会落进结束分支；**也不许刷新它**——窗口只能靠时间过期）①' **顺序铁律：先算 `is_echo`、再补发 MIC_OPEN**（反了＝真按键把自己判成回声、当场吞掉），且窗口宽度必须落在 **[1.2, 3.0] 秒**（实测回响最慢 1070ms：<1.2 会漏 —— v1.0.14 的 0.8 秒就是「按下就掉」；当前值 `ECHO_MAX_AGE = 1.5`，闸门直接读源码里的常量，不是比对字面量）② 吞掉的事件必须记数、不许静默 ③ 松手（`audio_stop`）不许出现任何结束语义（没有 `voice_hotkey_up()`、没有 `voice_active = False`）、只结算统计＋补开麦＋置 `stream_active` ④ 帧计数不在 `audio_start` 顶部清零、**且必须在判回声之后** ⑤ 结束分支接了发送登记 ⑥ **写入的「真实结果」必须回到状态机**（P2-2）：`_send_tx` 的返回值**只代表"投递到事件循环"**，GATT 写失败时要销掉那笔回声账 + **复位 `mic_open_sent`**（不复位＝`ensure_mic_open` 永远直接 return、遥控器一帧都不推，而日志上只有一行"补发成功"）；且 `_on_mic_open` 里必须**先记账、再排队**（反了＝投递一返回就可能回调销账，而账还没记上）。含 **20 条反例自证** | 两段真机日志把同一个坑的两个方向钉全了。**2026-09-22 22:00 之后 10 次会话里 4 次在 1.2~1.6 秒内被结束**：两个 `audio_start` 只隔 19ms，第一个被"防自激"吃掉并**清零**，第二个就当成"用户第二次按下"→ 注入热键把输入法语音输入**关掉** + 结束会话（「按一下、刚开口，一个字都出不来」）。于是 v1.0.13 改成"不清零"，结果**更糟**：补开麦排在判回声之前、成功时顺手记 `mic_reopen_at = 现在`，于是**真按键自己把自己判成回声**；而每次松手都会把相位打回 CLOSED、`mic_open_sent` 复位 → **下一次按键又重来一遍** → 连按 10 次一次都启动不了（日志里 `🎤 voice hotkey TAP` 一行没有）。⚠ 这类**顺序耦合**在源码里完全看不见（两行都合法、单独看都对），只能由闸门钉死 |
| `check_bat_env.py` | 仓库根目录 `.bat` 的**三条**一致性：**① 凡"自己挑解释器"的 bat，Python 候选列表必须逐项、按序相同**（`%USERPROFILE%\.workbuddy\...\envs\rvb\...` → `.venv` → 兜底 `python`）；**② 会打印非 ASCII 的行必须自己声明 `chcp`，且字节要能按该 chcp 解**；**③ 只要文件里有非 ASCII（含注释）就必须是 GBK + `chcp 936`**。边界按实测收窄过（② 只算非注释行、只算自己挑解释器的 bat；`run.bat` 明确豁免并写明原因），免得假红逼人乱改文件。含 **9 条反例自证** | 2026-09-23 真机：四个 bat 各写各的 Python 探测，而本机**没有** `.venv`、PATH 上那个 python 又**没装项目依赖**（bleak/winsdk/keyboard），唯一装齐的只有托管环境里那个 ⇒ 同一台机器上「一个工具能跑、另一个报『当前 Python 跑不起来』」—— 而这两步在用户路径上**一前一后**（先 `修复蓝牙配对.bat`、再 `测遥控器按键通道.bat`）。⚠ 我自己另犯过反向的错：按「中文 Windows 一律 GBK」把一个 `chcp 65001` 的文件转了编码，框线字符直接编不出来 —— **文件自己的 chcp 才是准的**。**③ 是 2026-09-29 真机验收撞出来的**：`chcp 65001` 之后 cmd.exe 会按**切换前的字节偏移**继续读批处理文件，UTF-8 的中文是 3 字节、偏移对不上 ⇒ 从一行中间接着读、`REM` 前缀被吃掉、剩下半句当命令执行，用户双击先看到一屏 `'xxx' is not recognized`。⚠ ② 的旧假设在这里**不成立**（② 说"REM 里的中文不打印，无所谓"，而错位吃的正是 REM 前缀），所以 ③ 不跳过注释。判定用「GBK 往返一致」而不是"能不能 decode"—— UTF-8 的 `⚠` 恰好也能被 GBK 解出来，但编回去不是原来的字节 |
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
| `pairing.py` | 「换过 USB 口之后程序显示未连接，Windows 却显示已配对」——**先跑这个** | **安装版**开始菜单 → **「修复蓝牙配对」**（等价于安装目录里的 `修复蓝牙配对.bat`，也等价于 `RemoteVoiceBridgeDiag.exe --fix-pairing`）；**源码版**双击 `修复蓝牙配对.bat` 或 `python pairing.py --fix-pairing`。⚠ **不带 `--fix-pairing` 就是纯只读诊断**（不需要管理员）。修复会弹一次 UAC，动注册表前自动备份到 `%ProgramData%\remote-voice-bridge\backup\`（v1.0.22 起；备份是**受保护的事务**：最小子树 + manifest（事务 ID / SHA-256）+ 解析 `.reg` 键路径限制前缀，还原只认 manifest 里列过的文件）。可用 `--method restore\|migrate\|purge` 指定方案，`purge` 需 `--yes`。报告：`pairing-fix.txt` |
| `check_remote_hid.py` | 「按键没反应」先跑它 —— **不用按键**就能验厂商页解码；`--watch N` 再实时听 N 秒 | `python tools\check_remote_hid.py`（纯单测）/ `--list`（列集合）/ `--watch 20`（监听 20 秒，期间按遥控器） |
| `check_frida_tap.py` | **「除语音键外的按键没反应」的首选自测**（v1.0.20 起）—— 注入 `WUDFHost` 实时听遥控器按键 | **源码版双击仓库根目录的 `测按键旁路.bat`**（= `python tools\check_frida_tap.py --watch 90`）。⚠ 第一次可能弹一次 UAC（注入需要管理员授权），点「是」。按方向/确认/返回/音量键，看有没有 `🔘 按键旁路 → 按钮「…」`。一行都没有时把屏幕上的**「自检」那行**发出来 —— 它会说清是「挂错宿主」还是「这款遥控器报文格式不同」 |
| `watch_reports.py` | 想知道**某个键的报告到底落在哪一路**（键盘 / 鼠标 / 厂商自定义页） | `python tools\watch_reports.py`（默认听 40 秒，`--seconds 60` 改时长，`--all` 连非 Google 的集合一起看）。⚠ **跑之前先退出桥接程序**（托盘右键 → 退出），否则它会把遥控器原生按键吞掉、报告显示"一条都没收到"。产出 `%APPDATA%\remote-voice-bridge\remote-hidwatch.txt`（明细里每个键盘事件都带 `[真实]` / `[注入]` / `[分不出]` 标记） |
| `watch_all_channels.py` | **「按键到底走哪条通道」的终极一问** —— HID 层看只有 0 条时，用它把**所有可能的路**一次全挂上 | **源码版直接双击仓库根目录的 `测遥控器按键通道.bat`**（顺带替你处理"桥程序还开着"这件事），或 `python tools\watch_all_channels.py`（默认 90 秒，`--seconds N` 改）。⚠ 先退出桥接程序（它占着 ATVV，不关就听不到 CTL）；实在要带着它测加 `--force`。同时监听：**键盘钩子（真实 / 注入分开数）** / 5 路 HID 集合 / 私有服务 `AE42` / 私有服务 `D343BFC5` / **ATVV CTL 的每一个字节**（认不出的 opcode 会标【认不出】）。**不用按键也能跑**（静态部分照出）。产出 `%APPDATA%\remote-voice-bridge\watch-all-channels.txt`。⚠ 判读**只看「真实（非注入）」那一行**：本程序每按一次语音键就会注入 `lctrl+lwin+lshift`，那些事件以前被当成了遥控器的按键（见 CHANGELOG v1.0.13 §六）。「订上了但 0 条」的私有服务也会进表 —— 别再分不清"是 0"和"没测"。**屏幕会分四段提示你按哪个键**（确认 → 返回 → 主页 → 方向/音量），提示行带实际秒数，报告里带时间戳的事件行能直接对上号。⚠ 报告开头必有「桥程序：」一行（含依据）—— 判读私有服务那两路的前提就是它。⚠ 两条铁律由 `--selftest` 钉住：**① GATT 连不上也必须把窗口跑满**（2026-09-23 真机：「跑一下就结束、都没等我按按钮」，因为窗口原先是长在 GATT 那个函数里的，BLE 一连不上就跟着被 `return` 掉了）；**② 配对列表里同名条目要逐个真打开、按连接状态挑**（同一个名字下有幽灵条目，抢第一个会报 `E_INVALIDARG`，顺序随枚举变 ⇒ 时好时坏）。⚠ **窗口之前还会先做一次「阳性对照」**：提示你按**一次语音键**（那是这个设备上唯一已知能到的通道，ATVV CTL `0x04`）——收到就写「✅ 通过：设备能发 + 工具在听 + 你确实按了 ⇒ 后面的 0 是真 0」；没收到就写「❌ 没通过 ⇒ **本轮没测成**」，**不许**把 0 读成「按键没来」；桥程序占着 ATVV 时写「⚠ 不可用」。这是本工具第一条**能自证「这次到底测成没测成」**的机制 |
| `radio_cycle.py` | 「不想重配对，先让 Windows 把 HOGP 那一路重新 attach 一遍」 | `python tools\radio_cycle.py`（关蓝牙 3 秒再开，然后等遥控器重连，最多 60 秒）/ `--status`（只看状态）/ `--off-only`。开关的就是设置面板那个蓝牙开关（`Windows.Devices.Radios`），**不动配对记录、不动注册表**。缺 `winrt-Windows.Devices.Radios` 时不崩，直接打印等价的手动步骤 |
| `probe_gatt_hid.py` | 绕开 Windows HID 栈，**直接问蓝牙**：遥控器暴露了哪些服务/特征 | `python tools\probe_gatt_hid.py` / 加 `--listen 40` 订阅并实时听。**不用退出桥程序**。产出 `gatt-probe.txt` |
| `probe_hid_claim.py` | HID 服务被 `ACCESS_DENIED` 挡住时，换三个入口（按 UUID 直接取 / `from_id_async` 重开 / 写 HID Control Point）能不能"要"过来 | `python tools\probe_hid_claim.py` |
| `dump_report_descriptor.py` | 想看遥控器各集合的 HID 报告描述符（**设备自报的权威**） | `python tools\dump_report_descriptor.py`。⚠ 顺带记下一个坑：`IOCTL 0x000B0193` 返回的是 `HidP KDR` 结构（缓冲区开头就是 ASCII `HidPKDR`），**不是**原始描述符 |
| `takeover_hid_reports.py` | **纯软件的最后一条路** —— 把 Windows 那个 HOGP 节点临时停掉，让 `0x1812` 空出来，我们自己订阅 Report(`0x2A4D`) 看遥控器**到底发不发按键报告** | **默认只读**：`python tools\takeover_hid_reports.py`（只列 6 个节点 + 打印"如果执行会动什么"，一个字节都不改）。⚠ **一键入口 `run-0x1812-takeover.bat` 已退役（v1.0.22）** —— 它现在只是个打印"已退役 + 为什么"的壳，不提权、不 `taskkill`、不传 `--go`（理由：那条路被 Windows 封死，`DevNodeStatus` 里没有 `DN_DISABLEABLE`，跑一次必然 `DISABLE_FAIL`；而"要管理员 + 强杀桥程序 + 动系统设备状态"去回答一个已有答案的问题，只有风险没有收益。闸门 `check_takeover_guard.py` ④ 钉着这一点）。真做只剩一条路：`python tools\takeover_hid_reports.py --go --seconds 60`（**要管理员**，建议先退出桥程序）—— **但先读完下面那条"禁不掉"的判读，很可能没必要跑**。⚠ 只禁 `BTHLEDEVICE\…mshidumdf` 那一个父节点 —— 禁 `HID\…&COLxx` 子集合没用（会话还在父节点手里，收不到报告会错判成"遥控器不发按键"）。恢复写在**同一条** PowerShell 命令里（禁用→睡→启用），Python 这边崩了/Ctrl+C 也会回来，收尾还会回读状态确认。产出 `%APPDATA%\remote-voice-bridge\takeover-hid-reports.txt`。判读是**三**值的：**收到报告** ⇒ 按键走 `0x1812`，本项目可以纯软件解码；**订上了但 0 条** ⇒ 遥控器确实不发按键数据；**⚠ 禁不掉**（`DISABLE_FAIL: 不支持` + `Cannot disable critical system device`）⇒ **这条路被 Windows 封死**：那个 devnode 的 `DevNodeStatus` 里没有 `DN_DISABLEABLE` 位（2026-09-22 真机 `0x0180000A`），设备管理器里「禁用设备」同样是灰的，用户态没别的办法 —— 此时看到 `ACCESS_DENIED` 是必然，**不是"没测成"**，也别拿它当"遥控器不发按键"的证据。⚠ 另外 `pnputil` 失败也会返回 `rc=0`，判据只看输出正文。 |
| `probe_devnode_binding.py` | 想知道遥控器每个 devnode **绑没绑驱动**、有没有 `Problem` | `python tools\probe_devnode_binding.py`。⚠ 注意判读口径：HIDClass 子集合（消费类/厂商页）的 `Service=(未绑定)` 是**正常**的（Driver 指向 HIDClass 类 GUID `{745a17a0-…}`）；HID 服务的驱动是 `mshidumdf`（Windows 的 HOGP 驱动） |

> `diag_remote.py` 的**真机部分**必须有人按键，没法自动化；但它的「报告生成器」
> 是纯函数式的，`check_all` 会用假数据把两条分支（能区分设备 / 不能区分）都跑一遍 ——
> 否则那段代码第一次运行就是在用户机器上。跑法：`python tools\diag_remote.py --selftest`。

## 现场探针（要人按键、CI 不跑）

这一类存在的理由：**有些结论只能由"现场"给出，不能靠读代码推断**。
它们不是闸门、不进 `check_all.py`，而是当有人主张"某某路还有戏"时，
**60 秒内正面回答**，省掉下一次翻 11 万行日志。
每个探针都自带 `--selftest`：先证明判读逻辑可信（不碰键盘、不用遥控器），
再去现场采数据 —— **自证不 PASS 就别信它的现场结论**。

| 脚本 | 回答什么问题 | 怎么用 |
|---|---|---|
| `probe_hogp_state.py` | 「设备管理器一切正常、配对也显示已配对，为什么按键一个都不到」—— **按键到底卡在哪一环**。四段：① **配对账本的密钥材料**（**三态**：读到 / 存在但空 / 读不到；密钥在 `BTHPORT\Parameters\Keys\<适配器MAC>\<设备MAC>\`，**读不到就只报读不到**，不给「没有密钥」的结论 —— 上一版就是在这儿判错的，详见该文件开头注释）② **Windows 此刻连没连着**（`ConnectionStatus`，一条就否掉「没连上所以收不到」）③ 同一个遥控器有几条已配对条目 ④ 各 devnode 的**在场**判定（cfgmgr32，能分出「现在活着的那一代」和注册表里的幽灵）。末尾给一段**综合判决**：已确定 / 仍未定 / 下一步 | `python tools\probe_hogp_state.py`（**只读，不用按键、不用退桥程序**）/ `--selftest`（只验判词，不碰注册表，8 项含 4 条反例）/ `--rebind`（需管理员，非破坏性重枚举） |
| `probe_remote_keys_reach.py` | **遥控器的按键到底有没有进 Windows** —— 这是「按键没反应」所有分支的总闸。**两段式**：① 基线 10 秒（什么都别按，量本底）② 取样 N 秒（**只按遥控器**的键）。判读**三态不含糊**：出现"基线里没有、且不是合成事件的"键 ⇒ **进了 Windows**（那问题在我们这层，纯软件能修）；一条新的都没有 ⇒ **没进 Windows**；基线被打字污染 ⇒ **无法判定**，重跑 | `python tools\probe_remote_keys_reach.py`（基线 10s + 取样 40s）/ `--seconds 60`（改取样时长）/ `--json`（末尾多打一行 JSON）/ `--selftest`（只验判读，不用遥控器）。⚠ **先退掉安装版 `RemoteVoiceBridge.exe`**（它的键盘钩子会把按键再报一遍，噪声翻倍） |

> **口径（本项目踩过，别再改回去）**：探针把 `scan_code < 0` 的一律排除，叫**「合成事件」**，
> 不叫"我们注入的"—— 实测 `send_after_voice=False` 时仍看到一个 `enter`(scan=-13)，
> 那是输入法之类合成的。这个筛子安全，是因为遥控器的键若真进了 Windows，
> 是**系统的 HID 栈**投递的、**一定带真实 scan 码**（真机实测 `left`=75、`right`=77、`enter`=28 全为正）
> ⇒ 滤掉负号**不会漏掉遥控器**。
>
> **为什么必须有 `--selftest`**：这个探针要是把"热键注入的回环"（`f13`/`left ctrl`/`reserved `
> 那批负 scan 码）判成"遥控器的键进去了"，就会给出**正好相反**的结论，
> 而那种结论会让人再去折腾一轮纯软件方案。所以第 3 条自证专门钉这条：
> **只喂合成事件 → 必须仍判"没进"**。

## 公共小工具

`_utf8.py` —— 钉住控制台编码。Windows 控制台默认 cp936/GBK，
脚本里只要有中文输出，在 CI（或某些机器）上就会 `UnicodeEncodeError` 直接崩。
**每个会打印中文的脚本都要在最开头调用它**，别裸 `print("中文")`。

`_bat.py` —— **读仓库根目录那些 `.bat` 的唯一入口**。它们的编码不止一种，
而且两种都是对的（`chcp 936` 配 GBK、`chcp 65001` 配 UTF-8，由
`check_bat_env.py` 管）。2026-09-29 把那几个 bat 从 UTF-8 转成 GBK 之后，
`check_run_bat.py` / `check_ci_workflow.py` / `check_takeover_guard.py` 里
**各自写死**的 `decode("utf-8")` 当场解出乱码，判据全部变成"找不到那个字符串"，
报出来的却是「run.bat 没启动 tray_app.py」这种**指向完全错误**的结论。
所以别在闸门里自己 `open(...).read()` 读 `.bat`，一律走它。

`_echo_latency_probe.py` —— 从 `bridge.log` 里量"我们发 MIC_OPEN → 遥控器回
audio_start"的**延迟分布**。它存在是因为一个具体的教训：v1.0.14 把防自激窗口
收成 0.8s，依据的"回声最长 ~350ms"是**错口径**量出来的（量的是"排队时刻"，
不是"写入完成时刻"）。真机 239 次实测量出来是**中位 20ms、最大 1070ms**，
`>0.8s` 有 8 条 —— 那 8 条就是"按一次掉"的来源。
**以后调这个窗口，先用它量一遍，别再用拍的数。**
不带参数直接跑；只读日志，不改任何东西。
