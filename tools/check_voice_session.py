"""闸：语音会话状态机不许「自己把自己关掉」。

## 为什么要有这道闸

2026-09-22 22:44 真机日志。武哥的原话：

> 「刚才好久都没办法语音输入……只有遥控有声音，电脑麦克风没有声音。」

日志里的时序（一秒钟内的三件事）：

```
22:44:53.515  ▶ Audio START          ← 第 1 个（我们补发 MIC_OPEN 的回响）
22:44:53.515  🎤 松手后自动重新开麦 → 遥控器麦克风继续收音
22:44:53.534  ▶ Audio START          ← 第 2 个，隔了 19ms
22:44:53.538  🎤 voice hotkey TAP    ← 又注入一次热键 = 把输入法语音输入**关掉**
22:44:53.625  🎙️ 语音会话【结束】（第二次按下语音键）   ← 会话被自己关了
```

22:00 之后的 10 次会话里，**4 次在 1.2~1.6 秒内被结束**。用户按一下键、
刚开口说第一个字，程序就把输入法关了 —— 一个字都出不来。

### 根因：防自激只会挡**一次**

遥控器是 PTT 硬件：按下发 `audio_start`、松手发 `audio_stop`。而用户要的是
"按一下开始、松手继续说"，所以松手时会**补发 MIC_OPEN** 让遥控器接着推流，
遥控器收到后会**回响**一个 `audio_start`（那是我们要来的，不是用户又按了一次）。

原来的写法是「挡掉一个回响，同时把 `mic_reopen_at` 清零」：

    if mic_reopen_at and (time.time() - mic_reopen_at) < MIC_REOPEN_GRACE:
        mic_reopen_at = 0.0          # ← 只挡这一次
        logger.debug("↩ 忽略自动重开麦触发的 audio_start")
    elif not voice_active:
        ...开始
    else:
        ...结束                        # ← 第 2 个回响落到这里

事实是回响**不止一个**（实测 19ms 内来两个）。第一个被吃掉并清零之后，
第二个就落进 `else` → 当成"用户第二次按下" → 注入热键 + 结束会话。

"清零"原本大概是想"只忽略最靠近补发的那一次，别把用户真按的第二次也吞掉"。
但代价方向搞反了：
  · 少吞一个回响 → 会话被莫名其妙关掉、输入法被关（**用户当场用不了**）
  · 多吞一个真按键 → 会话多听一会儿，再按一下就结束（**无害**）
按定案的规矩：**拿不准就往"不做"那边倒**（这里"不做"= 不做那个结束动作）。

## 这道闸钉什么

1. 防自激分支**不许**出现"挡掉后把 `mic_reopen_at` 清零"（＝只挡一次）。
2. 挡掉的事件**不许静默**（挡了多少次要能看见 —— 不然这次排查根本看不见它）。
3. **松手（audio_stop）绝不结束会话** —— 它只能"结算统计 + 补开麦"。
   `voice_hotkey_up()` 只允许出现在 `audio_start` 的结束分支里。
4. 帧计数不在 `audio_start` 分支顶部清零（v1.0.13 的老账，见
   `check_send_after_voice.py`；这条在这里再钉一遍，因为它是同一个状态机）。

带 2 条反例自证。

用法： python tools/check_voice_session.py
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _utf8 import setup as _setup_utf8  # noqa: E402
_setup_utf8()

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "main.py")


def _code_only(text: str) -> str:
    """剥掉注释行和 logger 调用行，只留真正会执行的东西。

    ⚠ 不剥就会自己绊自己：本文件的注释里**逐字引用了出问题的老写法**
    （`mic_reopen_at = 0.0`），直接 `in` 匹配会永远为真 —— 反例也就报不出红。
    （`check_audio_watchdog.py` 的 `except queue.Full`、
    `check_takeover_guard.py` 的 `Disable-PnpDevice` 都栽在同一件事上。）
    """
    keep = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#") or s.startswith("logger."):
            continue
        keep.append(ln)
    return "\n".join(keep)


def _block(src: str, start_pat: str, end_pat: str) -> str:
    m = re.search(start_pat + r"(.*?)(?=" + end_pat + r")", src, re.S)
    return m.group(1) if m else ""


def audit(src: str) -> list[tuple[bool, str]]:
    c: list[tuple[bool, str]] = []
    code = _code_only(src)

    # ── ① 防自激：窗口内必须能吞**多个**回响 ──────────────────────
    c.append(("MIC_REOPEN_GRACE" in src, "① 有 MIC_REOPEN_GRACE 窗口常量"))
    m_echo = re.search(
        r"if mic_reopen_at and \(time\.time\(\) - mic_reopen_at\) < MIC_REOPEN_GRACE:(.*?)elif not voice_active:",
        code, re.S)
    c.append((bool(m_echo), "① 找得到「挡自动重开麦回响」这个分支"))
    echo_body = m_echo.group(1) if m_echo else ""
    c.append(("mic_reopen_at = 0.0" not in echo_body,
              "① 挡掉回响后**没有**把 mic_reopen_at 清零"
              "（清零＝只挡一次 → 第 2 个回响落进结束分支 → 会话被自己关掉）"))
    c.append(("_echo_swallowed" in echo_body,
              "① 挡掉的事件记了数（吞了多少次要看得见，不然排查时它是个黑洞）"))

    # ── ② 松手不许结束会话 ────────────────────────────────────────
    m_stop = _block(code, r'elif event\["type"\] == "audio_stop":',
                    r'elif event\["type"\] == "mic_open_result":')
    c.append((bool(m_stop), "② 找得到 audio_stop 分支"))
    c.append(("voice_hotkey_up()" not in m_stop,
              "② 松手分支里**没有** voice_hotkey_up()"
              "（松手≠语音结束；一旦在这儿结束，就变成「按下开启、松手立刻关」）"))
    c.append(("ensure_mic_open()" in m_stop,
              "② 松手后会补发 MIC_OPEN（让遥控器继续收音）"))
    c.append(("atvv.state.stream_active = True" in m_stop,
              "② 补开麦时顺手置 stream_active（否则 atvv 层把后续帧全丢掉）"))

    # ── ③ 松手分支不许出现任何"结束语义" ──────────────────────────
    c.append(("voice_active = False" not in m_stop,
              "③ 松手分支里**没有**把 voice_active 置 False"
              "（松手只结算统计 + 补开麦；结束只可能来自「再一次按下」）"))

    # ── ④ 帧计数不许在 audio_start 顶部清零 ──────────────────────
    m_as = _block(code, r'elif event\["type"\] == "audio_start":',
                  r'elif event\["type"\] == "audio_stop":')
    if m_as:
        i_reset = m_as.find("_audio_frames = 0")
        i_down = m_as.find("voice_hotkey_down()")
        c.append((i_reset >= 0 and i_down >= 0 and i_reset < i_down,
                  "④ 帧计数零在「开始新一段」里、且在往下按热键之前"
                  "（否则收尾那次读到 0 → 零帧误触保护把每次发送都拦掉）"))
        c.append(("cancel_voice_send(" in m_as,
                  "④ 开始新一段时取消上一条待发送"))
    else:
        c.append((False, "④ 找得到 audio_start 分支"))

    # ── ⑤ 结束分支要真的调了热键松开 ──────────────────────────────
    m_end = re.search(r"else:\s*\n\s*voice_hotkey_up\(\)(.*?)\n            elif ",
                      code, re.S)
    c.append((bool(m_end), "⑤ 结束分支（第二次按下）里有 voice_hotkey_up()"))
    c.append((bool(m_end) and "request_voice_send(" in (m_end.group(1) if m_end else ""),
              "⑤ 结束分支登记了发送（含帧数，供零帧保护判断）"))
    return c


def main() -> int:
    src = open(SRC, encoding="utf-8").read()
    checks = audit(src)
    ok = True
    print("=" * 74)
    print(" 闸：语音会话状态机 —— 不许自己把自己关掉")
    print("=" * 74)
    for good, name in checks:
        print(f"  {'✅' if good else '❌'} {name}")
        if not good:
            ok = False

    print()
    print("── 反例（改坏后应当报红）──")
    n_red = 0

    # 反例 1：把「只挡一次」加回去（这就是 2026-09-22 的真 bug）
    bad1 = re.sub(r"(\n(\s+)_echo_swallowed \+= 1)",
                  r"\1\n\2mic_reopen_at = 0.0", src, count=1)
    if bad1 != src:
        n = sum(1 for g, _ in audit(bad1) if not g)
        if n:
            n_red += 1
            print(f"  ✅ 反例：把「挡一次就清零 mic_reopen_at」加回去"
                  f"（＝第 2 个回响把会话关掉） → 报了 {n} 项红")
        else:
            print("  ❌ 反例：把「只挡一次」加回去居然全绿 —— 这道闸没用")
            ok = False
    else:
        print("  ❌ 反例 1 没构造出来（锚点没找到）")
        ok = False

    # 反例 2：让松手也去结束会话
    m_stop = _block(_code_only(src), r'elif event\["type"\] == "audio_stop":',
                    r'elif event\["type"\] == "mic_open_result":')
    if m_stop:
        bad2 = src.replace(
            'logger.info("🎤 松手后自动重新开麦 → 遥控器麦克风继续收音")',
            'logger.info("🎤 松手后自动重新开麦 → 遥控器麦克风继续收音")\n'
            '                        voice_hotkey_up()', 1)
        if bad2 != src:
            n = sum(1 for g, _ in audit(bad2) if not g)
            if n:
                n_red += 1
                print(f"  ✅ 反例：让松手也调 voice_hotkey_up()"
                      f"（＝按下开启、松手立刻关） → 报了 {n} 项红")
            else:
                print("  ❌ 反例：松手结束会话居然全绿 —— 这道闸没用")
                ok = False
        else:
            print("  ❌ 反例 2 没构造出来（锚点没找到）")
            ok = False
    else:
        print("  ❌ 反例 2 没构造出来（找不到松手分支）")
        ok = False

    if n_red < 2:
        ok = False

    print()
    print("PASS" if ok else "FAIL —— 打 ❌ 的那几条会让「按一下、刚开口，"
                          "输入法就被程序自己关掉」复发")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
