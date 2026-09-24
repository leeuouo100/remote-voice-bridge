"""
一次按键，看它落在哪条通道 —— 把**所有可能的路**同时挂上。

## 为什么要做这个（前面查出来的事实）

已经确认的事（见 tools/probe_gatt_hid.py / probe_hid_claim.py / dump_report_descriptor.py）：

  · 遥控器有 5 路 HID 集合（键盘/消费类/鼠标/厂商页 0xFF01/厂商页 0xFF80），
    都在线（DIGCF_PRESENT），厂商页**打得开**，但按遍所有键**一条报告都不来**。
  · 遥控器的 HID 服务 0x1812 被**别人独占**着：`get_characteristics_async`
    一律返回 status=3 (ACCESS_DENIED)，连按 UUID 单独取、from_id_async 重开
    都被拒。占它的不是本程序（本程序只开 ATVV），所以只能是 **Windows 的 HOGP 栈**。
  · 语音键**能用**：走 ATVV CTL 的 0x04 audio_start，每次都到。

于是"按键没反应"只剩两种可能，而它们在 HID 层长得一模一样：

  ① 遥控器把这些按键发在 HID 上，但 Windows 的 HOGP **没把报告送上来**
     （挂起 / 订阅没建立 / 驱动没绑）
  ② 遥控器**根本不往 HID 发**，而是发在别的通道上
     —— 它身上还挂着两个**私有服务**：
        0000ae40  →  ae41 [write-noresp] / ae42 [notify]
        d343bfc0  →  c1..c4 [write] / c5 [notify]      ← 后缀和 ATVV 同厂
     这两个服务**没有**被独占，我们订得上。

## 本脚本要做的事

按一次键，同时记录下面 5 条通道有没有动静：

  ⌨ 键盘钩子（Windows 键盘事件）
  📦 HID 5 路集合的原始报告（含两个厂商页）
  🔵 ae42          私有通知
  🔵 d343bfc5      私有通知
  🟢 ATVV CTL 的**每一个字节**（含认不出的 opcode）

哪一条亮，就说明按键走哪条 —— 这是唯一能把 ①② 分开的办法。

## 用法

    # 1) 先在托盘右键「退出」关掉桥程序（它占着 ATVV，不关就听不到 CTL）
    # 2) 然后：
    python tools/watch_all_channels.py --seconds 45

    关不掉 / 就想带着它测：加 --force（CTL 那条会显示"被占用，跳过"）

不按任何键也能跑 —— 静态部分照样出（服务清单、HID 集合、描述符尝试）。

产出：屏幕实时打印 + %APPDATA%\\remote-voice-bridge\\watch-all-channels.txt
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _utf8 import setup as _setup_utf8  # noqa: E402

_setup_utf8()

import hidinfo      # noqa: E402
import hidwatch     # noqa: E402
from config import APP_VERSION, CONFIG_DIR  # noqa: E402

OUT = CONFIG_DIR / "watch-all-channels.txt"

ATVV_CTL = "ab5e0004-5a21-4f05-bc7d-af01f617b664"
ATVV_AUD = "ab5e0003-5a21-4f05-bc7d-af01f617b664"
# ⚠ 这里以前叫 `SKIP_SERVICES`、里面装的是**特征** UUID，判断时却拿
#   **服务** UUID 去比（`if su in SKIP_SERVICES`）⇒ 永远不成立，音频流特征
#   一直被订上（09-23 的报告里就能看到 `已订阅 ab5e0001/ab5e0003`）。
#   那次没出事只因为当时没有音频流；一旦真有流，它会灌进来海量字节、
#   把按键证据全淹掉。**"跳过名单"和"被跳过的对象"必须是同一层的东西。**
SKIP_CHARS = {
    # 音频流特征：订阅了会灌进来海量字节，淹掉按键证据
    ATVV_AUD,
}
# 阳性对照用的 key：订阅表里的 key 形如「服务前 8 位/特征前 8 位」
CTL_KEY = "ab5e0001/ab5e0004"

# ── 测试注入点（只给 --selftest 用）─────────────────────────────────
# ⚠ 为什么必须是**模块级**变量：`gatt_part` 是 main() 的**局部**函数，
#   所以 `globals()["gatt_part"] = 假货` **一点用都没有** —— Python 解析这个名字时
#   先在 main 的局部作用域里就把它找到了。2026-09-24 实测踩到：
#   自检自称"人为制造 GATT 失败"，实际上**真的 gatt_part 一直在跑**
#   （真枚举、真订阅、还真花了十几秒）—— 一个永远不生效的替身等于没测，
#   那道闸门测的根本不是它声称的东西。
GATT_RUNNER = None
HOGP = "00001812-0000-1000-8000-00805f9b34fb"

# ATVV CTL 上已知的 opcode（用来把"认不出的"标出来）
ATVV_OPS = {
    0x0B: "capabilities", 0x04: "audio_start", 0x00: "audio_stop",
    0x0C: "mic_open_result", 0x08: "start_search", 0x0A: "audio_sync",
}


def bridge_probe() -> tuple:
    """桥程序在不在跑 + **依据**。

    ⚠ 判读私有服务那两路的前提就是这个（桥占着 ATVV）。而 09-23 那份报告
      里**没有这一行** ⇒「桥在跑但它没被检出」与「桥真的没跑」两件事，
      事后完全分不开。所以这里必须把依据也说出来，不能只回一个 bool。
    """
    try:
        p = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq RemoteVoiceBridge.exe", "/NH"],
            capture_output=True, text=True, timeout=6, errors="replace",
            creationflags=0x08000000)
        out = (p.stdout or "").strip()
        if "RemoteVoiceBridge.exe" in out:
            pid = next((t for t in out.split() if t.isdigit()), "?")
            return True, f"在跑（PID≈{pid}）"
        return False, f"没在跑（tasklist 回：{out[:40]!r}）"
    except Exception as e:                              # noqa: BLE001
        return False, f"查不出来：{e.__class__.__name__}: {e}（按「没在跑」继续）"


def bridge_running() -> bool:
    return bridge_probe()[0]


def main(argv: list | None = None) -> int:
    # 默认 90 秒：测 "遥控器按键有没有到 Windows" 要按 11 个键，
    # 45 秒太赶（一边读说明一边按），而窗口不够长时读到的 "0 条"
    # 会被当成"按键没来"—— 那是**测量误差**冒充结论（09-22 就误读过一次）。
    seconds = 90
    # argv 可注入：--selftest 要在同一个进程里跑 main（下面直接调），
    # 不能靠改 sys.argv。可测性也是这次的教训之一。
    a = list(sys.argv[1:] if argv is None else argv)
    for i, x in enumerate(a):
        if x == "--seconds" and i + 1 < len(a):
            try:
                seconds = max(5, min(600, int(a[i + 1])))
            except ValueError:
                pass
    force = "--force" in a
    # 阳性对照的等待上限：这一步要人从"读说明"到"按下去"，给足但别无限等
    # （它占的是窗口之前的时间，等太久会让人以为程序卡住）。
    control_wait = 15.0
    for i, x in enumerate(a):
        if x == "--control-wait" and i + 1 < len(a):
            try:
                control_wait = max(3.0, min(120.0, float(a[i + 1])))
            except ValueError:
                pass

    lines: list[str] = []
    lock = threading.Lock()

    def say(s: str = "") -> None:
        with lock:
            print(s, flush=True)
            lines.append(s)

    say("=" * 78)
    say(" remote-voice-bridge · 全通道监听（一次按键，看它走哪条路）")
    say(f" 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  版本：v{APP_VERSION}")
    # 把「桥在不在跑」和它的依据一起写进报告 —— 上次缺这行，事后分不开。
    _br, _br_why = bridge_probe()
    say(f" 桥程序：{_br_why}")
    say("=" * 78)

    if _br and not force:
        say("\n⛔ 桥程序（RemoteVoiceBridge.exe）在跑，它占着 ATVV，")
        say("   会让我们听不到 CTL 通道。请托盘右键「退出」后重跑；")
        say("   确实要带着它测就加 --force。")
        return 3

    # ── 静态：HID 集合 + 尝试原始描述符 ─────────────────────────────
    say("\n" + "─" * 78)
    say("【静态】遥控器的 HID 集合")
    say("─" * 78)
    cols = [d for d in hidinfo.live_hid_collections() if d.get("is_google")]
    for d in cols:
        say(f"  0x{d['usage_page']:04X}/0x{d['usage']:04X}  in_len={d['in_len']:<3} "
            f"{hidinfo.collection_role(d['usage_page'], d['usage'])[0]}")
    if not cols:
        say("  ⚠ 一个都没枚举到 —— 遥控器可能没连上。")

    # ── 静态：GATT 服务 + HID 服务能否订阅 ──────────────────────────
    static_report: list[str] = []
    live_hits: dict[str, list] = {}
    subs: list[str] = []
    subs_ok: list[str] = []
    stop = threading.Event()

    async def gatt_part() -> None:
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.devices.bluetooth import (
            BluetoothLEDevice, BluetoothConnectionStatus)
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCommunicationStatus, GattClientCharacteristicConfigurationDescriptorValue)

        sel = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
        devs = await DeviceInformation.find_all_async_aqs_filter(sel)
        cands = [d for d in devs
                 if "remote" in (d.name or "").lower()
                 or "chromecast" in (d.name or "").lower()]
        say(f"\n  ▸ 已配对 BLE 设备 {len(devs)} 个，名字像遥控器的 {len(cands)} 个：")
        for d in cands:
            say(f"     · {d.name!r}  id={d.id}")
        if not cands:
            say("❌ 没找到已配对的遥控器（BLE）—— 下面那几路**仍然照测**。")
            return

        # ⚠ 配对列表里**不止一个条目**（一个在用、一个幽灵，见 09-19 的配对代数
        #   记录）。旧代码"名字像就取第一个"⇒ 经常抓到幽灵，报
        #   `[WinError -2147024809] 提供的设备 ID 不是有效的 BluetoothLEDevice 对象`，
        #   而当时的提示文案还把它猜成"蓝牙被桥程序占着"——方向全错。
        #   改成**逐个真打开、按连接状态挑**，并把每个条目的结果打进报告。
        opened: list = []
        for d in cands:
            try:
                b = await BluetoothLEDevice.from_id_async(d.id)
            except Exception as e:                      # noqa: BLE001
                say(f"     ❌ 打开失败（大概率是幽灵条目）："
                    f"{e.__class__.__name__}: {e}")
                continue
            st = b.connection_status
            say(f"     ✅ 打开成功，连接状态={int(st.value)}"
                + ("" if st == BluetoothConnectionStatus.CONNECTED
                   else "  ← 没连上（遥控器在睡，或这条是幽灵）"))
            opened.append((b, st))
        if not opened:
            say("❌ 这些条目一个都打不开 —— GATT 这几路这次测不了。")
            say("   但**下面的实时监听照跑**（键盘钩子 + HID 原始报告流不依赖 GATT）。")
            return
        ble = next((b for b, st in opened
                    if st == BluetoothConnectionStatus.CONNECTED), opened[0][0])
        if ble.connection_status != BluetoothConnectionStatus.CONNECTED:
            say("\n⚠ BLE 未连接，等遥控器醒来（按任意键）最多 10 秒…")
            for _ in range(20):
                await asyncio.sleep(0.5)
                if ble.connection_status == BluetoothConnectionStatus.CONNECTED:
                    break
        say(f"\n{'─' * 78}")
        say(f"【静态】GATT 服务（连接状态={int(ble.connection_status.value)}）")
        say("─" * 78)
        res = await ble.get_gatt_services_async()

        subs: list[tuple[str, object, object]] = []
        loop = asyncio.get_running_loop()

        for svc in res.services:
            su = str(svc.uuid).lower()
            cr = await svc.get_characteristics_async()
            if cr.status != GattCommunicationStatus.SUCCESS:
                why = "ACCESS_DENIED（被别人独占）" if int(cr.status.value) == 3 else str(cr.status)
                say(f"  ▸ {su}  ⚠ 特征枚举被拒：{why}")
                if su == HOGP:
                    say("     └ 这就是 Windows 的 HOGP 栈。它占着服务，")
                    say("       却一条输入报告都没送到 HID 集合上（下面实测）。")
                continue
            notify_chars = []
            for ch in cr.characteristics:
                v = int(getattr(ch.characteristic_properties, "value",
                                ch.characteristic_properties))
                if v & 0x10:
                    notify_chars.append(str(ch.uuid).lower())
            say(f"  ▸ {su}  特征 {len(cr.characteristics)} 个，"
                f"其中可 notify {len(notify_chars)} 个")
            for ch in cr.characteristics:
                cu_skip = str(ch.uuid).lower()
                if cu_skip in SKIP_CHARS:
                    say(f"     ⏭ 跳过 {su[:8]}/{cu_skip[:8]}"
                        f"（音频流特征，订上会淹掉按键证据）")
                    continue
                v = int(getattr(ch.characteristic_properties, "value",
                                ch.characteristic_properties))
                if not (v & 0x10):
                    continue
                cu = str(ch.uuid).lower()
                key = f"{su[:8]}/{cu[:8]}"

                def mk(key, cu, su):
                    def cb(sender, args):
                        try:
                            data = bytes(args.characteristic_value)
                        except Exception:               # noqa: BLE001
                            data = b""
                        tag = key
                        if cu.startswith("00002a4d"):
                            tag = "HID Report 0x2A4D"
                        live_hits.setdefault(tag, []).append((time.time(), data))
                        note = ""
                        if cu == ATVV_CTL and data:
                            op = data[0]
                            note = f"  ← op 0x{op:02X} {ATVV_OPS.get(op, '【认不出】')}"
                        # 走 say（进报告文件），别用 print：不然报告里只有计数、
                        # 没有原始字节，事后没法二次判读。
                        say(f"  🔵 [{time.time() - _t_start:7.2f}s] "
                            f"{tag}  {data.hex(' ')}{note}")
                    return cb

                try:
                    tok = ch.add_value_changed(mk(key, cu, su))
                    await ch.write_client_characteristic_configuration_descriptor_async(
                        GattClientCharacteristicConfigurationDescriptorValue.NOTIFY)
                    subs.append((key, ch, tok))
                    subs_ok.append(key)
                    say(f"     ✅ 已订阅 {key}")
                except Exception as e:              # noqa: BLE001
                    say(f"     ❌ 订阅 {key} 失败：{e.__class__.__name__}: {e}")

        # ⚠⚠ 实时监听窗口**不在这里** —— 它在 main() 的 listen_window()。
        #    2026-09-23 真机上翻过车：窗口原先是写在这个函数体里的，
        #    而本函数**有一条提前 return 的路**（BLE 连不上时，见上面
        #    `from_id_async` 的 except）。于是「遥控器连不上」这一个失败，
        #    就把整个 90 秒窗口一起吞掉了 —— 用户双击 bat 看到的是
        #    「跑一下就跑完了，都没等我按按钮」，报告里也只剩静态段，
        #    看起来像「测了、0 条」，**实际是根本没测**。
        #    ⇒ 铁律：**窗口是主循环的事，不是某一路的从属物**。
        #      键盘钩子与 HID 原始报告流本来就不依赖 GATT，
        #      GATT 挂掉只该让「少几路」，不该让「整场测试消失」。
        for key, ch, tok in subs:
            try:
                ch.remove_value_changed(tok)
            except Exception:                           # noqa: BLE001
                pass

    # HID 集合的原始报告读取（后台线程，和 GATT 并行）
    _t_start = time.time()

    def _on_watch_event(item) -> None:
        # ⚠ 这里以前**只 print、不进报告文件** —— 于是报告里只剩一个干巴巴的
        #   "33"，看不到键名，谁也没法判断那 33 个到底是什么键（09-19 就是这样）。
        ts, _kind, name, scan, injected = item
        tag = {True: "注入", False: "真实", None: "分不出"}[injected]
        say(f"  ⌨ [{ts - _t_start:7.2f}s] 键盘事件[{tag}] "
            f"name={name!r} scan={scan}")

    w = hidwatch.ReportWatcher(only_google=True, on_event=_on_watch_event)
    n_open = w.start(with_hooks=True)
    _hook_kind = ("低级钩子（能分「注入/真实」）" if w._kb_hook is not None
                  else "退路钩子（只有键名，分不出注入）" if w._hooks
                  else "⚠ 没挂上")
    say(f"\n【静态】HID 集合打开 {n_open} 路，键盘钩子：{_hook_kind}")
    if w.hook_error:
        say(f"  ⚠ {w.hook_error}")

    def run_gatt():
        # 走注入点：自检要能真的把 GATT 那一路换掉（见模块顶部 GATT_RUNNER）
        runner = GATT_RUNNER or gatt_part
        try:
            asyncio.run(runner())
        except Exception as e:                          # noqa: BLE001
            say(f"\n❌ GATT 部分异常：{e.__class__.__name__}: {e}")

    th = threading.Thread(target=run_gatt, daemon=True, name="gatt")
    th.start()

    # ── 🎤 阳性对照（2026-09-23 加）─────────────────────────────────────
    # ⚠ 这东西为什么必须有：这案子最反复的失败**不是"没收到"**，而是**收到 0
    #   的时候读不出「是没来」还是「没测」** —— 人没按、按晚了、订阅还没建立、
    #   链路断了，在报告里**长得一模一样**。09-22 把它读成"按键没来"，
    #   09-23 又读成"测过了、0 条"，两次都是**测量误差冒充结论**。
    #
    #   语音键是这个设备上**唯一已知能到**的通道（ATVV CTL op 0x04，桥程序
    #   天天在用）⇒ 拿它当**阳性对照**：
    #     收到了 ⇒ 设备能发、工具在听、你确实按了 ⇒ 后面的 0 才是「真 0」；
    #     没收到 ⇒ 这一轮**没测成**，报告里必须这么写，不许把 0 当结论。
    #   这是本工具第一条**能自证"这次到底测成没测成"**的机制。
    ctl_before = len(live_hits.get(CTL_KEY, []))
    _tw = time.time() + 8.0
    while time.time() < _tw:
        if CTL_KEY in subs_ok:
            break
        # GATT 线程已结束、又一个都没订上 ⇒ 立刻跳过，别白等
        # （--selftest 走的就是这条路：假 gatt 立刻返回）
        if not th.is_alive() and not subs_ok:
            break
        time.sleep(0.2)
    if CTL_KEY not in subs_ok:
        control = "unavailable"
    else:
        print()
        say("=" * 78)
        say(" 🎤 先做一次【链路自检】：请**按一次【语音键】**（麦克风那个键）")
        say("    目的只是证明「设备能发 + 工具在听 + 你确实按了」。")
        say("    收到了就往下走；没收到也照测，但结果只能读成「没测成」。")
        say("=" * 78)
        control = "missed"
        _tc = time.time() + control_wait
        while time.time() < _tc:
            if len(live_hits.get(CTL_KEY, [])) > ctl_before:
                control = "ok"
                break
            print("  ⏳ 等你按语音键… 还剩 %2ds   " % int(_tc - time.time()),
                  end="\r", flush=True)
            time.sleep(0.2)
        say("    → " + ("✅ 收到了 —— 链路是通的，下面那些 0 才算数"
                       if control == "ok" else "❌ 一条都没来（见后面判读）"))

    # ── 实时监听窗口（**主线程，无条件跑满**）────────────────────────────
    # ⚠ 这一段以前长在 gatt_part() 里面 —— BLE 一连不上就跟着被 return 掉，
    #   真机上的表现是「双击 bat，跑一下就结束了，都没等我按按钮」，
    #   而报告里只剩静态段，看上去像「测过、0 条」，**其实是根本没测**。
    #   ⇒ GATT 订得上就多几路证据；**订不上也必须把窗口跑满** ——
    #     键盘钩子和 HID 原始报告流本来就不依赖它。
    say("\n" + "=" * 78)
    say(f" 现在开始 {seconds} 秒实时监听 —— 按**下面出现的提示**按键。")
    say(" 分四段，每段**只按一个键**：确认 → 返回 → 主页 → 方向/音量。")
    say(" （语音键走 ATVV，这里会看到 op 0x04；其它键看落在哪条通道）")
    say("=" * 78)
    # ⚠ 倒计时不是装饰：这一段以前是"打印完立刻开始计时"，
    #   用户还在读说明、手还没伸到遥控器上，窗口已经烧掉一截，
    #   最后看到"0 条"还以为按键没来 —— 其实是**没来得及按**。
    #   这也是本工具上最容易被误读成"结论"的地方（09-22 那次就是这么读的）。
    for n in (3, 2, 1):
        say(f"   ⏳ {n} 秒后开始，把手放到遥控器上…")
        time.sleep(1.0)
    say("   🟢 开始！（下面会实时跳秒，跳满就是结束）")
    # ── 分键提示（2026-09-23 加）────────────────────────────────
    # ⚠ 为什么必须要它：08:53 那份报告里，**唯一能证明「人真按了键」的证据
    #   是两条语音键**（20.6s / 46.0s 各一次 audio_start）。而 确认/返回/主页
    #   到底按下去过没有，**报告里没有任何证据**。
    #   于是「一条都没收到」就有了两种读法：「键没来」还是「他只按了语音键」
    #   —— 分不出来。这和「窗口不够长」是同一类错误：**测量误差冒充结论**。
    #   切成几段、每段只让按一个键之后，报告里带时间戳的那几行就能
    #   直接对上「哪个键、在哪个窗口」，不再需要猜。
    # ⚠ 段长按窗口算：--seconds 改小时自动缩短，不会出现"提示还没说完窗口就结束"。
    #   下界取 5 而不是 8：实测 `--seconds 8` 时 `max(8, 8//4)=8`，四段全挤在
    #   同一秒上（第 0s 提示「确认」、第 8s 才提示「返回」而窗口已经结束）。
    seg = max(5, seconds // 4)
    cues = [
        (0,       "现在**只按【确认 OK】** 2~3 次，然后停手 5 秒"),
        (seg,     "现在**只按【返回 ←】** 2~3 次，然后停手 5 秒"),
        (seg * 2, "现在**只按【主页 ⌂】** 2~3 次，然后停手 5 秒"),
        (seg * 3, "现在按**方向键 / 音量＋ / 音量－**（可选，不作结论）"),
    ]
    _said: set = set()
    t0 = time.time()
    last_tick = -1
    while time.time() - t0 < seconds:
        time.sleep(0.2)
        el = time.time() - t0
        tick = int(el)
        for at, txt in cues:
            if tick >= at and at not in _said:
                _said.add(at)
                # 先把实时那行顶下去，别和提示糊在同一行上
                print()
                # ⚠ 提示行**必须带实际秒数**：报告里的事件行是 `[ 20.64s] 键盘事件…`，
                #   只有提示行也带秒数，"哪一段是按哪个键"才能直接对照。
                #   否则两行都在报告里、却对不上号 —— 又是"顺序耦合看不见"。
                say(f"  ⏱ [第 {tick}s] 👉 {txt}")
                say(f"       （本段到第 {at + seg if at + seg <= seconds else seconds}s 为止）")
        # 每秒刷**一次**（原来是 int(el) % 5 == 0 → 同一秒里刷 5 遍，白屏）
        if tick != last_tick:
            last_tick = tick
            tot = sum(len(v) for v in live_hits.values())
            print(f"  ⏱ 还剩 {seconds - tick:3d}s ｜ 已收到 {tot} 条  "
                  f"（键盘事件会直接打印在下面）", end="\r", flush=True)
    say("")

    # 窗口跑完了才让 GATT 收尾（它要么早已结束，要么在做收尾订阅）
    th.join(timeout=10)
    stop.set()
    time.sleep(0.5)
    w.stop()

    # ── 汇总 ─────────────────────────────────────────────────────────
    say("\n" + "=" * 78)
    say("【结论】各通道收到多少次")
    say("=" * 78)
    real = [e for e in w.key_events if e[4] is False]
    inj = [e for e in w.key_events if e[4] is True]
    unk = [e for e in w.key_events if e[4] is None]
    say(f"  {'通道':<46}{'次数':>6}")
    say("  " + "-" * 60)
    say(f"  {'⌨ 键盘钩子 · 真实（非注入）':<44}{len(real):>6}   ← 只有这行能作证")
    say(f"  {'⌨ 键盘钩子 · 注入（本程序自己发的）':<44}{len(inj):>6}")
    if unk:
        say(f"  {'⌨ 键盘钩子 · 分不出是否注入':<44}{len(unk):>6}")
    _n_ctl = len(live_hits.get(CTL_KEY, []))
    say(f"  {'🎤 阳性对照 · 语音键（ATVV CTL）':<44}{_n_ctl:>6}   "
        + ("← 收到 ⇒ 设备能发/工具在听/你确实按了" if _n_ctl
           else "← 见下面的判读"))
    # ⚠ 这里**必须**把 `c.status` 一起打出来（`hidwatch.summary_lines` 就是这么做的）。
    #   2026-09-23 真机上踩了这个坑：只打数字时，`打开 3 路` 而表里 5 行全 0 ——
    #   读报告的人会把「这一路我们**根本没打开**、什么都没观测到」当成
    #   「这一路观测了、确定 0 条」。同一天刚因为「被 ACL 拒当成空」把根因判反，
    #   报告里就再犯一次同样的错。**读不到 ≠ 没有，表格里也必须分开。**
    for c in w.collections:
        say(f"  {'📦 HID ' + c.key:<44}{len(c.reports):>6}{c.status}")
    unseen = [c for c in w.collections if not c.opened]
    if unseen:
        say("")
        say(f"  ⚠ 上面 {len(unseen)} 路**根本没打开**（`opened=False`）——")
        say("     它们的那个数字要读成「**我们看不见**」，**不是**「没有报告」。")
        for c in unseen:
            say(f"     · {c.key.strip()}：{c.status or c.open_error or '原因未记录'}")
        say("     键盘页打不开属正常（Windows 的 kbdhid 独占它），而且**不影响结论**：")
        say("     那一层由上面的「键盘钩子」覆盖。鼠标页打不开才是真空白。")
    # ⚠ 用 set(subs_ok) | set(live_hits)：**订上了但 0 条**的通道也必须进表。
    #   只列 live_hits 的话，报告里看不到 ae42 / d343bfc5 这两路到底听了没有 ——
    #   09-19 那份报告就是这个形态，事后谁也判断不出「私有服务是 0」还是「没测」。
    for k in sorted(set(subs_ok) | set(live_hits)):
        n_k = len(live_hits.get(k, []))
        mark = "" if n_k else "（已订阅，0 条）"
        say(f"  {'🔵 ' + k + mark:<44}{n_k:>6}")
    if w.hook_error:
        say(f"  （键盘钩子备注：{w.hook_error}）")

    say("")
    # ⚠ 判读**只此一处**（`hidwatch.ReportWatcher.summary_lines`），这里不再自己
    #   抄一份 —— 抄一份的下场就是两边慢慢跑偏。09-19 那份报告里 HID 五路全 0，
    #   结论却写着"按键能到 Windows"：就因为判读用的是 `if kb_n:` 这个**恒真**
    #   条件，把本程序自己注入的语音热键当成了遥控器的按键。
    _sl = w.summary_lines()
    try:
        _i = next(n for n, ln in enumerate(_sl) if ln.startswith("【判读】"))
    except StopIteration:                                   # noqa: BLE001
        _i = 0
    for ln in _sl[_i:]:
        say(ln)

    # ── 🎤 阳性对照的判读 ────────────────────────────────────────────
    #   ✅ 通过：设备能发 + 工具在听 + 人确实按了 ⇒ 上面那些 0 是**真 0**。
    #   ❌ 没通过：**不许**把 0 读成「按键没来」—— 这一轮是「没测成」。
    #   ⚠ 不可用：ATVV 被占着（多半是桥程序在跑）⇒ 只能读成「没观测到」。
    say("")
    if control == "ok":
        say("  → 🎤 阳性对照：**✅ 通过**（语音键的 CTL 通知收到了）")
        say("     · 设备能往这台 PC 发通知、工具在听、你确实按了")
        say("     · ⇒ 上面那些 0 是**真 0**：按键没有到达这台 PC，")
        say("       既不是「没测」，也不是「没来得及按」。")
    elif control == "missed":
        say("  → 🎤 阳性对照：**❌ 没通过**（语音键的 CTL 一条都没来）")
        say("     ⇒ 这一轮**没测成**：可能是你没按/按晚了，")
        say("       也可能是设备真的没发 —— 这两种从报告里分不开。")
        say("     ⚠ **这份报告里的 0 不能读成「按键没来」**，重跑一次，")
        say("       等屏幕出现提示之后再按。")
    else:
        say("  → 🎤 阳性对照：**⚠ 不可用**（ATVV CTL 没订上，多半是桥程序占着）")
        say("     ⇒ 这一轮**无法自证**「测成没测成」：0 只能读成")
        say("       「我们没观测到」，不能读成「按键没来」。")

    if not subs_ok:
        say("")
        say("  ⚠ 这一轮**一个 GATT 特征都没订阅上**（原因见上面）——")
        say("     私有服务 ae42 / d343bfc5 等于**没测**，")
        say("     不能当成「它们也是 0」。带着桥程序跑就属于这种情形。")

    # 私有服务的光是 hidwatch 看不到的（它只管 HID 集合），单独补一段。
    priv = sorted(k for k in live_hits
                  if k.startswith("0000ae40") or k.startswith("d343bfc0"))
    if priv:
        say("")
        say("  → 🎯 **私有服务**上有流量（不是 HID、也不是 ATVV）：")
        for k in priv:
            d = live_hits[k]
            say(f"     · {k} 收到 {len(d)} 条，样本 {d[0][1].hex(' ')}")
        say("     按键很可能走这里。修法：本项目订阅这些私有特征、")
        say("     按它们自己的格式解出按键 —— 改映射表永远碰不到它。")

    text = "\n".join(lines) + "\n"
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        Path(OUT).write_text(text, encoding="utf-8")
        print(f"\n报告已写出：{OUT}")
    except Exception as e:                              # noqa: BLE001
        print(f"\n⚠ 写报告失败：{e}")
    return 0


def selftest() -> int:
    """反例自证：**GATT 挂掉时，监听窗口仍然必须跑满**。

    为什么必须有这条：
      2026-09-23 真机上翻车 —— 窗口原先长在 `gatt_part()` 函数体里，而那个
      函数有一条「BLE 连不上就 return」的路。于是「遥控器连不上」这一个失败，
      把整个 90 秒窗口一起带走了。用户看到的是「双击 bat，跑一下就结束，
      都没等我按按钮」，而报告里只剩静态段 —— 静态段里全是 0，读起来像
      「测过、0 条」，**其实是根本没测**。

    本测试**人为制造当初那个条件**（把 gatt_part 换成立刻返回的假货），
    再断言窗口照样跑满。谁要是把窗口搬回 GATT 里，这里立刻红。
    """
    import contextlib
    import tempfile

    secs = 4
    tmp = Path(tempfile.gettempdir()) / "rvb-watch-selftest.txt"
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass

    globals()["OUT"] = tmp
    globals()["bridge_probe"] = lambda: (False, "selftest：假装没在跑")

    # ⚠ 必须走模块级注入点：`gatt_part` 是 main() 的局部函数，
    #   `globals()["gatt_part"] = ...` 拦不住它（09-24 实测确认）。
    _ran: list = []

    async def _dead_gatt() -> None:
        # 历史条件的复现：BLE 连不上 ⇒ 老代码就在这儿 return 掉了
        _ran.append(1)          # 留下"替身真的跑过"的证据

    globals()["GATT_RUNNER"] = _dead_gatt

    with open(os.devnull, "w", encoding="utf-8") as _null, \
            contextlib.redirect_stdout(_null):
        t0 = time.time()
        rc = main(["--seconds", str(secs)])
        el = time.time() - t0
    txt = tmp.read_text(encoding="utf-8") if tmp.exists() else ""
    globals()["GATT_RUNNER"] = None

    checks = [
        # ⚠ 反例实测过：把窗口循环掐成 `while False:` 之后，**只有下面第二条会红** ——
        #   「🟢 开始」那行打在循环之前，掐掉窗口它照样是绿的。
        #   ⇒ **别把耗时断言当冗余删掉**，它是这条闸门唯一真正起作用的判据。
        ("窗口真的跑起来了（报告里有「🟢 开始」）", "🟢 开始" in txt),
        (f"窗口跑满（{el:.1f}s ≥ {3 + secs}s = 3s 倒计时 + {secs}s 窗口）",
         el >= 3 + secs),
        ("GATT 失败不影响退出码", rc == 0),
        # ⚠ 这条是"闸门的闸门"：替身要是没生效，上面所有断言都是在真 GATT
        #   还活着的情况下过的 —— 等于什么都没证明（09-24 之前就是这样）。
        ("替身真的生效了（假 gatt_part 确实跑过）", bool(_ran)),
        ("报告里有「桥程序：」那行证据", "桥程序：" in txt),
        # 阳性对照是"这次到底测成没测成"的唯一凭据，报告里必须留痕。
        ("报告里有「🎤 阳性对照」的判读", "🎤 阳性对照" in txt),
        # 反例：对照逻辑要是写成"无条件等满 control_wait"，窗口就被拖长了。
        # selftest 里 GATT 是假的 ⇒ 对照必须是"不可用"⇒ 一步都不等。
        (f"阳性对照没拖长窗口（{el:.1f}s ≤ {3 + secs + 8}s）", el <= 3 + secs + 8),
        ("报告跑到底（有【结论】段，不是半路断掉）", "【结论】" in txt),
    ]
    for n, ok in checks:
        print(f"  {'✅' if ok else '❌'} {n}")
    bad = [n for n, ok in checks if not ok]
    if bad:
        print(f"\nSELFTEST FAIL：{len(bad)} 项没过 —— 监听窗口又被 GATT 的成败绑住了？")
        return 1
    print("\nSELFTEST PASS —— GATT 挂掉时监听窗口仍跑满")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    sys.exit(main())
