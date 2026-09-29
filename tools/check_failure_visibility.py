#!/usr/bin/env python3
"""回归闸：故障必须**可见**，且必须**说人话**。

这个项目栽过好几类"静默"事故，v1.0.9 又栽了一类新的：

  tray_app._bridge_worker 里用 `print(traceback)` 报桥线程的异常，
  但主 exe 是 console=False（PyInstaller 不分配控制台窗口）→ print 的流向是空的。
  结果：桥每 3 秒崩一次、bridge.log 里一行报错都没有、Windows 事件日志也查不到
  （Python 层异常不是进程崩溃），托盘还写着「未连接（按遥控器任意键唤醒）」——
  三件事叠在一起，把一个 `OSError: E_INVALIDARG` 捂了两个小时。

所以这里用**静态 + 反例**把三件事钉死（纯读源码，几毫秒，不需要真机）：

  1) 桥线程的异常必须走 logging（exc_info=True），不能靠 print
  2) BLE 连接失败必须有"为什么 + 怎么办"，且不能只试一条路
  3) 托盘状态必须能反映具体原因，不能无条件说"按遥控器任意键唤醒"
  4) `_open_ble_device` 必须真的被 run_bridge 调用（写了没接上 = 等于没写）
  5) **作废的配对记录必须在选设备那一步就被排掉**（v1.0.10 加）

第 5 条是同一个家系里最容易漏的一条：诊断写在 `_open_ble_device` 里，
但**选设备**发生在更前面的 `find_remote`。只在连接那一步报错的话，
每轮重连还是会拿那张废记录去撞一次墙 —— 日志刷屏、结论全无
（这正是 v1.0.9 花掉两小时的原因之一）。所以两处都要落地。

反例自检的写法沿用 check_hidinfo.py：把源码**改坏**，断言检查项必须翻红。
一个"永远绿"的闸比没有闸更危险。
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _utf8 import setup as _setup_utf8  # noqa: E402

_setup_utf8()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

checks: list[tuple[bool, str]] = []


def A(ok, msg):
    checks.append((bool(ok), msg))


def read(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8", errors="replace") as f:
        return f.read()


def func_body(src: str, name: str) -> str:
    """抠出顶层函数的函数体（到下一个顶层 def/class/@ 为止）。"""
    m = re.search(rf"^(?:async )?def {re.escape(name)}\(", src, re.M)
    if not m:
        return ""
    rest = src[m.end():]
    nxt = re.search(r"^(?:async )?def |^class |^@", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def code_only(src: str) -> str:
    """只留代码，去掉注释。

    为什么必须先剥注释：本闸有一项是"不许再出现 traceback.print_exc()"，
    而 `_bridge_worker` 的注释里**恰好要讲清楚这个旧写法错在哪**。
    不剥注释的话，一段说人话的解释会把闸吓红 —— 那下次就会有人去删注释，
    而不是去修代码。反例自检同理，必须作用在剥完注释的文本上。
    """
    out = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        i = line.find("  #")
        out.append(line[:i] if i >= 0 else line)
    return "\n".join(out)


TRAY = read("tray_app.py")
MAIN = read("main.py")

worker = code_only(func_body(TRAY, "_bridge_worker"))
status = code_only(func_body(TRAY, "_status_text"))
open_ble = code_only(func_body(MAIN, "_open_ble_device"))
# ⚠ 2026-09-29（P1-3）：`run_bridge` 已拆成「壳 + 真身」。
#   真身是 `_run_bridge_inner`，连接动作在里面；壳只负责收尾。
#   查壳 = 查了个空壳（那一项永远红，或者更糟：以后有人把检查删掉）。
#   所以实体定位指 `_run_bridge_inner`，壳另用一条断言盯它别自己绕过诊断。
bridge_body = code_only(func_body(MAIN, "_run_bridge_inner"))
bridge_shell = code_only(func_body(MAIN, "run_bridge"))
find_remote = code_only(func_body(MAIN, "find_remote"))
live_addr = code_only(func_body(MAIN, "_live_adapter_addr"))
pick_live = code_only(func_body(MAIN, "_pick_live_devices"))
hint = code_only(func_body(MAIN, "_repair_hint"))

# ── 1) 桥线程异常必须可见 ────────────────────────────────────────────────────
A(worker, "tray_app._bridge_worker 存在")
A("exc_info=True" in worker,
  "桥线程异常带 traceback 写进 log（exc_info=True）")
A("traceback.print_exc()" not in worker,
  "桥线程不再用 traceback.print_exc()（console=False 时输出流向是空的）")
A(re.search(r"\blog(?:ger)?\.error\(", worker) is not None,
  "桥线程异常走 logging，而不是 print")

# ── 2) 连接失败要能说清楚 + 不只试一条路 ─────────────────────────────────────
A(open_ble, "main._open_ble_device 存在")
A("from_bluetooth_address_async" in open_ble,
  "设备 ID 连不上时有备用路径（按远端 MAC）")
# v1.0.10 把"查活地址"抽成了 _live_adapter_addr（三处都要用：选设备、连不上时、
# 修复工具）。所以改判"那个助手真的去问了适配器 + 这里真的调了它"。
A("get_default_async" in live_addr or "bluetooth_address" in live_addr,
  "会查当前生效的无线电地址（_live_adapter_addr）")
A("_live_adapter_addr()" in open_ble,
  "_open_ble_device 真的用了它（而不是自己另写一套）")
A(re.search(r"local_addr\s*!=\s*now_addr", open_ble) is not None,
  "会比对「配对记录绑的地址」与「当前无线电地址」并据此给结论")
A("处置" in open_ble and "_repair_hint()" in open_ble,
  "给出了可执行的处置（且指向修复入口，而不是让用户自己删设备）")
A("修复蓝牙配对" in hint and "pairing.py" in hint,
  "_repair_hint 同时给出安装版与源码版两条路")
A("last_event" in open_ble,
  "把失败原因写进 state，好让托盘/控制台说出来")

# ── 3) 托盘状态不能骗人 ──────────────────────────────────────────────────────
A("last_event" in status,
  "托盘状态优先显示具体原因，而不是无条件的「按遥控器任意键唤醒」")
A("s.connected" in status and "s.streaming" in status,
  "托盘状态仍然区分 已连接 / 语音中")

# ── 4) 写了必须接上（v1.0.7 同类事故：防御机制写着但没在所有路径生效）──────────
A(bridge_body, "main._run_bridge_inner 存在（真身，不是那层壳）")
A(re.search(r"_open_ble_device\(dev_info\)", bridge_body) is not None,
  "_run_bridge_inner 真的调用了 _open_ble_device（而不是绕过它自己 from_id_async）")
A("BluetoothLEDevice.from_id_async(dev_info.id)" not in bridge_body,
  "_run_bridge_inner 里不再有裸的 from_id_async 调用（避免绕过诊断）")
# 壳也不能自己绕过诊断去连 —— 壳里出现裸调用说明有人把连接又抄了一份出去。
A("BluetoothLEDevice.from_id_async(dev_info.id)" not in bridge_shell,
  "壳 run_bridge 里也没有裸的 from_id_async（壳不许自己另开一条连接路）")

# ── 5) 作废记录必须在**选设备**那一步就排掉（v1.0.10）─────────────────────────
# 同一个家系里最容易漏的一条：诊断写在连接处，但选设备在更前面。
# 只在连接处报错的话，每轮重连仍要拿废记录撞一次墙。
A(pick_live, "main._pick_live_devices 存在")
A(re.search(r"local\s*==\s*now_addr|local\s*!=\s*now_addr", pick_live) is not None,
  "_pick_live_devices 真的按地址比对来分流")
A("_pick_live_devices(" in find_remote,
  "find_remote 一开始就把作废候选挑出去（不是等连接时才报错）")
A("state.update" in find_remote and "last_event" in find_remote,
  "候选全是作废的会写进 state（托盘能说人话）")
A("_repair_hint()" in find_remote,
  "并且直接告诉用户点哪（修复入口）")

# ── 反例 1：把 exc_info=True 拿掉，检查项必须翻红 ─────────────────────────────
mut = worker.replace("exc_info=True", "")
A("exc_info=True" not in mut,
  "[反例] 去掉 exc_info=True 后，本闸应当能发现")

# ── 反例 2：把地址比对拿掉，检查项必须翻红 ───────────────────────────────────
mut2 = open_ble.replace("local_addr != now_addr", "False")
A(re.search(r"local_addr\s*!=\s*now_addr", mut2) is None,
  "[反例] 去掉地址比对后，本闸应当能发现")

# ── 反例 3：把真身里的调用换成裸调用，检查项必须翻红 ─────────────────────────
mut3 = bridge_body.replace("_open_ble_device(dev_info)", "BluetoothLEDevice.from_id_async(dev_info.id)")
A(re.search(r"_open_ble_device\(dev_info\)", mut3) is None
  and "BluetoothLEDevice.from_id_async(dev_info.id)" in mut3,
  "[反例] 绕过 _open_ble_device 后，本闸应当能发现")

# ── 反例 4：把 find_remote 里的分流拿掉，检查项必须翻红 ───────────────────────
mut4 = find_remote.replace("_pick_live_devices(devices, now_addr)", "devices, stale = devices, []")
A("_pick_live_devices(" not in mut4,
  "[反例] find_remote 不再分流作废候选时，本闸应当能发现")

# ── 6) 子进程的输出不许"解不出来就当没有"（2026-09-29）────────────────────────
# `subprocess.run(..., text=True)` 是按 **locale 编码**解子进程输出的，而
# Windows 自带工具（tasklist / powershell）按**控制台代码页**吐字节 ——
# 两者不一定一致。实测（2026-09-29）：环境里带 PYTHONUTF8=1 时 locale 是
# utf-8、而 tasklist 吐的是 GBK，于是 subprocess 的**读取线程**抛
# `UnicodeDecodeError`，主流程只看到 `p.stdout` 是空的。
#
# 后果不是"崩了"，是**静默给出错误答案**：
#   `if "RemoteVoiceBridge.exe" in (p.stdout or "")` ⇒ 判成"桥程序没在跑"，
#   而它其实正在跑 —— 于是体检报告写成"遥控器各键 0 次"，看着像遥控器坏了。
# 这正是本文件开头那件事的同一个家系，所以钉在这里。
#
# 规则：凡是 `text=True`，必须同时带 `encoding=` 或 `errors="replace"`。
import ast
import glob


def text_calls_without_errors(src: str) -> list[int]:
    """返回「`text=True` 却没带 `encoding=` / `errors=`」的行号。

    ⚠ 用 ast 而不是正则：这些调用几乎都是**多行**写的，正则很难判断
    "这个 `errors=` 到底属不属于这一次调用"（窗口式匹配会假绿或假红）。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                and f.value.id == "subprocess"):
            continue
        if f.attr not in ("run", "Popen", "check_output", "check_call", "call"):
            continue
        kw = {k.arg for k in node.keywords if k.arg}
        text_on = any(
            k.arg in ("text", "universal_newlines")
            and isinstance(k.value, ast.Constant) and k.value.value is True
            for k in node.keywords)
        if text_on and "encoding" not in kw and "errors" not in kw:
            out.append(node.lineno)
    return out


_sources: dict[str, str] = {}
for _name in ("main.py", "pairing.py", "console_server.py", "tray_app.py",
              "hidwatch.py", "remote_hid.py", "frida_hid.py"):
    _p = os.path.join(ROOT, _name)
    if os.path.exists(_p):
        _sources[_name] = open(_p, encoding="utf-8", errors="replace").read()
for _p in sorted(glob.glob(os.path.join(ROOT, "tools", "*.py"))):
    _b = os.path.basename(_p)
    if not _b.startswith("_"):
        _sources[f"tools/{_b}"] = open(_p, encoding="utf-8", errors="replace").read()

_offenders: list[str] = []
_n_text = 0
for _name, _src in _sources.items():
    _n_text += _src.count("text=True")
    for _ln in text_calls_without_errors(_src):
        _offenders.append(f"{_name}:{_ln}")

# 判据不是空转：扫描面里确实有这类调用（否则上面那条会**永远绿**）
A(_n_text >= 5, f"扫描面覆盖到 {_n_text} 处 `text=True` 的子进程调用")
A(not _offenders,
  f"所有 `text=True` 的子进程调用都带了 `encoding=` 或 `errors=`"
  f"（越界：{_offenders or '无'}）—— 少了它，解码失败会抛在读取线程里，"
  f"主流程只看到空输出，于是**静默给出错误答案**")

# ── 反例 5：text=True 不带 errors= 必须被报出，带了必须放过 ─────────────────
_bad_src = ('import subprocess\n'
            'subprocess.run(["tasklist"], capture_output=True, text=True)\n')
_good_src = ('import subprocess\n'
             'subprocess.run(["tasklist"], capture_output=True, text=True,\n'
             '               errors="replace")\n')
_good_enc = ('import subprocess\n'
             'subprocess.run(["x"], capture_output=True, text=True,\n'
             '               encoding="utf-8")\n')
A(text_calls_without_errors(_bad_src) == [2],
  "[反例] `text=True` 不带 errors= ⇒ 报出行号 2")
A(not text_calls_without_errors(_good_src)
  and not text_calls_without_errors(_good_enc),
  "[反例] 带了 `errors=` 或 `encoding=` ⇒ 必须放过（多行写法也要认）")

# ── 7) 日志不许把**正常现象**说成故障（2026-09-30 真机）─────────────────────
#
# 这一类比"漏报"更阴：它不是没说话，而是**说错话**。用户（和下一个排查的人）
# 会照着那句话去修一个不存在的问题，而真正的问题被埋在噪声里。
# 2026-09-30 真机日志里两条实证：
#
#   ① `🔘 HID 按键 'w'（scan=17） → 没有对应按钮，已忽略（若这是遥控器上的键，
#      请到控制台给它指定动作）` —— 13 条里 11 条其实是**物理键盘敲的字母**
#      （同一段代码的注释就写着「物理键盘敲字母也走这里」）。用户第一反应是
#      「遥控器在乱发键」。
#   ② `⚠ 遥控器还在推流，但程序这边没有会话 → 补发 MIC_CLOSE 让它停
#      （正常情况下不该出现…）` —— 每次正常收尾都响，因为遥控器**在途的帧**
#      （0.16 秒内）会命中它。真出事（多推 52 秒那次）时没人会再看这句。
#
# 判据：文案必须**只说它知道的事**，并且**给出去查的条件**。


def key_log_is_honest(text: str) -> tuple[bool, bool, bool]:
    """① 认不出的按键那条文案：改了说法 / 说清来源 + 给出条件 / 旧误导句没了。

    ⚠ 传进来的必须是**剥过注释**的文本（`code_only`）。这一段注释里**故意**
      逐字引用了旧文案来交代它错在哪 —— 不剥注释的话，一段说人话的解释会把
      判据吓红，下次就会有人去删注释而不是去修代码（这个文件开头那段
      `code_only` 的 docstring 讲的就是同一个坑，这里又踩了一次）。
    """
    return (
        "🔘 程序看到一个键" in text,
        ("这**不是**故障：物理键盘敲的字母" in text
         and "只有当你**正在按遥控器上的某个键**时它才出现" in text),
        "没有对应按钮，已忽略" not in text,
    )


def first_frame_has_lag(text: str) -> bool:
    """② 首个音频帧那行必须带「距会话开始 X ms」。

    用户对"语音输入好不好用"最直接的感受就是这个数，而真机实测快慢差 5 倍
    （0.3s vs 1.7s）—— 不打出来就只能靠人数时间戳。
    """
    return "距会话开始" in text


_main = code_only(read("main.py"))
_new_ok, _cond_ok, _old_gone = key_log_is_honest(_main)
A(_new_ok, "⑦ 认不出的按键那条日志改了文案：不再写像故障的话")
A(_cond_ok,
  "⑦ 那条文案必须点明**物理键盘 / 本程序自己注入的键也会走到这条路**，"
  "并给出「什么条件下才该怀疑遥控器」—— 不说清的话，用户会把物理键盘敲的"
  "字母读成「遥控器在乱发键」")
A(_old_gone, "⑦ 旧那句误导文案不许留着 —— 它就是误读源")
A(first_frame_has_lag(_main),
  "⑦ 首个音频帧那行带上了「距会话开始 X ms」（把「感觉还行」变成可核对数字）")

# ── 反例 6/7：改回误导版 / 拿掉耗时，判据必须翻红（走同一条判据函数）────────
_misleading = _main.replace("这**不是**故障：物理键盘敲的字母",
                            "若这是遥控器上的键", 1)
A(_misleading != _main, "[反例] 能把那条文案改回误导版（锚点存在）")
A(key_log_is_honest(_main)[1] and not key_log_is_honest(_misleading)[1],
  "[反例] 改回误导版 ⇒ ⑦ 那条判不合格")

_no_lag = _main.replace("（距会话开始 {_lag * 1000:.0f} ms）", "", 1)
A(_no_lag != _main, "[反例] 找得到那行耗时（锚点存在）")
A(first_frame_has_lag(_main) and not first_frame_has_lag(_no_lag),
  "[反例] 把「距会话开始」拿掉 ⇒ ⑦ 判不合格")

# ── 汇总 ────────────────────────────────────────────────────────────────────
fails = 0
for ok, msg in checks:
    print(("  ✅ " if ok else "  ❌ ") + msg)
    if not ok:
        fails += 1

print()
if fails:
    print(f"{fails} 项未通过 —— 故障可见性/可诊断性被削弱了")
    sys.exit(1)
print(f"故障可见性自检通过（{len(checks)} 项）")
