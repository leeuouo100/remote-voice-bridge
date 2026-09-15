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
run_bridge = code_only(func_body(MAIN, "run_bridge"))

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
A("get_default_async" in open_ble,
  "会查当前生效的无线电地址")
A(re.search(r"local_addr\s*!=\s*now_addr", open_ble) is not None,
  "会比对「配对记录绑的地址」与「当前无线电地址」并据此给结论")
A("重新配对" in open_ble and "删除" in open_ble,
  "给出了可执行的处置（删除设备 → 重新配对）")
A("last_event" in open_ble,
  "把失败原因写进 state，好让托盘/控制台说出来")

# ── 3) 托盘状态不能骗人 ──────────────────────────────────────────────────────
A("last_event" in status,
  "托盘状态优先显示具体原因，而不是无条件的「按遥控器任意键唤醒」")
A("s.connected" in status and "s.streaming" in status,
  "托盘状态仍然区分 已连接 / 语音中")

# ── 4) 写了必须接上（v1.0.7 同类事故：防御机制写着但没在所有路径生效）──────────
A(re.search(r"_open_ble_device\(dev_info\)", run_bridge) is not None,
  "run_bridge 真的调用了 _open_ble_device（而不是绕过它自己 from_id_async）")
A("BluetoothLEDevice.from_id_async(dev_info.id)" not in run_bridge,
  "run_bridge 里不再有裸的 from_id_async 调用（避免绕过诊断）")

# ── 反例 1：把 exc_info=True 拿掉，检查项必须翻红 ─────────────────────────────
mut = worker.replace("exc_info=True", "")
A("exc_info=True" not in mut,
  "[反例] 去掉 exc_info=True 后，本闸应当能发现")

# ── 反例 2：把地址比对拿掉，检查项必须翻红 ───────────────────────────────────
mut2 = open_ble.replace("local_addr != now_addr", "False")
A(re.search(r"local_addr\s*!=\s*now_addr", mut2) is None,
  "[反例] 去掉地址比对后，本闸应当能发现")

# ── 反例 3：把 run_bridge 里的调用换成裸调用，检查项必须翻红 ─────────────────
mut3 = run_bridge.replace("_open_ble_device(dev_info)", "BluetoothLEDevice.from_id_async(dev_info.id)")
A(re.search(r"_open_ble_device\(dev_info\)", mut3) is None
  and "BluetoothLEDevice.from_id_async(dev_info.id)" in mut3,
  "[反例] 绕过 _open_ble_device 后，本闸应当能发现")

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
