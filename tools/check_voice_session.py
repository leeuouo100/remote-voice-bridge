"""闸：语音会话状态机不许「自己把自己关掉」。

## 为什么要有这道闸

这个状态机的核心只有一句话：**遥控器发来的 `audio_start` 有两种含义**
（用户按下了语音键 / 它只是回了我们一声"我开始推流了"），而两者在协议上
**一模一样**，只能靠"我们刚发过 MIC_OPEN"这个上下文去分。分错了的代价是
**用户当场用不了**，而且不崩、不报错、UI 波形还在跳 —— 静默失效。
所以这条判据必须由闸门钉死，不能靠自觉。

## 这道闸钉什么

1. **窗口宽度** `ECHO_MAX_AGE` 必须落在 [1.2, 3.0] 秒。
   真机实测（2026-09-23，239 次 MIC_OPEN / 239 次都配上回声）：
   延迟中位 20ms、**最大 1070ms**。
   · < 1.2 → 慢写入那几次的回声漏出窗口 → 被当成真按键 → **按下就掉**
   · > 3.0 → 真按键被当成回声吞掉
1'. **判据不能只看时间，还要看"那声账还没销"**：必须同时有
   `pending_mic_echo > 0`。只比时间的话，回声之后 1.5s 内用户的真按键
   会被一起吞掉。
1''. **回声分支只许销账、不许改时刻**（`pending_mic_echo -= 1`，
   绝不出现 `mic_echo_since =`）。改了时刻 = 窗口被回声续命 = 窗口永不过期
   = 按键全被吞（v1.0.13 的下场）。
1'''. **记账必须收在 MIC_OPEN 的发送路径里**（`_on_mic_open` → `_send_tx`）：
   排队成功记一条"欠一声回声"；**写入真正成功**时把锚点挪过去，而且
   **只挪时刻、不加计数**（加了 → 账永远还不清 → 真按键被吞）。
1''''. **顺序铁律：先算 `is_echo`，再补发 MIC_OPEN。**
2. 挡掉的事件不许静默（挡了多少次要看得见）。
3. **松手（audio_stop）绝不结束会话** —— 只能"结算统计 + 补开麦"。
4. 帧计数不在 `audio_start` 分支顶部清零，**且必须在判回声之后**。

## 翻车史（三个版本连着栽在同一件事上，方向还不一样）

| 版本 | 做法 | 结果 |
|---|---|---|
| v1.0.11 | 吞掉回声时**清零** `mic_reopen_at` | 只挡一次 → 第 2 个回声落进结束分支 → 4/10 次会话 1.2~1.6s 被自己关掉 |
| v1.0.13 | 不清零，但**顺序错**（补开麦排在判回声之前＋顺手记时刻） | **真按键自己判成回声** → 每次按键都重来 → 热键一次没注入（一行 `🎤 voice hotkey TAP` 都没有） |
| v1.0.14 | 顺序对，但窗口收成 **0.8s**（依据是"回声最长 350ms"这个**假数**） | 那个 350ms 是拿"**排队**时刻"量的；真实 GATT 写入慢到 ~1s → 回声晚 ~1s → 8 次漏出窗口 → **按下就掉** |

⚠ v1.0.14 的教训最值得记：**测量口径错了，改出来的数字就是错的**。
   "排队成功"和"遥控器真的收到了"之间能差 1 秒，量回声必须锚在**写入完成**上。

带反例自证（改坏后必须报红），见文件末尾。

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


def _strip_docstrings(text: str) -> str:
    """去掉三引号字符串（docstring）。

    ⚠ 必须剥：本文件里那些"为什么"的说明**逐字引用了老写法**
    （`mic_reopen_at = 0.0`、`pending_mic_echo > 0`），`in` 匹配会把它们
    当成真的代码 —— 于是改坏了也照绿，反例也就报不出红。
    （`check_audio_watchdog.py` 的 `except queue.Full`、
    `check_takeover_guard.py` 的 `Disable-PnpDevice` 都栽在同一件事上。）
    """
    return re.sub(r'""".*?"""', "", text, flags=re.S)


def _code_only(text: str) -> str:
    """剥掉注释行、logger 调用行、docstring，只留真正会执行的东西。"""
    keep = []
    for ln in _strip_docstrings(text).splitlines():
        s = ln.strip()
        if s.startswith("#") or s.startswith("logger."):
            continue
        keep.append(ln)
    return "\n".join(keep)


def _block(src: str, start_pat: str, end_pat: str) -> str:
    m = re.search(start_pat + r"(.*?)(?=" + end_pat + r")", src, re.S)
    return m.group(1) if m else ""


def _audio_start_block(code: str) -> str:
    return _block(code, r'elif event\["type"\] == "audio_start":',
                  r'elif event\["type"\] == "audio_stop":')


def _audio_stop_block(code: str) -> str:
    return _block(code, r'elif event\["type"\] == "audio_stop":',
                  r'elif event\["type"\] == "mic_open_result":')


def audit(src: str) -> list[tuple[bool, str]]:
    c: list[tuple[bool, str]] = []
    code = _code_only(src)

    # ── ① 窗口常量与区间 ──────────────────────────────────────────
    c.append(("ECHO_MAX_AGE" in code, "① 有 ECHO_MAX_AGE 窗口常量"))
    m_age = re.search(r"^ECHO_MAX_AGE\s*=\s*([0-9.]+)", code, re.M)
    age = float(m_age.group(1)) if m_age else 0.0
    c.append((1.2 <= age <= 3.0,
              f"① 窗口宽度落在 [1.2, 3.0] 秒（当前 {age}s）"
              "—— 实测回声最慢 1070ms：<1.2 会漏（v1.0.14 的 0.8 ＝「按下就掉」）、"
              ">3 会把真按键一起吞"))

    m_asblk = _audio_start_block(code)

    # ── ①' 判据：时间 + 「欠一声回声」两条都要 ─────────────────────
    m_is = re.search(r"is_echo\s*=\s*\((.*?)\)\s*\n", m_asblk, re.S)
    if not m_is:
        m_is = re.search(r"is_echo\s*=\s*([^\n]+)", m_asblk)
    is_body = m_is.group(1) if m_is else ""
    c.append((bool(m_is), "①' 找得到 is_echo 判据"))
    c.append(("pending_mic_echo" in is_body,
              "①' 判据里有 pending_mic_echo（'欠一声回声'的账还没销）"
              "—— 只看时间窗，回声之后 1.5s 内用户的真按键会被一起吞掉"))
    c.append(("mic_echo_since" in is_body,
              "①' 判据里比对 mic_echo_since（锚点＝写入**真正成功**那一刻）"))

    # ── ①'' 回声分支：只销账，不许改时刻 ──────────────────────────
    m_echo = re.search(r"if is_echo:(.*?)elif not voice_active:", code, re.S)
    echo_body = m_echo.group(1) if m_echo else ""
    c.append((bool(m_echo), "①'' 找得到「挡回声」这个分支"))
    c.append((not re.search(r"mic_echo_since\s*=", echo_body),
              "①'' 回声分支里**从不**改 mic_echo_since"
              "（改时刻＝窗口被回声续命 → 永不过期 → 按键全被吞，v1.0.13）"))
    c.append((re.search(r"pending_mic_echo\s*=\s*max\(0", echo_body) is not None,
              "①'' 回声一到就把那笔账销掉（pending_mic_echo 减一）"
              "—— 不销账的话下一条真按键会被误吞"))
    c.append(("_echo_swallowed" in echo_body,
              "①'' 挡掉的事件记了数（吞了多少次要看得见，不然排查时它是个黑洞）"))

    # ── ①''' 记账收在 MIC_OPEN 发送路径里 ────────────────────────
    m_sched = _block(code, r"def _mark_mic_open_scheduled\(\):",
                     r"def _mark_mic_open_written\(\)")
    c.append((bool(m_sched), "①''' 有 _mark_mic_open_scheduled（排队那一刻记账）"))
    c.append(("pending_mic_echo += 1" in m_sched
              and "mic_echo_since = time.time()" in m_sched,
              "①''' 排队成功时记一条「欠一声回声」+ 记时刻"))
    m_written = _block(code, r"def _mark_mic_open_written\(\):",
                       r"def _on_mic_open\(")
    c.append((bool(m_written), "①''' 有 _mark_mic_open_written（写入成功那一刻挪锚点）"))
    c.append(("mic_echo_since = time.time()" in m_written,
              "①''' 写入真正成功时把锚点挪过去"
              "（GATT 写入慢到 ~1s ⇒ 回声晚 ~1s；不挪就漏出窗口 → 按下就掉）"))
    c.append(("pending_mic_echo +=" not in m_written,
              "①''' 写入成功**只挪时刻、不加计数**"
              "（加了＝账永远还不清 → 下一条真按键被吞）"))
    c.append(("pending_mic_echo > 0" in m_written,
              "①''' 挪时刻只在「还欠着回声」时做"
              "（否则一次迟到的写入回调会把已经销完账的窗口重新拉开）"))
    m_onopen = _block(code, r"def _on_mic_open\(", r"def _on_mic_close\(")
    c.append(("_mark_mic_open_scheduled()" in m_onopen,
              "①''' 排队成功后立刻记账（就在 _on_mic_open 里）"))
    c.append(("on_sent=" in m_onopen,
              "①''' 把写入完成的回调接给了 _send_tx（on_sent）"))
    m_sendtx = _block(code, r"def _send_tx\(cmd: bytes", r"def _mark_mic_open_scheduled\(\):")
    c.append(("on_sent is not None" in m_sendtx and "on_sent()" in m_sendtx,
              "①''' _send_tx 在写入 status=成功时才回调 on_sent"))
    c.append(("GattCommunicationStatus.SUCCESS" in m_sendtx,
              "①''' 回调的判据是 GATT status 成功（不是「排上了就算成功」）"))

    # ── ①'''' 顺序铁律：先判回声，再补开麦 ───────────────────────
    i_echo_calc = m_asblk.find("is_echo =")
    i_micopen = m_asblk.find("ensure_mic_open()")
    c.append((i_micopen >= 0,
              "①'''' audio_start 分支里补发了 MIC_OPEN"
              "（遥控器按语音键只发 AUDIO_START、从不发 START_SEARCH；"
              "少了它遥控器一帧都不推 → 连续「0 个音频帧」）"))
    c.append((i_echo_calc >= 0 and i_micopen > i_echo_calc,
              "①'''' 顺序：先算 is_echo，**再**补发 MIC_OPEN"
              "（顺序反了＝真按键把自己判成回声，当场吞掉 → v1.0.13 的「用不了」）"))

    # ── ②③ 松手不许结束会话 ──────────────────────────────────────
    m_stop = _audio_stop_block(code)
    c.append((bool(m_stop), "② 找得到 audio_stop 分支"))
    c.append(("voice_hotkey_up()" not in m_stop,
              "② 松手分支里**没有** voice_hotkey_up()"
              "（松手≠语音结束；一旦在这儿结束，就变成「按下开启、松手立刻关」）"))
    c.append(("ensure_mic_open()" in m_stop,
              "② 松手后会补发 MIC_OPEN（让遥控器继续收音）"))
    c.append(("atvv.state.stream_active = True" in m_stop,
              "② 补开麦时顺手置 stream_active（否则 atvv 层把后续帧全丢掉）"))
    c.append(("voice_active = False" not in m_stop,
              "③ 松手分支里**没有**把 voice_active 置 False"
              "（松手只结算统计 + 补开麦；结束只可能来自「再一次按下」）"))

    # ── ④ 帧计数不许在 audio_start 顶部清零 ──────────────────────
    if m_asblk:
        i_reset = m_asblk.find("_audio_frames = 0")
        i_down = m_asblk.find("voice_hotkey_down()")
        c.append((i_reset >= 0 and i_down >= 0 and i_reset < i_down,
                  "④ 帧计数零在「开始新一段」里、且在往下按热键之前"
                  "（否则收尾那次读到 0 → 零帧误触保护把每次发送都拦掉）"))
        c.append((i_reset >= 0 and i_echo_calc >= 0 and i_reset > i_echo_calc,
                  "④ 帧计数清零也在判回声**之后**"
                  "（否则回响那一下把本段帧数抹成 0）"))
        c.append(("cancel_voice_send(" in m_asblk,
                  "④ 开始新一段时取消上一条待发送"))
    else:
        c.append((False, "④ 找得到 audio_start 分支"))

    # ── ⑤ 结束分支要真的松开热键并登记发送 ───────────────────────
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

    def _expect_red(label: str, bad: str) -> None:
        nonlocal ok, n_red
        if bad == src:
            print(f"  ❌ 反例没构造出来（锚点没找到）：{label}")
            ok = False
            return
        n = sum(1 for g, _ in audit(bad) if not g)
        if n:
            n_red += 1
            print(f"  ✅ 反例：{label} → 报了 {n} 项红")
        else:
            print(f"  ❌ 反例：{label} 居然全绿 —— 这道闸拦不住它复发")
            ok = False

    # 反例 1：窗口收到 0.8s —— 这就是 v1.0.14 真机上"按下就掉"的那一版
    _expect_red("把窗口 ECHO_MAX_AGE 收回 0.8s"
                "（＝回声漏出窗口被当成真按键，按下就掉）",
                re.sub(r"^ECHO_MAX_AGE\s*=\s*[0-9.]+", "ECHO_MAX_AGE = 0.8",
                       src, count=1, flags=re.M))

    # 反例 2：判据退回"只看时间"（丢掉"欠一声回声"那笔账）
    _expect_red("把判据退回只看时间窗（丢了 pending_mic_echo）",
                re.sub(r"is_echo = \(pending_mic_echo > 0\s*\n\s*and ",
                       "is_echo = (True and ", src, count=1))

    # 反例 3：回声分支去刷新窗口（＝窗口永不过期，按键全被吞）
    _expect_red("在回声分支里刷新 mic_echo_since"
                "（＝窗口被回声续命 → 按键全被吞）",
                re.sub(r"(\n(\s+)_echo_swallowed \+= 1)",
                       r"\1\n\2mic_echo_since = time.time()", src, count=1))

    # 反例 4：写入成功后**也**加计数（＝账永远还不清，真按键被吞）
    _expect_red("让 _mark_mic_open_written 也把计数加一"
                "（＝账还不清 → 下一条真按键被吞）",
                re.sub(r"(def _mark_mic_open_written\(\):.*?if pending_mic_echo > 0:\n)",
                       r"\1            pending_mic_echo += 1\n", src, count=1, flags=re.S))

    # 反例 5：让松手也去结束会话
    _expect_red("让松手也调 voice_hotkey_up()"
                "（＝按下开启、松手立刻关）",
                src.replace(
                    'logger.info("🎤 松手后自动重新开麦 → 遥控器麦克风继续收音")',
                    'logger.info("🎤 松手后自动重新开麦 → 遥控器麦克风继续收音")\n'
                    '                        voice_hotkey_up()', 1))

    # 反例 6：把补开麦挪到判回声**之前**（v1.0.13 的病）
    _expect_red("把补开麦挪到判回声**之前**"
                "（＝ v1.0.13 真按键把自己判成回声、当场吞掉）",
                src.replace("                now = time.time()\n"
                            "                is_echo =",
                            "                session.ensure_mic_open()\n"
                            "                now = time.time()\n"
                            "                is_echo =", 1))

    if n_red < 5:
        ok = False

    print()
    print("PASS" if ok else "FAIL —— 打 ❌ 的那几条会让「按一下、刚开口，"
                          "输入法就被程序自己关掉」复发")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
