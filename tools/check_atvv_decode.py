"""ATVV 解码健壮性 —— 2026-09-29 审查报告 P0-3。

被修的是什么
------------
`atvv.decode_audio()` 的 **v0.4 分支**里有一句 `logger.debug(...)`
（帧长不符时丢弃该帧），而 `atvv.py` 原先**既没有 `import logging`、
也没有定义 `logger`** ⇒ 一旦走到那条分支就是 `NameError`。

它的杀伤面比看上去大：
  · `decode_audio()` 是**每一帧音频**都要过的路（约 62.5 帧/秒）；
  · 异常发生在 BLE 通知回调线程上，被外层 try 吞成一行语焉不详的 error；
  · 结果是**所有 v0.4 的音频帧全部解不出来** —— 现象是"按了没声音"，
    而日志里没有任何指向"帧长不符"的线索。

本机遥控器协商的是 v1.0（`CAPS: v1.0 codec=0x02 sr=16000 frame=247`），
所以这条一直没被触发 —— 属于**潜伏**缺陷：换一台 v0.4 的遥控器
（或上游改了协商顺序）当场就炸。

这条闸只认行为：拿一个长度不符的 v0.4 帧去解，**必须**干净地返回 None，
不许抛异常。带反例自证（把 logger 抽掉，同样的调用必须炸 → 证明这条路
真的会走到 `logger.debug` 那一行，不是被前面的 return 短路掉的）。

用法
----
    python tools/check_atvv_decode.py

退出码 0 = 通过；1 = 失败。
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FRAME_SIZE = 42        # v0.4 协商出来的帧长（随便一个 ≥6 的值即可）


def _proto_with_v04():
    """造一个"协商成 v0.4、正在推流"的 ATVVProtocol（不碰任何硬件）。"""
    import atvv

    p = atvv.ATVVProtocol()
    p.state.caps = atvv.ATVVCapabilities(
        version=(0, 4), codecs=0x02, interaction_model=0, frame_size=FRAME_SIZE,
    )
    p.state.codec = 0x02
    p.state.stream_active = True
    return p


def check_logger_defined(verbose: bool = True) -> bool:
    """静态：模块里必须有 logging 与模块级 logger。"""
    src = (ROOT / "atvv.py").read_text(encoding="utf-8", errors="replace")
    ok = True
    if not re.search(r"^\s*import logging\b", src, re.M):
        print("   ❌ atvv.py 里没有 `import logging`")
        ok = False
    else:
        print("   OK atvv.py 里有 import logging")
    if not re.search(r"^logger\s*=\s*logging\.getLogger\(", src, re.M):
        print("   ❌ atvv.py 里没有模块级 `logger = logging.getLogger(...)`"
              " —— 用了 logger 却没定义 = NameError")
        ok = False
    else:
        print("   OK atvv.py 里有模块级 logger")

    import atvv
    if not isinstance(getattr(atvv, "logger", None), logging.Logger):
        print(f"   ❌ atvv.logger 不是 logging.Logger（实际 {type(getattr(atvv,'logger',None))}）")
        ok = False
    else:
        print(f"   OK atvv.logger 可用：{atvv.logger.name}")
    return ok


def check_wrong_len_returns_none(verbose: bool = True) -> bool:
    """行为：v0.4 + 帧长不符 → 干净地返回 None（不抛异常、不硬解）。"""
    p = _proto_with_v04()
    for n in (0, 5, 10, FRAME_SIZE - 1, FRAME_SIZE + 1):
        try:
            got = p.decode_audio(b"\x00" * n)
        except Exception as e:                 # noqa: BLE001
            print(f"   ❌ 长度 {n} 的 v0.4 帧把 decode_audio 炸了："
                  f"{type(e).__name__}: {e}")
            return False
        if got is not None:
            print(f"   ❌ 长度 {n} 的 v0.4 帧不该解出东西（返回 {len(got)} 个采样）"
                  f" —— 硬解只会把噪声当语音喂给输入法")
            return False
        if verbose:
            print(f"   OK 长度 {n:>3} → None（干净丢弃，无异常）")
    return True


def check_good_len_still_decodes(verbose: bool = True) -> bool:
    """行为：长度**对得上**的 v0.4 帧必须真的解出采样。

    ⚠ 这一条是防"整段 return None 也能过"的假绿：上面那条只证明
      "长度不符时不抛异常"，如果有人在函数开头直接 `return None`，
      上面那条照样绿。所以必须再证明"该解的时候真的解出来了"。
    """
    p = _proto_with_v04()
    frame = bytes([0x00, 0x01, 0x02, 0x00, 0x10, 0x08]) + bytes(FRAME_SIZE - 6)
    try:
        got = p.decode_audio(frame)
    except Exception as e:                     # noqa: BLE001
        print(f"   ❌ 长度正确的 v0.4 帧解不出来：{type(e).__name__}: {e}")
        return False
    if not got:
        print("   ❌ 长度正确的 v0.4 帧返回了空 —— v0.4 这条路等于废的")
        return False
    print(f"   OK 长度正确 → 解出 {len(got)} 个采样（v0.4 这条路是通的）")
    return True


def run_counter_examples(verbose: bool = True) -> bool:
    """反例自证：把 logger 抽掉，长度不符那条必须炸。

    为什么这条反例是关键：它同时证明了两件事 ——
      ① 代码**真的会走到** `logger.debug` 那一行（不是被前面的 return 短路）；
      ② 所以"没有 logger"确实是一颗会响的雷，不是纸上推演。
    """
    import atvv

    print("\n── 反例自证 ──")
    orig = atvv.logger
    atvv.logger = None                        # 模拟"没定义 logger"
    try:
        p = _proto_with_v04()
        try:
            p.decode_audio(b"\x00" * 10)
        except Exception as e:                # noqa: BLE001
            print(f"   OK [反例] 抽掉 logger → 长度不符的帧炸出 "
                  f"{type(e).__name__} ⇒ 这条检查真的测到了那一行")
            return True
        print("   ❌ [反例] 抽掉 logger 后居然没炸 → 说明长度不符这条根本没走到 "
              "logger 那一行，检查是无效的")
        return False
    finally:
        atvv.logger = orig


def main() -> int:
    print("=" * 60)
    print("atvv 解码自检 —— v0.4 分支不许 NameError（审查报告 P0-3）")
    print("=" * 60)

    print("\n── 1. logger 必须存在 ──")
    ok = check_logger_defined()

    print("\n── 2. v0.4 帧长不符 → 干净丢弃 ──")
    ok = check_wrong_len_returns_none() and ok

    print("\n── 3. v0.4 帧长正确 → 真的能解（防「整段 return None」假绿）──")
    ok = check_good_len_still_decodes() and ok

    ok = run_counter_examples() and ok

    print("\n" + ("✅ 通过" if ok else "❌ 失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
