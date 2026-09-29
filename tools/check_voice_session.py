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
STATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "state.py")


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


def _indent4_block(text: str, header: str) -> str:
    """取以 `header` 开头、到下一个「恰好 4 空格缩进」的语句为止的**原文**。

    ⚠ 别用「正则 lookahead + 注释行当结束锚点」来切 check_health 的函数体：
      先过 `_code_only()` 的话，`# ── Main loop ──` 这类注释行会被整行剥掉，
      锚点落空 ⇒ 匹配不到 ⇒ 下面两条断言会**因为找不到而变绿**（假绿），
      反例也报不出红 —— 闸门看着是好的，其实什么都没验。
      （`check_packaging.py` 那边"不加引号会连注释一起匹配上"是同一类坑：
        切块/匹配的边界必须用真能对上的东西。）
    """
    lines = text.splitlines()
    i = next((k for k, l in enumerate(lines) if l.startswith(header)), -1)
    if i < 0:
        return ""
    j = i + 1
    while j < len(lines):
        if re.match(r"^    \S", lines[j]):
            break
        j += 1
    return "\n".join(lines[i:j])


def _audio_start_block(code: str) -> str:
    return _block(code, r'elif event\["type"\] == "audio_start":',
                  r'elif event\["type"\] == "audio_stop":')


def _audio_stop_block(code: str) -> str:
    return _block(code, r'elif event\["type"\] == "audio_stop":',
                  r'elif event\["type"\] == "mic_open_result":')


def audit(src: str, state_src: str = "") -> list[tuple[bool, str]]:
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

    # ── ⑥ 「关了还在显示收音中」这一族（2026-09-29 武哥报的）─────────
    #
    # 现象：语音输入已经关了，托盘图标还橙红、控制台还写「语音中」/「有声音」。
    # 两个独立的成因，都要钉住：
    #   (a) `audio_start` 分支**顶部**无条件点亮 streaming —— 而遥控器回给
    #       MIC_OPEN 的**回声** audio_start 也会走到那儿 ⇒ 会话刚结束又来一条回声，
    #       状态就被重新点亮，而唯一能清它的那条 audio_stop 可能永远不来。
    #   (b) 会话结束时只写 `update(streaming=False)` —— 遥控器那一路的
    #       **电平与波形**只在"下一次开始"时才清，于是卡片继续写「有声音」、
    #       波形冻在最后一帧，看起来也像"还在收音"。
    i_echo_top = m_asblk.find("is_echo")
    head = m_asblk[:i_echo_top] if i_echo_top >= 0 else m_asblk
    c.append(("streaming=True" not in head,
              "⑥ audio_start 分支**顶部**没有点亮 streaming"
              "（回声也会走到那儿 ⇒ 会话结束后被重新点亮 → 「关了还显示收音中」）"))

    c.append(("state.end_session(" in code,
              "⑥ 会话结束走 state.end_session(…)（不是裸的 update(streaming=False)）"))
    c.append((re.search(r"update\(streaming=False", code) is None,
              "⑥ 全文件不再有裸的 update(streaming=False, …)"
              "（只灭 streaming 不够 —— 遥控器那一路的电平/波形也得当场撤）"))

    # 主循环里必须每一轮核一遍：超时兜底 + 状态归位。
    # ⚠ 不能待在 check_health() 里 —— 它只在"最近 3 秒按过键"时才被调用，
    #   而"按了开始就走开"恰恰没有按键 ⇒ 那段代码一次都跑不到。
    m_loop = re.search(r"while True:(.*)", code, re.S)
    loop_body = m_loop.group(1) if m_loop else ""
    c.append(("VOICE_MAX_SECONDS" in loop_body,
              "⑥ 超时兜底在主循环里（放 check_health 里永远跑不到："
              "它只在最近 cfg.key_check_window 秒内按过键时才被调用）"))
    c.append(("state.get().streaming != voice_active" in loop_body,
              "⑥ 主循环里有「streaming 必须等于 voice_active」的状态归位"))

    # check_health 的函数体用**行扫描**切（见 _indent4_block 的注释：
    # 用注释行当锚点会被 _code_only 剥掉 ⇒ 匹配不到 ⇒ 假绿）。
    ch_body = _indent4_block(src, "    async def check_health")
    c.append((bool(ch_body), "⑥ 找得到 check_health 函数体"))
    c.append((bool(ch_body) and "VOICE_MAX_SECONDS" not in ch_body,
              "⑥ 超时兜底**不在** check_health 里（挪回去 = 静默失效复发）"))
    # ── ⑥' state.end_session 自己得把该撤的都撤掉 ─────────────────
    if state_src:
        st = _code_only(state_src)
        m_es = re.search(r"def end_session\(.*?(?=\ndef )", st, re.S)
        es = m_es.group(0) if m_es else ""
        c.append((bool(m_es), "⑥' state.py 里有 end_session()"))
        c.append(("streaming" in es and "False" in es,
                  "⑥' end_session 熄灭 streaming（顶部「语音中」/ 托盘橙红）"))
        c.append(("remote_level_db" in es,
                  "⑥' end_session 把遥控器那一路电平归零"
                  "（否则「遥控器麦克风」卡片会一直写「有声音」）"))
        c.append(("_wave.clear()" in es,
                  "⑥' end_session 清掉遥控器波形（否则波形冻在最后一帧）"))
        c.append(("_wave_sys" not in es,
                  "⑥' end_session **不碰**电脑麦克风那一路"
                  "（它是独立采集的，混音器还在跑，别越权去擦）"))
    return c


def main() -> int:
    src = open(SRC, encoding="utf-8").read()
    state_src = open(STATE, encoding="utf-8").read()
    checks = audit(src, state_src)
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
        n = sum(1 for g, _ in audit(bad, state_src) if not g)
        if n:
            n_red += 1
            print(f"  ✅ 反例：{label} → 报了 {n} 项红")
        else:
            print(f"  ❌ 反例：{label} 居然全绿 —— 这道闸拦不住它复发")
            ok = False

    def _expect_red_state(label: str, bad_state: str) -> None:
        """反例打在 state.py 上（⑥' 那几条）。"""
        nonlocal ok, n_red
        if bad_state == state_src:
            print(f"  ❌ 反例没构造出来（锚点没找到）：{label}")
            ok = False
            return
        n = sum(1 for g, _ in audit(src, bad_state) if not g)
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

    # 反例 7：把「无条件点亮 streaming」塞回 audio_start 分支顶部
    #（＝ 回声也点亮「语音中」→ 会话结束后被重新点亮 → 关了还显示收音中）
    _expect_red("把 state.update(streaming=True) 塞回 audio_start 分支顶部"
                "（＝回声也点亮「语音中」）",
                src.replace(
                    "                state.clear_audio()          # 清掉上一次的波形，UI 从空开始画",
                    "                state.clear_audio()          # 清掉上一次的波形，UI 从空开始画\n"
                    '                state.update(streaming=True, last_event="语音中…")', 1))

    # 反例 8：会话结束退回裸的 update(streaming=False)
    #（＝遥控器那一路的电平/波形不撤，卡片一直写「有声音」）
    _expect_red("会话结束退回裸的 update(streaming=False)"
                "（＝卡片一直写「有声音」、波形冻住）",
                src.replace('state.end_session("语音结束")',
                            'state.update(streaming=False, level=0, last_event="语音结束")', 1))

    # 反例 9：删掉主循环里的「状态归位」
    _expect_red("删掉主循环里的状态归位"
                "（＝状态一旦被点亮就再没人纠正）",
                src.replace("if state.get().streaming != voice_active:",
                            "if False:", 1))

    # 反例 10：把超时兜底挪回 check_health（＝代码在、永远跑不到）
    _expect_red("把超时兜底挪回 check_health"
                "（＝它只在最近 3 秒按过键时才被调用，永远跑不到）",
                src.replace(
                    "        if not force and now - last_health_check < cfg.heartbeat_cooldown:",
                    "        if voice_active and (now - last_health_check) > VOICE_MAX_SECONDS:\n"
                    "            pass\n"
                    "        if not force and now - last_health_check < cfg.heartbeat_cooldown:", 1))

    # 反例 11（打在 state.py 上）：end_session 忘了归零遥控器电平
    _expect_red_state("end_session 忘了把遥控器电平归零"
                      "（＝「遥控器麦克风」卡片一直写「有声音」）",
                      state_src.replace(
                          "        _state.last_event      = last_event\n"
                          "        _state.remote_level_db = -96.0\n",
                          "        _state.last_event      = last_event\n", 1))

    if n_red < 10:
        ok = False

    print()
    print("PASS" if ok else "FAIL —— 打 ❌ 的那几条会让「按一下、刚开口，"
                          "输入法就被程序自己关掉」复发，"
                          "或让「关了还显示收音中」复发")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
