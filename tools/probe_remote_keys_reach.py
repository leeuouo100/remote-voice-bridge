"""探针：遥控器的按键到底有没有进 Windows —— 一条命令给出三态结论。

## 为什么要它

「按键没反应」这件事在两份真机日志上查了 8 天，反复卡在同一个盲区：
日志既分不清「遥控器发了这个键」，也分不清「遥控器什么都没发」。

2026-09-23 的取证结论是**没发**（115302 行日志里，遥控器独有的
`up`/`down`/`home`/`volume up|down|mute` 一条都没有；见 CHANGELOG）。
但那个结论是从历史日志里**反推**出来的 —— 以后若再有人主张
「纯软件有戏」，需要一个能在 60 秒内**正面回答**的探针，
而不是再翻一次 115k 行日志。这就是本工具。

## 它怎么判：两段式，先量本底再取样

```
① 基线段（前 10 秒）：**什么都别按**  → 量出本底噪声
② 取样段（之后 N 秒）：**只按遥控器的键** → 记录新增的按键
```

判据（三态，不含糊）：

| 取样段结果 | 判读 |
|---|---|
| 出现「基线里没有、且不是合成事件的」键 | ✅ 遥控器的键**进了 Windows**，问题在我们这层 |
| 一条新的都没有 | ❌ 遥控器的键**没进 Windows**（Windows 根本没收到） |
| 基线段噪声过大 | ⚠ 无法判定 —— 有人在打字，重跑一次 |

## ⚠ 最大的坑（本项目的真实教训，别再踩）

**合成事件会回环成按键事件，而且看起来"很像遥控器的键"。** `main.py` 注入
`左Ctrl+左Win+左Shift` 时，全局钩子会把自己注入的键也报一遍 ——
真机日志里那批 `f13`(scan=-124)、`left ctrl`(-162)、`left shift`(-160)、
`reserved `(-252) 就是这么来的。**光看键名会把它们当成"遥控器按键进去了"。**

本工具的处置：**`scan_code < 0` 的一律排除出判据**。理由不是"负号＝我们注入的"
（2026-09-23 实测发现别的软件合成的键也是负号：`send_after_voice=False` 时
仍看到一个 `enter`(scan=-13)，那是输入法之类合成的），而是：

> 遥控器的键若真进了 Windows，是**系统的 HID 栈**投递的，
> 一定带**真实 scan 码**（真机实测过的 `left`=75、`right`=77、`enter`=28 都是正数）
> ⇒ 用正数 scan 码这个筛子**不会漏掉遥控器**，却能滤掉所有合成事件。

## 用法

    python tools/probe_remote_keys_reach.py              # 基线 10s + 取样 40s
    python tools/probe_remote_keys_reach.py --seconds 60 # 自定义取样时长
    python tools/probe_remote_keys_reach.py --selftest   # 只验判读逻辑：不碰键盘、不用遥控器

⚠ **不要把它加进 `tools/check_all.py`** —— 它需要有人在遥控器上按键，
   是「现场探针」不是「静态闸门」。混进去会让 CI 挂在一件需要人手的事上。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _utf8 import setup as _setup_utf8  # noqa: E402
_setup_utf8()

BASELINE_SECONDS = 10

# 「打字键」＝单个字母/数字/普通符号 + 常见修饰键。
# 这些**不能**用来判断遥控器：物理键盘和遥控器都可能产生，
# 而且真机日志里 343 条 🔘 全是这一类（见 CHANGELOG 取证）。
TYPING = re.compile(
    r"^(?:[a-z0-9]|,|\.|/|;|'|\[|\]|\\|`|-|=|\s|"
    r"left shift|right shift|left ctrl|right ctrl|left alt|right alt|"
    r"left windows|right windows|caps lock|tab|tab key|"
    r"space|backspace|delete|enter|esc|escape|"
    r"up|down|left|right|home|end|page up|page down|"
    r"insert|pause|scroll lock|num lock|print screen|"
    r"apps|menu)$",
    re.I,
)

# 遥控器上真实存在、且**物理键盘上通常不会按到**的键。
# 命中这些才是强信号（物理键盘也有音量/静音键，所以仍要结合基线看）。
REMOTE_STRONG = {
    "volume up", "volume down", "volume mute", "mute",
    "media play pause", "media next track", "media previous track",
    "media stop", "play", "pause", "stop", "next", "previous",
    "browser home", "browser search", "search",
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
    """**合成事件** —— 由本程序或别的软件注入，不是遥控器发来的。

    判据用 scan 码**符号**，而不是"是谁注入的"：`keyboard` 库对注入/合成的事件
    给负 scan 码。实测（2026-09-23）：`send_after_voice=False` 时仍看到一个
    `enter`(scan=-13) —— 那是输入法之类合成的，不是我们发的，
    所以口径只能写成「合成」，不能写成「我们注入的」。

    为什么这个筛子安全：遥控器的键若真进了 Windows，是**系统的 HID 栈**投递的，
    一定带**真实 scan 码**（真机实测 `left`=75、`right`=77、`enter`=28 全是正数）
    ⇒ 滤掉负号**不会漏掉遥控器**。
    """
    return (s.scan or 0) < 0


def _interesting(s: Sample) -> bool:
    return not TYPING.match(s.name)


def dump(events: list[Sample]) -> None:
    print(f"{'相对秒':>8}  {'阶段':<9} {'事件':<5} {'键名':<22} {'scan':>6}  标记")
    print("-" * 78)
    for s in events:
        marks = []
        if _synthetic(s):
            marks.append("SYNTHETIC(合成事件，不是遥控器)")
        if s.name.lower() in REMOTE_STRONG:
            marks.append("★遥控器特征键")
        if not _interesting(s):
            marks.append("打字键")
        print(f"{s.t:8.2f}  {s.phase:<9} {s.kind:<5} {s.name!r:<22} "
              f"{str(s.scan):>6}  {' '.join(marks)}")


def verdict(events: list[Sample]) -> tuple[bool, str]:
    """返回 (是否全绿, 结论文本)。"""
    base_keys = {s.name for s in events if s.phase == "baseline" and _interesting(s)}
    base_typing = sum(1 for s in events if s.phase == "baseline" and not _interesting(s))
    sample = [s for s in events if s.phase == "sample"]
    fresh = [s for s in sample
             if _interesting(s) and not _synthetic(s) and s.name not in base_keys]
    fresh_typing = sum(1 for s in sample if not _interesting(s))

    print()
    print("=" * 78)
    print("【判读】")
    print(f"  基线段：非打字键 {len(base_keys)} 种 {sorted(base_keys)}，打字键 {base_typing} 条")
    print(f"  取样段：非打字键 {len([s for s in sample if _interesting(s)])} 条"
          f"（其中新出现的 {len(fresh)} 条）、打字键 {fresh_typing} 条")

    if fresh:
        kinds = Counter(s.name for s in fresh)
        print("  ✅ 遥控器的键**进了 Windows** —— 取样段出现了这些新键：")
        for n, c in kinds.most_common():
            hit = "  ←★遥控器特征键" if n.lower() in REMOTE_STRONG else ""
            print(f"       {n!r} × {c}{hit}")
        print("     ⇒ 那「按键没反应」就是我们这层（映射/拦截/注入）的事，**纯软件能修**。")
        return True, "REACHED_WINDOWS"

    amb = base_typing + fresh_typing
    if amb > 30:
        print("  ⚠ 无法判定 —— 这两段里打字太多，本底被污染了（请重跑，取样段只按遥控器）")
        return False, "INCONCLUSIVE"

    print("  ❌ 取样段**一条新键都没有** ⇒ 遥控器的键**没进 Windows**。")
    print("     已排除的可能：不是「打字被过滤掉了」（打字键单独计数了），"
          "也不是「把合成事件当成遥控器」（scan<0 已剔除，遥控器一定带真实 scan 码）。")
    print("     ⇒ 与 2026-09-23 那份 115302 行日志的取证一致（见 CHANGELOG）。")
    return True, "NOT_REACHING"


def _selftest() -> int:
    """自证：判读逻辑必须能把四种情形分开 —— 尤其第 3 条（本项目真踩过）。

    不带这个自证的探针是危险的：它要是把「合成事件（热键注入的回环）」
    判成「遥控器的键进去了」，就会得出**正好相反**的结论，
    而那种结论会让人再去折腾一轮纯软件方案。
    """
    import contextlib
    import io

    def ev(t, name, scan, phase):
        return Sample(t, "down", name, scan, phase)

    cases: list[tuple[str, list[Sample], str, bool]] = [
        ("取样段空（只按了打字键）→ 没进 Windows",
         [ev(1, "a", 30, "baseline"), ev(12, "b", 48, "sample")],
         "NOT_REACHING", True),
        ("取样段出现遥控器特征键 → 进了 Windows",
         [ev(12, "volume up", 175, "sample"), ev(13, "left", 75, "sample")],
         "REACHED_WINDOWS", True),
        ("⚠ 只有合成事件（scan<0）→ 必须仍判没进",
         [ev(11, "reserved ", -252, "sample"), ev(11, "f13", -124, "sample"),
          ev(11, "left ctrl", -162, "sample")],
         "NOT_REACHING", True),
        ("打字噪声过大 → 无法判定（不给结论）",
         [ev(10 + i * 0.1, "x", 45, "sample") for i in range(40)],
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
    print(f"  第 1 段（{BASELINE_SECONDS} 秒）：**什么都别按** —— 先量本底")
    print(f"  第 2 段（{args.seconds} 秒）：**只按遥控器上的键**，反复按，别打字")
    print()
    print("  ⚠ 先退掉安装版 RemoteVoiceBridge.exe（它的钩子会把按键也报一遍，噪声翻倍）")
    print()
    input("  准备好按回车开始…")

    col = Collector(started=time.time())
    keyboard.hook(col.on_key, suppress=False)

    col.phase = "baseline"
    for left in range(BASELINE_SECONDS, 0, -1):
        print(f"\r  基线中… {left:2d}s（别按任何键）", end="", flush=True)
        time.sleep(1)
    print()

    col.phase = "sample"
    print()
    print("  👉 现在只按遥控器的键（方向/确认/返回/音量），反复按")
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
