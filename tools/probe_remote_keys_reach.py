"""探针：遥控器的按键到底有没有进 Windows —— 一条命令给出三态结论。

## 为什么要它

「按键没反应」这件事查了很久，反复卡在同一个盲区：日志既分不清
「遥控器发了这个键」，也分不清「遥控器什么都没发」。

2026-09-23 的取证结论是**没发**（115302 行日志里，遥控器独有的
`up`/`down`/`home`/`volume *` 一条都没有；见 CHANGELOG）。
但那个结论是从历史日志里**反推**出来的 —— 以后若再有人主张
「纯软件有戏」，需要一个能在 60 秒内**正面回答**的探针，
而不是再翻一次 115k 行日志。这就是本工具。

## 它怎么判：两段式，先量本底再取样

```
① 基线段（前 10 秒）：**一个键都别碰**   → 量出本底噪声
② 取样段（之后 N 秒）：**只按遥控器**     → 看有没有新键出现
```

判据（三态，不含糊）：

| 取样段结果 | 判读 |
|---|---|
| 出现了基线段里没有的键 | ✅ 遥控器的键**进了 Windows**，问题在我们这层 |
| 一个键都没有 | ❌ 遥控器的键**没进 Windows**（Windows 根本没收到） |
| 打字太多（>30 条）或基线段就被污染 | ⚠ 无法判定 —— 有人在敲物理键盘，重跑一次 |

## ⚠ 两个必须要说清的坑（本项目真踩过）

**坑 1：测量口径错了，得出的数字就是错的。**
v1.0.14 把防自激窗口设成 0.8s，依据是「回声最长 350ms」—— 那个数是拿
**排队**时刻量的，而真正的 GATT 写入可能慢到 1 秒，回声就跟着晚 1 秒
→ 漏出窗口被当成真按键 → **按下就掉**。所以本探针的取样段要求
「只按遥控器、不碰键盘」，就是为了让口径干净。

**坑 2：`scan_code < 0` 不能当作「遥控器没发」的判据。**
2026-09-23 实测：我们自己注入的语音热键回环里，
`left windows`(scan=91)、`shift`(scan=42)、`ctrl`(scan=29) 都是**正数**，
而 `f13`(-124)、`left ctrl`(-162)、`reserved `(-252) 是负数 ——
**同一批注入事件，符号并不统一**。

⇒ 本工具的口径：**负 scan 码只做"标记"，不参与判读**。
真正的判据是**基线段 vs 取样段的差集** —— 所以协议要求
① 基线段一个键都不碰（本底干净）② 取样段只按遥控器（差集就是遥控器的键）。
`--selftest` 第 3 条专门钉住「只有合成事件时不许判成进了」。

**坑 3（本工具第一版的错）：不许把 `enter`/`escape`/`home` 当噪音排除。**
第一版抄了个「打字键」名单，把 `up`/`down`/`home`/`enter`/`escape` 一起
排除掉了 —— 可这些**正是**遥控器确认 / 返回 / 主页 / 方向键最可能的名字，
等于把要查的东西过滤掉了。现在任何键名都算数（`--selftest` 第 4 条钉住）。

## 用法

    python tools\\probe_remote_keys_reach.py              # 基线 10s + 取样 40s
    python tools\\probe_remote_keys_reach.py --seconds 60 # 自定义取样时长
    python tools\\probe_remote_keys_reach.py --selftest   # 只验判读逻辑，不碰键盘

⚠ **取样段请按遥控器的「确认 / 返回 / 主页 / 方向 / 音量」键**，
   并且**绝对不要碰物理键盘**。
⚠ **先退掉安装版 `RemoteVoiceBridge.exe`**（它的键盘钩子会把按键再报一遍），
   否则噪声翻倍。
⚠ **不要把它加进 `tools/check_all.py`** —— 它需要有人在遥控器上按键，
   是「现场探针」不是「静态闸门」。混进去会让 CI 挂在一件需要人手的事上。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _utf8 import setup as _setup_utf8  # noqa: E402
_setup_utf8()

BASELINE_SECONDS = 10

# 取样段超过这么多条按键，就认为有人在敲物理键盘，差集不再可信 → 不给结论。
# （只按遥控器、40 秒，很难按出 30 条。）
NOISE_LIMIT = 30

# 遥控器上真实存在、**物理键盘上通常不会按到**的键 —— 命中这些是强信号。
REMOTE_STRONG = {
    "volume up", "volume down", "volume mute", "mute",
    "media play pause", "media next track", "media previous track",
    "media stop", "play", "pause", "stop", "next", "previous",
    "browser home", "browser search", "browser back", "browser refresh",
    "browser stop", "browser favourites", "search",
    "tv", "youtube", "netflix",
}


@dataclass
class Sample:
    """一次按键事件（只留判读需要的字段）。"""
    t: float
    kind: str          # down / up
    name: str
    scan: int | None
    phase: str         # baseline / sample


@dataclass
class Collector:
    events: list[Sample] = field(default_factory=list)
    phase: str = "baseline"
    started: float = 0.0

    def on_key(self, e) -> None:
        name = (e.name or "").strip()
        if not name:
            return                      # 名字都读不到 → 一律丢弃（本项目最反复的静默洞）
        scan = getattr(e, "scan_code", None)
        self.events.append(Sample(time.time() - self.started, e.event_type,
                                  name, scan, self.phase))


def _synthetic(s: Sample) -> bool:
    """**疑似别的软件注入**的事件。

    判据用 scan 码**符号**：`keyboard` 库对一部分事件给负 scan 码。
    ⚠ 这个标记**不能当唯一判据**（见文件头坑 2）：我们自己注入的
      `left windows`(91)、`shift`(42) 就是正数。
      所以它只用来「提示这一条可能是别的软件发的」，
      真正判读靠基线段 vs 取样段的差集。
    """
    return (s.scan or 0) < 0


def _is_strong(s: Sample) -> bool:
    return s.name.lower() in REMOTE_STRONG


def dump(events: list[Sample]) -> None:
    print(f"{'相对秒':>8}  {'阶段':<9} {'事件':<5} {'键名':<22} {'scan':>6}  标记")
    print("-" * 78)
    for s in events:
        marks = []
        if _synthetic(s):
            marks.append("SYNTHETIC?（负 scan，可能是别的软件发的）")
        if _is_strong(s):
            marks.append("★遥控器特征键")
        print(f"{s.t:8.2f}  {s.phase:<9} {s.kind:<5} {s.name!r:<22} "
              f"{str(s.scan):>6}  {' '.join(marks)}")


def verdict(events: list[Sample]) -> tuple[bool, str]:
    """返回 (是否全绿, 结论文本)。

    判据 = **基线段里没有、而取样段里出现了的键名**（疑似合成的不计）。
    """
    base_all = {s.name for s in events if s.phase == "baseline" and not _synthetic(s)}
    sample = [s for s in events if s.phase == "sample" and not _synthetic(s)]
    fresh = [s for s in sample if s.name not in base_all]

    print()
    print("=" * 78)
    print("【判读】")
    print(f"  基线段：{len(base_all)} 种键 {sorted(base_all)}")
    print(f"  取样段：{len(sample)} 条按键，其中新出现 {len(fresh)} 条")

    # 本底就不干净 → 差集没有意义，不许硬给结论
    if base_all:
        print(f"  ⚠ 无法判定 —— 基线段（该一个键都不碰的那 10 秒）里出现了 "
              f"{sorted(base_all)}")
        print("     本底脏了，差集就分不出「遥控器的键」和「你碰的键」。")
        print("     请重跑：第 1 段手离开键盘和遥控器。")
        return False, "INCONCLUSIVE"

    if len(sample) > NOISE_LIMIT:
        print(f"  ⚠ 无法判定 —— 取样段 {len(sample)} 条按键，"
              f"超过噪声阈值 {NOISE_LIMIT}（有人在敲物理键盘）。")
        print("     请重跑：第 2 段只按遥控器。")
        return False, "INCONCLUSIVE"

    if fresh:
        kinds = Counter(s.name for s in fresh)
        print("  ✅ 遥控器的键**进了 Windows** —— 取样段出现了这些新键：")
        for n, c in kinds.most_common():
            hit = "  ←★遥控器特征键" if n.lower() in REMOTE_STRONG else ""
            print(f"       {n!r} × {c}{hit}")
        print("     ⇒ 那「按键没反应」就是我们这层（映射/拦截/注入）的事，"
              "**纯软件能修**。")
        return True, "REACHED_WINDOWS"

    print("  ❌ 取样段**一个键都没有** ⇒ 遥控器的键**没进 Windows**。")
    print("     已排除的可能：不是「打字被过滤掉了」（现在任何键名都算数，"
          "包括 enter/escape/home 这种和键盘同名的），"
          "也不是「把别的软件合成的键当成遥控器」（负 scan 已剔除）。")
    print("     ⇒ 与 2026-09-23 那份 115302 行日志的取证一致（见 CHANGELOG）。")
    return True, "NOT_REACHING"


def _selftest() -> int:
    """自证：判读逻辑必须能把六种情形分开。

    不带这个自证的探针是危险的：它要是把「别的软件合成的键」判成
    「遥控器的键进去了」，就会得出**正好相反**的结论，
    而那种结论会让人再去折腾一轮纯软件方案。
    """
    import contextlib
    import io

    def ev(t, name, scan, phase):
        return Sample(t, "down", name, scan, phase)

    cases: list[tuple[str, list[Sample], str, bool]] = [
        ("取样段一个键都没有 → 没进 Windows",
         [],
         "NOT_REACHING", True),
        ("取样段出现遥控器特征键（volume up）→ 进了 Windows",
         [ev(12, "volume up", 175, "sample")],
         "REACHED_WINDOWS", True),
        ("⚠ 只有合成事件（scan<0）→ 必须仍判没进",
         [ev(11, "reserved ", -252, "sample"), ev(11, "f13", -124, "sample"),
          ev(11, "left ctrl", -162, "sample")],
         "NOT_REACHING", True),
        ("★ 出现 enter/escape/home（名字像键盘键，但这正是遥控器的"
         "确认/返回/主页）→ 必须判成「进了」",
         [ev(12, "enter", 28, "sample"), ev(13, "escape", 1, "sample"),
          ev(14, "home", 71, "sample")],
         "REACHED_WINDOWS", True),
        ("打字噪声过大（>30 条）→ 无法判定（不给结论）",
         [ev(10 + i * 0.1, "x", 45, "sample") for i in range(40)],
         "INCONCLUSIVE", False),
        ("⚠ 基线段就不干净（有人碰了键）→ 无法判定",
         [ev(3, "a", 30, "baseline"), ev(12, "enter", 28, "sample")],
         "INCONCLUSIVE", False),
    ]

    ok = True
    print("=" * 78)
    print(" 自证：判读逻辑（不需要遥控器、不需要按键）")
    print("=" * 78)
    for title, events, want_code, want_ok in cases:
        with contextlib.redirect_stdout(io.StringIO()):
            got_ok, got_code = verdict(events)
        good = (got_code == want_code and got_ok == want_ok)
        ok = ok and good
        print(f"  {'✅' if good else '❌'} {title}")
        print(f"        期望 {want_code}/{want_ok}　实得 {got_code}/{got_ok}")
    print()
    print("PASS" if ok else "FAIL —— 判读逻辑不可信，别拿它的结论去做决定")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="遥控器按键是否进了 Windows")
    ap.add_argument("--seconds", type=int, default=40,
                    help="取样段秒数（默认 40）")
    ap.add_argument("--json", action="store_true", help="末尾多打一行 JSON（给脚本用）")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑判读逻辑自证，不碰键盘、不需要遥控器")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    try:
        import keyboard  # noqa: PLC0415
    except Exception as e:                                    # noqa: BLE001
        print(f"FAIL 装不上 keyboard 库：{e}")
        return 2

    print("=" * 78)
    print(" 探针：遥控器的按键到底有没有进 Windows")
    print("=" * 78)
    print()
    print(f"  第 1 段（{BASELINE_SECONDS} 秒）：**手离开键盘和遥控器** —— 量本底")
    print(f"  第 2 段（{args.seconds} 秒）：**只按遥控器上的键**，反复按")
    print("        建议按：确认 / 返回 / 主页 / 方向 / 音量")
    print()
    print("  ⚠ 先退掉安装版 RemoteVoiceBridge.exe（它的钩子会把按键也报一遍）")
    print("  ⚠ 第 1 段千万别碰键盘，否则本底脏了、这次就白测")
    print()
    input("  准备好按回车开始…")

    col = Collector(started=time.time())
    keyboard.hook(col.on_key, suppress=False)

    col.phase = "baseline"
    for left in range(BASELINE_SECONDS, 0, -1):
        print(f"\r  基线中… {left:2d}s（手离开键盘和遥控器）", end="", flush=True)
        time.sleep(1)
    print()

    col.phase = "sample"
    print()
    print("  👉 现在只按遥控器的键（确认 / 返回 / 主页 / 方向 / 音量），反复按")
    for left in range(args.seconds, 0, -1):
        if left % 5 == 0 or left <= 5:
            print(f"\r  取样中… {left:2d}s", end="", flush=True)
        time.sleep(1)
    print()
    print()

    dump(col.events)
    ok, code = verdict(col.events)
    print("=" * 78)
    print(f"  {'PASS' if ok else 'WARN'}  {code}")

    if args.json:
        print(json.dumps({"verdict": code, "events": len(col.events),
                          "sample_seconds": args.seconds}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
