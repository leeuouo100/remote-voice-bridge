"""厂商页按键解码自检 —— 不按键也能跑的那部分。

为什么单独拆一个脚本
--------------------
「遥控器按键没反应」有四种修法完全不同的成因（报告没来 / 落在厂商页 /
落在鼠标页 / 真的到了键盘层），在日志上长得一模一样。这个脚本把**能脱离
硬件验证的那一半**（解码表、报告格式、集合能不能打开）先钉死，
真机上按下键之后再去看日志，就能只对焦"报告到底来没来"这一件事。

用法
----
    python tools/check_remote_hid.py               # 只跑纯解码单测，不需要遥控器
    python tools/check_remote_hid.py --watch 20    # 再开 20 秒厂商页，实时打印按键
    python tools/check_remote_hid.py --list        # 只列当前 HID 集合

退出码 0 = 通过；1 = 失败。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _decode_cases() -> list[tuple[bytes, tuple[str | None, bool], str]]:
    """（原始报告, 期望的 (按钮, 是否按下), 说明）"""
    return [
        (bytes.fromhex("0103"), ("up", True),       "方向上"),
        (bytes.fromhex("0104"), ("down", True),     "方向下"),
        (bytes.fromhex("0105"), ("left", True),     "方向左"),
        (bytes.fromhex("0106"), ("right", True),    "方向右"),
        (bytes.fromhex("0107"), ("ok", True),       "确认"),
        (bytes.fromhex("010b"), ("back", True),     "返回"),
        (bytes.fromhex("010a"), ("home", True),     "Home"),
        (bytes.fromhex("0108"), ("mute", True),     "静音"),
        (bytes.fromhex("010c"), ("vol_up", True),   "音量＋"),
        (bytes.fromhex("010d"), ("vol_down", True), "音量－"),
        (bytes.fromhex("010e"), ("youtube", True),  "YouTube"),
        (bytes.fromhex("010f"), ("netflix", True),  "Netflix"),
        (bytes.fromhex("0101"), ("power", True),    "电源"),
        (bytes.fromhex("0111"), ("input", True),    "信源"),
        (bytes.fromhex("0100"), ("<up>", False),    "松手（具体松开谁由调用方补）"),
        (bytes.fromhex("0199"), (None, False),      "不认识的用法码 → 忽略"),
        (b"",                   (None, False),      "空报告 → 忽略"),
    ]


def run_decode_tests() -> bool:
    from config import CHROMECAST_BUTTONS
    import remote_hid

    print("── 1. 解码表 ──")
    tbl = remote_hid.USAGE_TO_BUTTON
    print(f"   共 {len(tbl)} 个按键来自厂商页（CHROMECAST_BUTTONS 共 "
          f"{len(CHROMECAST_BUTTONS)} 个）")
    for usage in sorted(tbl):
        btn = tbl[usage]
        print(f"     0x{usage:02X} → {btn:<9} {CHROMECAST_BUTTONS[btn]['label']}")

    missing = [k for k, v in CHROMECAST_BUTTONS.items()
               if isinstance(v["usage"], int) and k not in tbl.values()]
    if missing:
        print(f"   ❌ 这些 int-usage 的按键没进解码表：{missing}")
        return False
    if "voice" in tbl.values():
        # 语音键走 ATVV 的 BLE 数据通道，厂商页那路上不该有它 ——
        # 混进来就是"按语音键触发两次"。
        print("   ❌ 解码表里不该出现语音键（它走 ATVV，不走厂商页）")
        return False
    print("   OK 语音键不在厂商页表内（不会和 ATVV 抢）")

    print("\n── 2. 报告解码 ──")
    bad = 0
    for raw, expect, note in _decode_cases():
        got = remote_hid.decode_report(raw)
        ok = got == expect
        bad += 0 if ok else 1
        print(f"   {'OK ' if ok else '❌ '} {raw.hex(' ') or '(空)':<8} → "
              f"{str(got):<22} {note}")
    if bad:
        print(f"   ❌ {bad} 条解码不符预期")
        return False
    print("   OK 全部符合预期")

    print("\n── 3. 「松手」用上一个按下的键补齐 ──")
    # decode_report 对松手只能返回 <up>，真正松开哪个键是 RemoteHidButtons 用
    # _last_down 补的。这里单独验证那段状态机，它是 PTT 能不能松开的关键
    # （漏掉就会 Ctrl/Win 永远卡在按下状态）。
    hits: list[tuple[str, bool]] = []

    class _Rec:
        def __call__(self, btn, down):
            hits.append((btn, down))

    rb = remote_hid.RemoteHidButtons(_Rec())
    for raw in (bytes.fromhex("0108"), bytes.fromhex("0100"),
                bytes.fromhex("0199"), bytes.fromhex("0100")):
        rb._handle(raw)
    want = [("mute", True), ("mute", False)]
    if hits != want:
        print(f"   ❌ 期望 {want}，实际 {hits}")
        return False
    print(f"   OK 按下 → 松手配对正确：{hits}")
    return True


def run_audit_throttle_tests() -> bool:
    """通道审计的**限流**逻辑 —— 用假时钟真跑，不看源码。

    为什么值得单测：审计行是"按键没反应"唯一的判决书，但判决书每 20 秒念一次
    就会把日志淹掉（一天 4300+ 行重复），反而找不到真正有用的那几行。
    这里钉死两件事：
      ① 前几次必须照报（不然刚启动排查时什么都看不到）；
      ② 之后必须是限流过的（不然就是刷屏）。
    两个方向都要卡，只卡一个就会走向另一种极端。
    """
    import remote_hid

    class _FakeCol:
        key = "vid=18D1 pid=9450 page=0xFF01 usage=0x0001"
        is_vendor = True

    class _FakeLog:
        def __init__(self) -> None:
            # ⚠ 计数属性不能叫 warn / info —— 那会把同名方法在实例上盖掉，
            #   报错是 "'int' object is not callable"，看着像 logger 的毛病。
            self.n_warn = 0
            self.n_info = 0

        def warning(self, msg, *a):                 # noqa: A003
            self.n_warn += 1

        def info(self, msg, *a):
            self.n_info += 1

        def exception(self, *a, **k):
            pass

    orig_log = remote_hid.logger
    orig_loud = remote_hid._AUDIT_LOUD_TIMES
    orig_quiet = remote_hid._AUDIT_QUIET_SEC
    ok = True
    try:
        rb = remote_hid.RemoteHidButtons(lambda *a: None)
        rb._cols = [_FakeCol()]

        # ① 一条报告都没有：10 分钟内每 20 秒调一次＝30 次
        lg = _FakeLog()
        remote_hid.logger = lg
        for i in range(30):
            rb._audit(1000.0 + i * 20.0)
        print(f"   无报告时 30 次审计 → 告警 {lg.n_warn} 条")
        if lg.n_warn < remote_hid._AUDIT_LOUD_TIMES:
            print("   ❌ 前几次的告警被压掉了（刚启动排查时什么都看不到）")
            ok = False
        elif lg.n_warn > remote_hid._AUDIT_LOUD_TIMES + 2:
            print("   ❌ 告警没有限流（这就是「一天 4300 行重复」那个刷屏坑）")
            ok = False
        else:
            print("   OK 前几次照报、之后限流")

        # ② 有报告但条数不变：也不许每 20 秒打一次明细
        lg2 = _FakeLog()
        remote_hid.logger = lg2
        rb._counts = {"vid=18D1 pid=9450 page=0xFF01 usage=0x0001": 7}
        for i in range(30):
            rb._audit(2000.0 + i * 20.0)
        print(f"   条数不变 30 次审计 → 明细 {lg2.n_info} 条")
        if lg2.n_info == 0:
            print("   ❌ 有报告却一条明细都不打（判决书没了）")
            ok = False
        elif lg2.n_info > 4:
            print("   ❌ 条数没变还一直在打明细")
            ok = False
        else:
            print("   OK 明细只在条数变化 / 心跳周期时打")

        # ③ 反例：把限流参数拆掉，上面两条必须变红
        remote_hid._AUDIT_LOUD_TIMES = 10 ** 9
        lg3 = _FakeLog()
        remote_hid.logger = lg3
        rb2 = remote_hid.RemoteHidButtons(lambda *a: None)
        rb2._cols = [_FakeCol()]
        for i in range(30):
            rb2._audit(3000.0 + i * 20.0)
        if lg3.n_warn >= 30:
            print(f"   OK [反例] 去掉「前几次之后压静默」→ 30 次全报（{lg3.n_warn} 条），"
                  "证明上面卡的不是空气")
        else:
            print(f"   ❌ [反例] 去掉限流后仍然只报 {lg3.n_warn} 条 → 说明检查无效")
            ok = False

        remote_hid._AUDIT_LOUD_TIMES = orig_loud
        remote_hid._AUDIT_QUIET_SEC = 0.0
        lg4 = _FakeLog()
        remote_hid.logger = lg4
        rb3 = remote_hid.RemoteHidButtons(lambda *a: None)
        rb3._cols = [_FakeCol()]
        rb3._counts = {"x": 7}
        for i in range(30):
            rb3._audit(4000.0 + i * 20.0)
        remote_hid._AUDIT_QUIET_SEC = orig_quiet
        if lg4.n_info >= 30:
            print(f"   OK [反例] 去掉静默窗口 → 条数不变也每 20 秒打（{lg4.n_info} 条）")
        else:
            print(f"   ❌ [反例] 去掉静默窗口后仍只打 {lg4.n_info} 条 → 说明检查无效")
            ok = False
    finally:
        remote_hid.logger = orig_log
        remote_hid._AUDIT_LOUD_TIMES = orig_loud
        remote_hid._AUDIT_QUIET_SEC = orig_quiet
    return ok


def list_collections() -> None:
    import hidinfo
    print("── 当前 HID 集合 ──")
    try:
        cols = hidinfo.live_hid_collections()
    except Exception as e:                            # noqa: BLE001
        print(f"   ❌ 枚举失败：{e}")
        return
    if not cols:
        print("   （一个都没有）")
    for d in cols:
        tag = ""
        if d.get("is_google"):
            tag = "  ← Google 遥控器"
            if int(d.get("usage_page") or 0) >= 0xFF00:
                tag += "（厂商页，本模块接管）"
        print(f"   vid={d['vid'] or 0:04X} pid={d['pid'] or 0:04X} "
              f"page=0x{d['usage_page'] or 0:04X} usage=0x{d['usage'] or 0:04X} "
              f"in_len={d['in_len']}{tag}")


def watch(seconds: float) -> bool:
    import remote_hid

    print(f"\n── 4. 实时监听厂商页 {seconds:.0f} 秒（请按遥控器上的键）──")
    seen: list[tuple[float, str, bool]] = []

    def _on(btn: str, down: bool) -> None:
        seen.append((time.time(), btn, down))
        print(f"   🔘 {btn:<9} {'按下' if down else '松开'}")

    rb = remote_hid.RemoteHidButtons(_on)
    n = rb.start()
    if not n:
        print("   ❌ 一路厂商页集合都没打开 —— 按键不会生效。")
        print("      常见原因：遥控器没连上 / 没配对 / VID 不是 18D1")
        print("      （语音键走 ATVV，不受影响）")
        return False
    print(f"   ✅ 已接管 {n} 路厂商页集合")
    try:
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        rb.stop()
    if not seen:
        print(f"   ⚠ {seconds:.0f} 秒内没收到任何按键报告。")
        print("     两种可能：① 这段时间内确实没按键；② 报告根本没来。")
        print("     请重跑一次并在倒计时内按几个键；仍无输出就把这段发出来。")
    else:
        print(f"   OK 收到 {len(seen)} 次按键")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="厂商页按键解码自检")
    ap.add_argument("--watch", type=float, metavar="SEC",
                    help="额外开 N 秒厂商页实时监听（需要遥控器在线）")
    ap.add_argument("--list", action="store_true", help="只列 HID 集合")
    args = ap.parse_args()

    print("=" * 60)
    print("remote_hid 自检 —— 遥控器厂商页按键解码")
    print("=" * 60)

    if args.list:
        list_collections()
        return 0

    ok = run_decode_tests()
    ok = run_audit_throttle_tests() and ok
    list_collections()
    if args.watch:
        ok = watch(args.watch) and ok

    print("\n" + ("✅ 通过" if ok else "❌ 失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
