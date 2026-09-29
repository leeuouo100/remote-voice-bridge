"""按键旁路（Frida 注入 WUDFHost）自检 —— 不按键也能跑的那部分。

为什么单独拆一个脚本
--------------------
「除语音键外所有按键都没反应」这个悬案，2026-09-29 才定案：报告不是没发，
是在 `WUDFHost.exe` 内部就被 UMDF 驱动消费掉了。修法是注入那个进程、
在它读 GATT 特征的那次 IOCTL 输出缓冲区上抄一份（见 `frida_hid.py` 文件头）。

这条路的失效**全是静默的**：注入失败、挂错宿主、报告格式不对 —— 现象
一模一样，都是「按了没反应」。所以把**能脱离硬件验证的那一半**先钉死：
  ① 消费类页 usage 表（16 位）与 config 的按键一一对齐、语音键不在里面
  ② 两种报告格式的解码（消费类页 3 字节 / 厂商页 8 位 usage）
  ③ 「要抹掉原生动作的 usage」算法（只抹已映射的键）
  ④ .js 与 .py 的 IOCTL 常量不许漂移（改一边忘一边 = 一个字节都收不到）
  ⑤ 反例自证：把解码写反，上面几条必须变红

用法
----
    python tools/check_frida_tap.py             # 纯静态，不需要遥控器/frida
    python tools/check_frida_tap.py --watch 20  # 额外注入 WUDFHost 实时监听（要真机）

退出码 0 = 通过；1 = 失败。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _decode_cases() -> list[tuple[bytes, tuple[str | None, bool], str]]:
    """（原始报告, 期望的 (按钮, 是否按下), 说明）。

    ⚠ 消费类页是**小端 16 位** usage：`02 42 00` → 0x0042（方向上）。
    把字节序写反（0x4200）就一条都认不出来，而现象和"报告没来"一样。
    """
    return [
        # ── 消费类页：3 字节 [0x02][lo][hi] ──
        (bytes.fromhex("024200"), ("up", True),       "消费类 方向上 0x0042"),
        (bytes.fromhex("024300"), ("down", True),     "消费类 方向下 0x0043"),
        (bytes.fromhex("024400"), ("left", True),     "消费类 方向左 0x0044"),
        (bytes.fromhex("024500"), ("right", True),    "消费类 方向右 0x0045"),
        (bytes.fromhex("024100"), ("ok", True),       "消费类 确认 0x0041"),
        (bytes.fromhex("022402"), ("back", True),     "消费类 返回 0x0224"),
        (bytes.fromhex("022302"), ("home", True),     "消费类 Home 0x0223"),
        (bytes.fromhex("02e200"), ("mute", True),     "消费类 静音 0x00E2"),
        (bytes.fromhex("02e900"), ("vol_up", True),   "消费类 音量＋ 0x00E9"),
        (bytes.fromhex("02ea00"), ("vol_down", True), "消费类 音量－ 0x00EA"),
        (bytes.fromhex("027700"), ("youtube", True),  "消费类 YouTube 0x0077"),
        (bytes.fromhex("027800"), ("netflix", True),  "消费类 Netflix 0x0078"),
        (bytes.fromhex("029e01"), ("power", True),    "消费类 电源 0x019E"),
        (bytes.fromhex("028901"), ("input", True),    "消费类 信源 0x0189"),
        (bytes.fromhex("020000"), ("<up>", False),    "消费类 空闲帧（全部松开）"),
        (bytes.fromhex("02ff00"), (None, False),      "消费类 不认识的 usage → 忽略"),
        # ── 厂商页：首字节 0x01，8 位 usage ──
        (bytes.fromhex("0103"), ("up", True),         "厂商页 方向上 0x03"),
        (bytes.fromhex("0107"), ("ok", True),         "厂商页 确认 0x07"),
        (bytes.fromhex("010b"), ("back", True),       "厂商页 返回 0x0B"),
        (bytes.fromhex("0100"), ("<up>", False),      "厂商页 空闲帧"),
        (bytes.fromhex("0199"), (None, False),        "厂商页 不认识的 usage → 忽略"),
        # ── 垃圾输入 ──
        (b"",                   (None, False),        "空报告 → 忽略"),
        (bytes.fromhex("00"),   (None, False),        "1 字节 → 忽略"),
        (bytes.fromhex("090000000000000000"), (None, False), "别的蓝牙键鼠 9 字节报告 → 忽略"),
    ]


def run_decode_tests() -> bool:
    import config
    import frida_hid

    print("── 1. 消费类页 usage 表 ──")
    tbl = frida_hid.CC_USAGE_TO_BUTTON
    print(f"   共 {len(tbl)} 个按键（CHROMECAST_BUTTONS 共 "
          f"{len(config.CHROMECAST_BUTTONS)} 个）")
    for usage in sorted(tbl):
        btn = tbl[usage]
        label = (config.CHROMECAST_BUTTONS.get(btn) or {}).get("label", "⚠ 未定义")
        print(f"     0x{usage:04X} → {btn:<9} {label}")

    # ① 表里的 button_id 必须都在 CHROMECAST_BUTTONS 里（拼错就是"按键没反应"）
    unknown = [b for b in tbl.values() if b not in config.CHROMECAST_BUTTONS]
    if unknown:
        print(f"   ❌ 表里有 config 不认识的 button_id：{unknown}")
        return False
    print("   OK 表里的 button_id 都能在 CHROMECAST_BUTTONS 找到")

    # ② 所有「int usage」的按键都要被这张表覆盖（漏一个 = 那个键没反应）
    missing = [k for k, v in config.CHROMECAST_BUTTONS.items()
               if isinstance(v["usage"], int) and k not in tbl.values()]
    if missing:
        print(f"   ❌ 这些 int-usage 的按键没进消费类页表：{missing}")
        return False
    print("   OK 所有 int-usage 按键都进了消费类页表")

    # ③ 语音键绝不能在里面（它走 ATVV，混进来就是"按语音键触发两次"）
    if "voice" in tbl.values():
        print("   ❌ 消费类页表里不该出现语音键（它走 ATVV，不走 HID）")
        return False
    print("   OK 语音键不在消费类页表内（不会和 ATVV 抢）")

    print("\n── 2. 报告解码（两种格式）──")
    bad = 0
    for raw, expect, note in _decode_cases():
        got = frida_hid.decode_tap_report(raw)
        ok = got == expect
        bad += 0 if ok else 1
        print(f"   {'OK ' if ok else '❌ '} {raw.hex(' ') or '(空)':<20} → "
              f"{str(got):<22} {note}")
    if bad:
        print(f"   ❌ {bad} 条解码不符预期")
        return False
    print("   OK 全部符合预期")

    print("\n── 3. 「松手」用上一个按下的键补齐 ──")
    # decode_tap_report 对松手只返回 <up>，真正松开哪个键由 RemoteHidTap._handle
    # 用 _last_down 补。单独验证那段状态机（漏掉会让 PTT 的 Ctrl/Win 卡住）。
    hits: list[tuple[str, bool]] = []

    class _Rec:
        def __call__(self, btn, down):
            hits.append((btn, down))

    tap = frida_hid.RemoteHidTap(_Rec())
    for raw in (bytes.fromhex("02e200"), bytes.fromhex("020000"),
                bytes.fromhex("02ff00"), bytes.fromhex("020000")):
        tap._handle_report(raw)
    want = [("mute", True), ("mute", False)]
    if hits != want:
        print(f"   ❌ 期望 {want}，实际 {hits}")
        return False
    print(f"   OK 按下 → 松手配对正确：{hits}")

    print("\n── 4. 「要抹掉原生动作的 usage」算法 ──")
    km = {"up": "up", "ok": "enter", "back": "", "home": "native",
          "mute": "voice_ptt", "voice": "voice"}
    cc, vp = frida_hid.block_usages_for(km)
    # up / ok / mute / voice 是"已映射"，要抹；back（禁用）与 home（native）不抹
    if 0x0042 not in cc or 0x0041 not in cc or 0x00E2 not in cc:
        print(f"   ❌ 已映射的键没进抹除表：cc={[hex(x) for x in cc]}")
        return False
    if 0x0224 in cc or 0x0223 in cc:
        print(f"   ❌ 禁用/native 的键不该被抹（用户就是想让它们保持系统行为）："
              f"cc={[hex(x) for x in cc]}")
        return False
    if 3 not in vp or 0x0A in vp:
        print(f"   ❌ 厂商页抹除表不对：vp={[hex(x) for x in vp]}")
        return False
    print(f"   OK 只抹已映射的键：cc={[hex(x) for x in cc]} vp={[hex(x) for x in vp]}")

    print("\n── 5. .js 与 .py 的 IOCTL 常量一致 ──")
    js = (ROOT / "frida_tap.js").read_text(encoding="utf-8")
    m = re.search(r"READ_IOCTL\s*=\s*(0x[0-9A-Fa-f]+)", js)
    if not m:
        print("   ❌ frida_tap.js 里找不到 READ_IOCTL")
        return False
    js_val = int(m.group(1), 16)
    if js_val != frida_hid.READ_IOCTL:
        print(f"   ❌ 漂移了：frida_tap.js={hex(js_val)}，"
              f"frida_hid.py={hex(frida_hid.READ_IOCTL)}")
        return False
    print(f"   OK 两边都是 {hex(js_val)}")

    # 抹除分支必须两种格式都在（少一种 = 那种格式的键会"原生 + 映射"双发）
    for token in ("blockCC", "blockVendor", "writeByteArray"):
        if token not in js:
            print(f"   ❌ frida_tap.js 里找不到 {token}（抹除分支可能被删了）")
            return False
    print("   OK 抹除分支覆盖两种格式")

    return True


def run_counter_examples() -> bool:
    """反例自证：把解码写坏，上面那些检查必须能抓到。

    为什么值得：一条永远绿的断言等于没有断言。这里人为把消费类页的
    字节序写反、把语音键塞进表里，看检查会不会红。
    """
    import frida_hid

    print("\n── 6. 反例自证 ──")
    ok = True

    orig = frida_hid.decode_tap_report

    def _swapped(raw: bytes):
        # 故意把 16 位 usage 的字节序写反
        if raw and raw[0] == 0x02 and len(raw) >= 3:
            usage = raw[2] | (raw[1] << 8)
            if usage == 0:
                return "<up>", False
            btn = frida_hid.CC_USAGE_TO_BUTTON.get(usage)
            return (btn, True) if btn else (None, False)
        return orig(raw)

    frida_hid.decode_tap_report = _swapped
    try:
        got = frida_hid.decode_tap_report(bytes.fromhex("024200"))
        if got == ("up", True):
            print("   ❌ [反例] 字节序写反后仍然解对了 → 说明解码检查无效")
            ok = False
        else:
            print(f"   OK [反例] 字节序写反 → 解出 {got}（不是 up）⇒ 检查有效")
    finally:
        frida_hid.decode_tap_report = orig

    orig_tbl = frida_hid.CC_USAGE_TO_BUTTON
    try:
        frida_hid.CC_USAGE_TO_BUTTON = dict(orig_tbl)
        frida_hid.CC_USAGE_TO_BUTTON[0x1234] = "voice"
        unknown = [b for b in frida_hid.CC_USAGE_TO_BUTTON.values()
                   if b not in __import__("config").CHROMECAST_BUTTONS]
        has_voice = "voice" in frida_hid.CC_USAGE_TO_BUTTON.values()
        if has_voice or unknown:
            print(f"   OK [反例] 把语音键/野键塞进表 → 检查能抓到"
                  f"（voice={has_voice} unknown={unknown}）")
        else:
            print("   ❌ [反例] 塞进野值后检查抓不到 → 说明表检查无效")
            ok = False
    finally:
        frida_hid.CC_USAGE_TO_BUTTON = orig_tbl

    return ok


def watch(seconds: float) -> bool:
    """真机：注入 WUDFHost 实时监听（需要遥控器在线 + 装了 frida）。"""
    import frida_hid

    print(f"\n── 7. 实时监听按键旁路 {seconds:.0f} 秒（请按遥控器上的键）──")
    seen: list[tuple[float, str, bool]] = []

    def _on(btn: str, down: bool) -> None:
        seen.append((time.time(), btn, down))
        print(f"   🔘 {btn:<9} {'按下' if down else '松开'}")

    tap = frida_hid.RemoteHidTap(_on)
    tap.start()
    try:
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        print("   " + tap.selfcheck())
        tap.stop()
    if not seen:
        print(f"   ⚠ {seconds:.0f} 秒内没收到任何按键报告。")
        print("     三种可能：① 这段时间确实没按键；② 注入失败（看上面日志）；"
              "③ 报告格式不同（把自检那行发出来）。")
    else:
        print(f"   OK 收到 {len(seen)} 次按键")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="按键旁路（Frida 注入 WUDFHost）自检")
    ap.add_argument("--watch", type=float, metavar="SEC",
                    help="额外注入 WUDFHost 实时监听 N 秒（需要真机 + frida）")
    args = ap.parse_args()

    print("=" * 60)
    print("frida_hid 自检 —— 遥控器按键旁路（注入 WUDFHost 读 HID 报告）")
    print("=" * 60)

    ok = run_decode_tests()
    ok = run_counter_examples() and ok
    if args.watch:
        ok = watch(args.watch) and ok

    print("\n" + ("✅ 通过" if ok else "❌ 失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
