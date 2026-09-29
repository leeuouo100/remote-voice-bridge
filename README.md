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

## 版本与更新日志

**当前版本：v1.0.22。**

版本号只在 `config.py` 的 `APP_VERSION` 里写一次；`installer.iss` 的默认值、
git tag 必须跟它一致 —— CI 会拦不一致（`tools/check_version.py`）。

> ⚠ **README 只讲当前行为；逐版历史只在 [CHANGELOG.md](CHANGELOG.md)。**
>
> 这里以前也留了一份逐版摘要，最后停在 v1.0.14 的「回声窗口 **0.8 秒**」——
> 而那个数 **v1.0.15 就推翻了**：回声是遥控器**收到** `MIC_OPEN` 之后才发的，
> 而 GATT 写入在真机上能慢到 **~1.07 秒**，0.8 秒的窗口根本兜不住 ⇒
> 现象是「**按一次掉、再按一次才能说话**」。
> 现在实现的是 **1.5 秒窗口 + 一笔「欠一声回声」的账**（见 `main.py` 的
> `ECHO_MAX_AGE` 那段注释），判据不只是"过了多久"。
> 两份历史必然漂，所以只留一份 —— **想知道"为什么是现在这样"，去 CHANGELOG 按版本号找。**

关于"**现在是什么行为**"，看下面这两节：

- **使用** —— 装、配对、说、按键、发送，全流程的当前行为
- **✅ 按键这件事查清了（v1.0.20 修好）** —— 按键为什么以前一个都到不了 Windows

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

### 支持范围（Python 版本）

**用安装版（`RemoteVoiceBridge-Setup-*.exe`）的话，不需要装 Python** ——
运行时是 PyInstaller 一起打进去的。

**从源码跑（`run.bat`）只支持 Python 3.10 / 3.11。** 唯一真源是 `config.py`
的 `PY_MIN` / `PY_MAX`，CI 验的版本必须落在里面（`tools/check_ci_workflow.py` 盯着）。

> 为什么不是更宽：`requirements.txt` 里 `numpy==1.24.3` **在 3.12 / 3.13 上没有
> 预编译包**（要现场编译，基本装不上）。而它又被音频那一路钉着。
> 要放宽 = 升级依赖 + 在 CI 里建版本矩阵，两件事得一起做。

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

**程序显示「未连接」，但 Windows 设置里写着「已配对」（甚至还有电量）：**

**配对记录是"按本地蓝牙无线电"存的**，而这颗 USB 蓝牙棒的地址是**按插在哪个 USB 口**
缓存的 —— 换口 → 地址变 → 旧记录作废：Windows 设置里照样显示「已配对」，
但蓝牙栈造不出可用设备，怎么重试都连不上。（2026-09-15 真机就是这个。）

**v1.0.10 起可以一键修好，不用重新配对：**

```
开始菜单 → 「修复蓝牙配对」          （等价于安装目录里的 修复蓝牙配对.bat）
```

它会先只读诊断，发现问题就弹一次 UAC（**动注册表前自动备份**到
`%ProgramData%\remote-voice-bridge\backup\` —— v1.0.22 起从 `%APPDATA%` 搬过去，
因为那个目录当前用户可写，而备份里装的是能解密链路的密钥材料），
修完重启蓝牙栈、再真的把设备打开一次当验收。

删完节点之后它要**按一下遥控器任意键**把设备唤醒（最多等 60 秒），
Windows 才会按当前适配器把节点重建出来 —— 超时也没关系，下次遥控器一醒就会自动重建。

只想看看、什么都不改：

```bat
python pairing.py                      :: 源码版（只读诊断，不需要管理员）
RemoteVoiceBridgeDiag.exe              :: 安装版：直接跑就是只读诊断
```

日志和报告：

```bat
%APPDATA%\remote-voice-bridge\bridge.log        :: 为什么连不上（含两个地址的对比）
%APPDATA%\remote-voice-bridge\bridge.log.1      :: 上一份（写满 5 MB 轮转，最多 3 份）
%APPDATA%\remote-voice-bridge\pairing-fix.txt   :: 完整体检报告
%APPDATA%\remote-voice-bridge\pairing-fix.log   :: 修复过程日志
```

> **日志会轮转，也会打码**（v1.0.23 起）：`bridge.log` 到 5 MB 就转成
> `bridge.log.1`（留 3 份，约 20 MB 封顶），不用再手动清。日志里的蓝牙地址
> **默认只留前 2 / 后 2 字节**（`04:xx:xx:xx:xx:94`），所以**可以直接贴给别人**；
> "两个地址是不是同一个"看首尾字节照样分得出来。要看完整地址就把 `config.json`
> 的 `"log_redact"` 改成 `false`。原始按键字节（`raw=02 42 00`）默认**不记**，
> 要看就把 `"log_raw_hid"` 改成 `true`。

想判断「**等它重建就行**」还是「**必须重新配对**」：

```bat
RemoteVoiceBridgeDiag.exe --keys    :: 提权只读，列出链路密钥树
```

> ⚠ **别把「父键读不到」读成「密钥丢了」。** 这两件事的处置**正好相反**，
> 而它们的区别只在"**我是在哪一层看的**"：
>
> | 键 | 里面是什么 | 本机实测（非管理员） |
> |---|---|---|
> | `Parameters\Keys`（父） | 只是个容器 | `PermissionError` WinError 5 |
> | `Parameters\Keys\<本地地址>`（中间层） | 每个已配对设备的子键 | 同上 |
> | `Parameters\Keys\<本地地址>\<设备地址>`（**叶子**） | **真密钥**：`LTK` / `IRK` / `CSRK` / `EDIV` / `ERand` | **读得到，而且齐全** |
> | `Parameters\Devices\<设备地址>` | **元数据**：名字 / VID / PID / 外观 / 时间戳 | 读得到（**那里本来就不该有 LTK**） |
>
> ⇒ ① **父键打不开是 ACL，不是"没有"**；② 拿元数据键（`Devices\...`）下
> "没有密钥材料"的结论是错的 —— 它从来就不放密钥。
>
> 所以判词只有**三态**：**读到 / 存在但空 / 读不到**。`--keys` 会把三种分开说；
> 而"读不到"只许报「**无法判定**」，不许报「没有密钥」—— 后者会把人骗去
> `purge` 重配对，而重配对要人手按遥控器组合键，是有代价的。
>
> **本机实测（2026-09-28）：配对是健康的 —— 记录与无线电一致、设备能真打开、
> 密钥齐全。** 所以**换 USB 口之前不需要 `--fix`**；换完口之后才需要
> （新地址上没有旧记录）。

**根治建议**：这颗蓝牙棒**固定插一个口别换**；机器上还有别的蓝牙（本机的英特尔板载
就是个幽灵：注册表里有、PnP 里根本不存在）就在设备管理器里禁掉，免得它抢。

> 如果修复工具报「关联节点删不动」（ACL 锁住），加 `--take-ownership`：
> `RemoteVoiceBridgeDiag.exe --fix-pairing --method rebuild --take-ownership`
> → 遥控器按任意键唤醒。（⚠️ 它会改系统键的 ACL：所有者 SYSTEM → Administrators，
> 所以默认不开。）
>
> 还是不行，最后兜底是清掉记录重新配对一次：
> `RemoteVoiceBridgeDiag.exe --fix-pairing --method purge --yes --take-ownership`
> → 遥控器长按 **Home + 返回 约 3 秒** 进配对模式 → 重新添加。
>
> 想看"到底是谁挡着不让删"：
> `RemoteVoiceBridgeDiag.exe --acl-dump`（提权，只读，把所有者 + 每条 ACE 打出来）

**按键没反应 / 想确认遥控器到底发了什么：**

**先跑快的那个（2 秒，不用按键、也不用退程序）：**

```bat
RemoteVoiceBridgeDiag.exe --hid        :: 安装版（在安装目录里，或加完整路径）
python tools\diag_remote.py --hid      :: 源码版
```

它不用你按任何键，直接报出遥控器的 HID 真身。**为什么值这一步** ——
遥控器在 BLE 的 HID 服务（`0x1812`）上暴露了 **5 个 HID 集合**：

| 集合 | 是什么 | Windows 侧实际状态 |
|---|---|---|
| `0x01/0x06` | 键盘（9 字节报告） | `kbdhid` 绑上、STATUS OK |
| `0x0C/0x01` | 消费类控制（4 字节） | HIDClass 绑上、STATUS OK |
| `0x01/0x02` | 鼠标（5 字节） | `mouhid` 绑上、STATUS OK |
| `0xFF01` | 厂商自定义（21 字节） | HIDClass 绑上、**没有驱动处理** |
| `0xFF80` | 厂商自定义（21 字节） | HIDClass 绑上、**没有驱动处理** |

### ✅ 按键这件事查清了（v1.0.20 修好）：报告被驱动宿主吃掉了

2026-09-22 / 23 的两轮取证得出的是「五个集合**一条报告都没来**」，
并据此推断「纯软件和用按键不可兼得」。**这个推断在 2026-09-29 被推翻了** ——
不是没发，是**我们站错了层**：

> BLE HID（HOGP）在 Windows 上是 **UMDF 驱动，跑在 `WUDFHost.exe` 进程里**。
> 它通过 `ntdll!NtDeviceIoControlFile` + IOCTL `0x80018483` 把 GATT 特征读上来，
> **报告就在那次调用的输出缓冲区里** —— 而这一层在用户态 HID 接口**之前**。

所以自己 `ReadFile`、Raw Input、键盘钩子**全都看不到**（报告早被驱动取走了），
`0x1812` 也当然打不开（被 HOGP 独占）。之前每一条排查路，**都是在报告被取走之后才开始看**。

**v1.0.20 的修法**：用 Frida 注入 `WUDFHost.exe`，在那次 IOCTL 的输出缓冲区上抄一份。
不需要内核驱动、不需要关 Secure Boot、不需要测试签名；只需要**一次性管理员授权**做注入。
解出来的按键喂给**现有**的映射表，`config.json` / 控制台一行都不用改。

| 手段 | v1.0.19 之前 | v1.0.20 |
|---|---|---|
| 自开厂商页读报告（`remote_hid.py`） | 真机 0 条 | 保留（对别的驱动组合仍可能有效） |
| 注入驱动宿主读报告（`frida_hid.py`） | — | ✅ 新路：进驱动内部读，绕过整层 HID 映射 |

**怎么验证**：双击仓库根目录的 **`测按键旁路.bat`**，按遥控器的
方向 / 确认 / 返回 / 音量键，看屏幕有没有打印 `🔘 按键旁路 → 按钮「…」`。
一行都没有时，把屏幕上的「自检」那行发出来 —— 它会说清是
「挂错了宿主」还是「这款遥控器的报文格式不同」。

### 想自己复验（60 秒，不改任何东西）

> 按键这条路现在最快的一键自测是 **`测按键旁路.bat`**（v1.0.20，注入驱动宿主读报告）。
> 下面这几个是**更早那轮排查**留下的取证工具，仍然可用 —— 想看清"按键落在哪一路"
> 时它们更细。

```bat
python tools\probe_remote_keys_reach.py              :: 基线 10s（别按）+ 取样 40s（只按遥控器的键）
python tools\probe_remote_keys_reach.py --selftest   :: 只验判读逻辑，不用遥控器也不用按键
```

两段式：先量本底噪声，再让你只按遥控器的键，最后给**三态结论**
（✅ 键进了 Windows / ❌ 一条都没进 / ⚠ 打字太多无法判定）。
⚠ 判读时会**剔掉 scan 码为负的键**（那是合成事件：热键注入的回环、输入法合成的键等）。
判据不是"负号＝我们发的"，而是**遥控器的键一定带真实 scan 码**
（真机实测 `left`=75、`right`=77、`enter`=28 都是正数）—— 所以这个筛子不会漏掉遥控器。
不剔掉就会得出**正好相反**的结论（这个坑 2026-09-23 真踩过：差点把
`f13`(-124)、`reserved `(-252) 当成"遥控器按键进去了"）。

**源码环境下不按键也能先验一遍解码：** `python tools\check_remote_hid.py`
（加 `--watch 20` 再实时听 20 秒按键）。

**要看"某个键的报告落在哪一路"（需要按键）：**

安装版从**开始菜单 →「遥控器诊断」**打开；源码版双击仓库根目录的 `diag-remote.bat`。
它会引导你按 7 段键，然后把结果写成 `%APPDATA%\remote-voice-bridge\remote-diag.txt`
—— 报告开头的【结论 0】是不用按键的硬件身份，末尾的【结论 4】是各集合收到的
原始报告计数。把这个文件发出来就行，不用你描述现象。（跑之前先把主程序退干净。）

源码环境下还可以单独长听：`python tools\watch_reports.py --seconds 60`。

---

## 构建安装包

### 本地构建

```bat
pip install -r requirements.txt -r requirements-build.txt
pyinstaller remote-voice-bridge.spec --noconfirm
iscc installer.iss            :: 需安装 Inno Setup 6
```

想装出与 CI **完全一致**的那一套依赖（传递依赖也钉死、每个 wheel 核 SHA-256）：

```bat
pip install --require-hashes -r requirements.lock.txt
```

锁文件是 `tools\make_deps_lock.py` 生成的（改了 `requirements*.txt` 就重跑一次）；
`python tools\make_deps_lock.py --check` 可以离线核对它有没有漂。

产物：`installer\RemoteVoiceBridge-Setup-1.0.8.exe`

改版本号时记得**两个文件一起改**（`config.py` 的 `APP_VERSION` 和 `installer.iss`
的 `MyAppVersion`），然后跑一次：

```bat
python tools\check_version.py     :: 应输出 OK 1.0.8
python tools\smoke_console.py     :: 应输出 SMOKE OK
```

### GitHub Actions 自动构建（推荐）

推送 tag 即自动在 `windows-latest` 上跑 PyInstaller + Inno Setup，并把
`Setup.exe` 挂到 Release：

```bash
git tag v1.0.8 && git push origin v1.0.8
```

构建前会先校验 `tag` 与 `config.py` 的 `APP_VERSION` 是否一致，不一致会直接失败。

> ⚠️ Actions 产出的 Release **默认是 Draft**，要对外可见需手动改：
> `gh release edit v1.0.8 --draft=false`

也可在 Actions 页面手动 `Run workflow`。

**产物旁边还会附一份 `sbom.cdx.json`**（CycloneDX 软件物料清单）：里面列着这个包
到底装了哪些第三方组件、各是什么版本 —— 不用只信我们的说明文字。
另外每个安装包都带一份**构建证明**，可以自己验"这个 exe 是不是本仓库构建的"：

```bash
gh attestation verify RemoteVoiceBridge-Setup-1.0.23.exe --repo leeuouo100/remote-voice-bridge
```

> ⚠️ **安装包目前没有 Authenticode 代码签名**（仓库里没有证书）。
> 所以 Windows SmartScreen 会提示"未知发布者"，需要点"仍要运行"。
> 构建流程里签名的位置已经留好（有证书就自动生效），见 `CHANGELOG.md` 的 P2-7 一节。
> 构建证明**不能替代**代码签名：前者证明"从哪来"，后者证明"发布者是谁"。

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
remote_hid.py   读遥控器的 HID 厂商自定义页并解码按键（v1.0.11；⚠ 真机实测报告根本没来，见 CHANGELOG v1.0.12）+ 每 20 秒一条「📊 HID 通道审计」日志（自带限流：前 3 次照报、之后每 5 分钟），用来一眼看出报告到底有没有来
frida_hid.py    注入 WUDFHost 读遥控器 HID 报告（v1.0.20，**真机上唯一能拿到按键的那一路**）
frida_tap.js    上面那一路用的 Frida 脚本（机制移植自 VibeMote，MIT）
hidinfo.py      HID 设备/集合枚举（SetupAPI + HidP）
hidwatch.py     打开 HID 集合读原始报告（CreateFile + ReadFile）
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
