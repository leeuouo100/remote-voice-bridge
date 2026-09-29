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
   发出时记一条"欠一声回声"；**写入真正成功**时把锚点挪过去，而且
   **只挪时刻、不加计数**（加了 → 账永远还不清 → 真按键被吞）。
1''''. **写入的"真实结果"必须回到状态机**（2026-09-29 审查报告 P2-2）：
   `_send_tx` 的返回值只代表"投递到事件循环"，**不代表写成功**。
   GATT 写失败时必须 ① 销掉那笔回声账 ② **复位 `mic_open_sent`** ——
   不复位的话 `ensure_mic_open()` 会永远以为已经开过麦而直接 return，
   遥控器**一帧音频都不推**，而日志上只有一行"补发成功"（静默失效）。
   顺序铁律：`_on_mic_open` 里**先记账、再排队**（反过来的话，投递一返回
   主循环就可能立刻回调"失败销账"，而这边还没记账 → 账永远挂着）。
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
                     r"def _undo_mic_open_scheduled\(")
    c.append((bool(m_sched), "①''' 有 _mark_mic_open_scheduled（发出那一刻记账）"))
    c.append(("pending_mic_echo += 1" in m_sched
              and "mic_echo_since = time.time()" in m_sched,
              "①''' 发出时记一条「欠一声回声」+ 记时刻"))

    # ── ①'''' P2-2：写入的**真实结果**必须回到状态机 ───────────────
    # 老写法把"投递到事件循环"当成功返回，于是 GATT 写失败时状态机不回滚：
    # mic_open_sent 一直是 True ⇒ ensure_mic_open 永远直接 return ⇒
    # 遥控器一帧音频都不推，而日志上只有一行"补发成功"（静默失效）。
    m_done = _block(code, r"def _on_mic_open_done\(ok: bool\):",
                    r"def _on_mic_open\(sid: int\):")
    c.append((bool(m_done), "①'''' 有 _on_mic_open_done（写入真实结果回传，P2-2）"))
    c.append(("mic_echo_since = time.time()" in m_done,
              "①'''' 写入真正成功时把锚点挪过去"
              "（GATT 写入慢到 ~1s ⇒ 回声晚 ~1s；不挪就漏出窗口 → 按下就掉）"))
    c.append(("pending_mic_echo +=" not in m_done,
              "①'''' 写入成功**只挪时刻、不加计数**"
              "（加了＝账永远还不清 → 下一条真按键被吞）"))
    c.append(("pending_mic_echo > 0" in m_done,
              "①'''' 挪时刻只在「还欠着回声」时做"
              "（否则一次迟到的写入回调会把已经销完账的窗口重新拉开）"))
    c.append(("_undo_mic_open_scheduled(" in m_done,
              "①'''' 写入**失败**时销掉「欠一声回声」那笔账"
              "（不销＝窗口一直挂着，下一条真按键可能被误吞）"))
    # ⚠ 复位要走 `session.mark_mic_open_failed()`，**不许**在这里直接改
    #   `session.state.mic_open_sent` —— 那个字段是状态机的一部分，必须和相位
    #   迁移在同一把锁里改（P2-3）。这条判据原先认的是直接赋值，P2-3 改完之后
    #   当场报红 —— 正是它该做的事（改动跑偏了会被拦住）。
    c.append(("mark_mic_open_failed()" in m_done,
              "①'''' 写入失败时撤销「已开麦」标记（走 session.mark_mic_open_failed()，"
              "与相位迁移同一把锁）"
              "（不复位＝ensure_mic_open 永远直接 return，遥控器一帧都不推，"
              "而日志上只有一行「补发成功」）"))

    m_onopen = _block(code, r"def _on_mic_open\(sid: int\):", r"def _on_mic_close")
    c.append(("_mark_mic_open_scheduled()" in m_onopen,
              "①'''' 发出后立刻记账（就在 _on_mic_open 里）"))
    # ⚠ 顺序：**先记账、再排队**。反过来的话，投递一返回主循环就可能立刻
    #   写完并回调"失败销账"，而这边还没 += 1 —— 销账销在记账之前，
    #   账上就永远挂着 1 笔"欠一声回声"（下一条真按键可能被误吞）。
    i_mark = m_onopen.find("_mark_mic_open_scheduled()")
    i_send = m_onopen.find("_send_tx(")
    c.append((i_mark >= 0 and i_send > i_mark,
              "①'''' 顺序：先 _mark_mic_open_scheduled() 再 _send_tx()"
              "（反了＝写入回调的销账可能跑在记账之前 → 账永远挂着）"))
    c.append(("on_done=" in m_onopen,
              "①'''' 把写入的**真实结果**回调接给了 _send_tx（on_done）"))

    m_sendtx = _block(code, r"def _send_tx\(cmd: bytes", r"def _mark_mic_open_scheduled\(\):")
    c.append(("on_done(ok)" in m_sendtx and "if on_done is None" in m_sendtx,
              "①'''' _send_tx 把写入结果（成功/失败）都回调出去"
              "（没有 on_done，调用方就无从回滚 —— P2-2）"))
    c.append(("GattCommunicationStatus.SUCCESS" in m_sendtx,
              "①'''' 回调的判据是 GATT status 成功（不是「排上了就算成功」）"))
    # ⚠ 用**计数**而不是"有没有出现"：`_fire(False)` 在"写入抛异常"那条路上
    #   本来就有一次，只判"出现过"的话，把"连排队都没排上"那次删掉也照样绿
    #   （反例 4d 自证过这一点）。
    #   三条路各一次 + 函数定义本身一次 = 4。
    c.append((m_sendtx.count("_fire(") >= 4,
              "①'''' 三条路都要回调 on_done：写入有结果 / 写入抛异常 / 连排队都没排上"
              f"（当前 _fire( 出现 {m_sendtx.count('_fire(')} 次；"
              "漏了最后一条＝那笔回声账永远挂着、销不掉）"))

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

    # ── ⑤ 结束分支：必须走唯一的收尾入口 ─────────────────────────
    # ⚠ 原先这里是 `else:\n voice_hotkey_up()` 那个形态。收尾归一之后
    #   （2026-09-29 审查报告 P0-1）分支里改成调 end_voice_session() ——
    #   松热键/发 MIC_CLOSE/撤 stream_active/清记账 都在它内部。
    #   断言跟着改，但**要求更严**：不只是"松了热键"，而是"走了唯一入口"。
    m_end = re.search(r'end_voice_session\("第二次按下语音键"', code)
    c.append((bool(m_end), "⑤ 结束分支（第二次按下）走了 end_voice_session()"))
    c.append((bool(m_end) and "send=True" in code[
        code.find('end_voice_session("第二次按下语音键"'):][:60],
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

    # ── ⑦ 会话收尾归一：6 条路径必须走同一个入口（P0-1）──────────
    #
    # 2026-09-29 审查报告 P0-1 的核心，也是真机"已经关了、遥控器还在收音"的
    # 直接原因：收尾原先有 6 条路径、各写各的，而**让遥控器停止推流**
    # （发 MIC_CLOSE + 撤 `atvv.state.stream_active`）**一处都没做**。
    # 真机实证：会话 2026-09-29 11:26:34 结束，遥控器一路推到 11:27:25
    # （多推 52 秒 / 3161 帧），全程没有任何 MIC_CLOSE；用户看到的正是
    # 「输入法早关了，波形还在动、还在显示在收音」。
    c.append(("def end_voice_session(" in code,
              "⑦ main.py 里有唯一的收尾入口 end_voice_session()"))

    evs = _indent4_block(src, "    def end_voice_session")
    c.append((bool(evs), "⑦ 找得到 end_voice_session 的函数体"))
    c.append(("session.close(" in evs,
              "⑦ 收尾会调 session.close() 发 MIC_CLOSE"
              "（⚠ 不能用 voice_key_up/_try_close：它们有「模式」和「相位」两道门槛，"
              "确认键/超时/断连那几条路径根本进不去 → 遥控器永远收不到关麦命令）"))
    c.append(("atvv.state.stream_active = False" in evs,
              "⑦ 收尾立刻撤 atvv 的推流许可"
              "（只发 MIC_CLOSE 不够：on_audio/decode_audio 都先看这个标志，"
              "不撤的话后续帧照样喂进 UI 和混音队列 → 波形还在动）"))
    c.append(("state.end_session(" in evs, "⑦ 收尾撤 UI 状态"))
    c.append(("pending_mic_echo = 0" in evs,
              "⑦ 收尾清 pending_mic_echo"
              "（留着它 → 下一段的第一下真按键被当回声吞掉）"))
    c.append(("voice_active = False" in evs and "voice_started_at = 0.0" in evs,
              "⑦ 收尾清会话记账（voice_active / voice_started_at）"))
    c.append(("voice_hotkey_up()" in evs,
              "⑦ 收尾释放输入法热键（放在最前面：下面任何一步抛异常"
              "也不能把 Ctrl/Win 按着不放）"))
    c.append(("if send:" in evs and "request_voice_send(" in evs
              and "cancel_voice_send(" in evs,
              "⑦ 自动发送按策略「登记或取消」（两条路都要有）"))

    # 6 条路径都要接上 —— 漏一条等于没有（本项目反复踩的坑）
    for reason in ("第二次按下语音键", "确认键", "厂商页确认键",
                   "超时自动收尾", "断开或退出"):
        c.append((f'end_voice_session("{reason}"' in code,
                  f"⑦ 收尾路径「{reason}」走的是同一个入口"))
    c.append((len(re.findall(r"end_voice_session\(", code)) >= 6,
              "⑦ 至少 5 处调用 + 1 处定义（漏一条路径 = 那条路静默失效）"))

    # 旧写法不许复活：各写各的正是"漏掉 MIC_CLOSE"的根源
    c.append((re.search(r"voice_hotkey_up\(\)\s*\n\s*state\.end_session\(", code) is None,
              "⑦ 不再有「自己松热键 + 自己撤 UI」的老形态"
              "（那样写就绕过了 MIC_CLOSE / stream_active，遥控器不会停）"))
    c.append((re.search(r'state\.end_session\("语音结束（确认键）"\)', code) is None,
              "⑦ 确认键那条不再自己写 state.end_session"
              "（收尾机制只留一份，改一处就全对）"))

    # 策略断言：只有"用户明确说完了"的两条才登记自动发送
    c.append(('end_voice_session("确认键", send=swallow)' in code,
              "⑦ 键盘确认键那条按 swallow 决定发不发"
              "（swallow=False 时这一下 OK 自己就变 Enter，补了就是连发两下）"))
    c.append(('end_voice_session("超时自动收尾", send=False)' in code,
              "⑦ 超时收尾**不**自动发送"
              "（麦克风可能开着 10 分钟，里面可能是环境音/旁人的话）"))

    # 断连/退出那条：必须**自己 await** 一次写入。
    # ⚠ 这段收尾原先在 `run_bridge` 的 `finally` 里；2026-09-29（P1-3）之后
    #   搬进了 `_BridgeResources.teardown()` —— 定位跟着改，否则取到的是那层
    #   薄壳，两条断言都会**因为找不到而变红**（闸门看着还"有效"，其实看错了地方）。
    #   仍然必须自己 await：end_voice_session 的 _send_tx 只是"投递到事件循环"，
    #   而我们此刻**就在**那个循环的收尾里 —— asyncio.run 一收尾，那条还没跑到的
    #   写入就被取消了：MIC_CLOSE 静默丢失，日志上还写着"已发出"。
    td = _block(src, r"    async def teardown", r"\nasync def run_bridge")
    c.append((bool(td), "⑦ 找得到 _BridgeResources.teardown（退出/断连那条收尾现在在这）"))
    c.append(("mic_close_cmd(" in td and "await asyncio.wait_for(" in td,
              "⑦ 退出/断连那条**自己 await** 写入 MIC_CLOSE"
              "（只靠 _send_tx 投递的话，循环一关就被取消 → 命令静默丢失）"))
    c.append(('end_voice_session("断开或退出"' in td,
              "⑦ 退出/断连那条也走同一个入口"))

    # 残留推流自愈：程序认为没会话、遥控器却还在推 → 补发 MIC_CLOSE。
    c.append(("_remote_frame_last_at" in code,
              "⑦ 有「遥控器最后推帧时刻」这个证据（_remote_frame_last_at）"))
    c.append(("session.close(\"残留推流自愈\")" in code,
              "⑦ 有「残留推流自愈」：没有会话却在收帧 → 补发 MIC_CLOSE"))
    oa = _indent4_block(src, "    def on_audio")
    i_stamp = oa.find("_remote_frame_last_at = time.time()")
    i_gate = oa.find("if not atvv.state.stream_active:")
    c.append((i_stamp >= 0 and i_gate >= 0 and i_stamp < i_gate,
              "⑦ 那个时刻必须在 stream_active 那道门**之前**记"
              "（记在门后 → 要抓的情形里它永远不刷新 → 自愈条件永远不成立，"
              "代码在、永远跑不到）"))

    # ── ⑧ 自愈的**宽限期**（2026-09-30 真机）────────────────────────────────
    #
    # 为什么要这一组：我们发完 MIC_CLOSE 之后，遥控器手里/空中的那几帧还会到
    # （实测 0.16 秒内），于是**每次正常收尾都会命中自愈**、打一句
    # 「某条收尾路径漏发了命令」—— 而上一行明明写着 `MIC_CLOSE 已发出`。
    # 真机日志里一次会话响一次，属于"狼来了"：真出事（多推 52 秒那次）时
    # 反而没人会看这一句。宽限期就是让这句告警重新变得可信。
    c.append(("_MIC_CLOSE_GRACE" in code,
              "⑧ 有 MIC_CLOSE 宽限期（没有它 → 每次正常收尾都误报「收尾漏发了命令」）"))
    c.append((code.count("_mic_close_sent_at = time.time()") >= 2,
              "⑧ 「我们最后一次关麦」的时刻两条路都记：排队时记一笔、"
              "真落地（ok=True）再挪一次（排队与落地能差 1 秒，宽限期跟着差）"))
    i_grace = code.find("<= _MIC_CLOSE_GRACE")
    i_warn = code.find("补发 MIC_CLOSE 让它停")
    c.append((i_grace >= 0 and i_warn > i_grace,
              "⑧ 宽限期判断排在告警**之前**（反过来等于没有宽限期）"))
    # ⚠ 这一条必须看**原文 src**，不能看 `code`：`_code_only` 会把
    #   `logger.debug(...)` / `logger.warning(...)` 这类调用**整行剥掉**
    #   （它认为日志文本不属于"代码"），于是 `logger.debug(` 在 code 里根本
    #   搜不到 —— 判据会永远为假。第一版就栽在这儿（当场报红）。
    s_grace = src.find("<= _MIC_CLOSE_GRACE")
    s_warn = src.find("补发 MIC_CLOSE 让它停")
    c.append((s_grace >= 0 and s_warn > s_grace
              and "logger.debug(" in src[s_grace:s_warn],
              "⑧ 宽限期内只记 debug —— 正常现象不该占 INFO 的注意力"))
    c.append(("正常情况下不该出现" not in code,
              "⑧ 告警文案里不再有那句误报（「正常情况下不该出现」）"))
    c.append(("_on_mic_close_done" in code
              and "nonlocal _mic_close_sent_at" in code,
              "⑧ 关麦时刻在回调里被挪（成功才挪；失败不挪 —— 那说明遥控器压根"
              "没收到，宽限期不该替它挡枪）"))

    # ── ⑨ 「遥控器停了，程序还在等」—— 会话挂死（v1.0.25 真机实测）──────
    #
    # 真机原样：遥控器 01:07:16 停推 → 程序一路以为「语音中」挂到 01:09:54，
    # 用户再按语音键**想说话**，却被读成「第二次按下＝结束」
    # （日志原样 `🎙️ 语音会话【结束】（第二次按下语音键）`）⇒ 热键被收起、
    # 输入法压根没被叫起来。用户感受就是「按了语音键说话，一点反应都没有」。
    # 根因：遥控器推流是每 16 ms 一帧的连续流，"收不到帧"只有一个含义 —— 它停了，
    # 而程序只认三条结束信号（再按一次 / 确认键 / 600 秒），遥控器自己停了它不知道。
    c.append(("REMOTE_SILENCE_END_SECONDS" in src,
              "⑨ 有「遥控器静默多久就认定这段语音已结束」的常量"))
    c.append(("REMOTE_SILENCE_END_SECONDS" in loop_body,
              "⑨ 反向兜底在主循环里（放 check_health 里永远跑不到 —— "
              "用户「按了开始就走开」时恰恰一个按键都没有）"))
    c.append((bool(ch_body) and "REMOTE_SILENCE_END_SECONDS" not in ch_body,
              "⑨ 反向兜底**不在** check_health 里"))
    # ⚠ 切块必须用**原文**锚点（`# ④ 反向兜底` 是注释行，_code_only 会整行剥掉），
    #   切完再各自 _code_only —— 否则注释里逐字引用的写法会把判据喂绿。
    _m4 = re.search(r"# ④ 反向兜底(.*?)# ── 语音结束后的自动发送", src, re.S)
    b4 = _code_only(_m4.group(1)) if _m4 else ""
    c.append((bool(_m4), "⑨ 找得到主循环 ④ 反向兜底那一块"))
    c.append(("_remote_frame_last_at or voice_started_at" in b4,
              "⑨ ④ 的分母是 `_remote_frame_last_at or voice_started_at`"
              "（一帧都没收到时退回「开会话那一刻」；只用前者会因分母为 0 永不成立）"))
    c.append(("voice_active" in b4,
              "⑨ ④ 先确认「会话确实开着」再收尾（否则会去收一段不存在的会话）"))
    c.append(('end_voice_session("遥控器已停止推流"' in b4,
              "⑨ ④ 走唯一的收尾入口 end_voice_session()"
              "（裸写 update(streaming=False) 会漏掉 MIC_CLOSE/回声记账）"))
    # ⚠ 本项目老病：**没有 global 声明的赋值 = 新建一个局部名** ——
    #   写进去的是局部变量，模块级那个永远停在旧值 ⇒「开会话时复位」静默失效。
    c.append(("global _audio_frames, _audio_peak, _echo_swallowed, _remote_frame_last_at"
              in code,
              "⑨ on_control 里声明了 global _remote_frame_last_at"
              "（漏了它，复位写的是局部名，等于没复位）"))
    asb = _audio_start_block(code)
    c.append(("_remote_frame_last_at = 0.0" in asb,
              "⑨ 开会话时复位「遥控器最后一次推帧」（不复位会拿着上一段的旧值"
              "算出「已静默很久」⇒ 新会话刚开就被自己收掉）"))
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
    _expect_red("让 _on_mic_open_done 成功时也把计数加一"
                "（＝账还不清 → 下一条真按键被吞）",
                re.sub(r"(def _on_mic_open_done\(ok: bool\):.*?if pending_mic_echo > 0:\n)",
                       r"\1            pending_mic_echo += 1\n", src, count=1, flags=re.S))

    # 反例 4b（P2-2）：写入失败后**不回滚** mic_open_sent
    #（＝状态机永远以为已经开麦 → ensure_mic_open 永远直接 return → 一帧都不推）
    _expect_red("写入失败后不回滚 mic_open_sent"
                "（＝遥控器一帧音频都不推，日志上却写着「补发成功」）",
                src.replace(
                    "        if session.mark_mic_open_failed():\n",
                    "        if False:\n", 1))

    # 反例 4c（P2-2）：先排队、后记账（＝回调销账可能跑在记账之前）
    _expect_red("把 _on_mic_open 里的顺序倒过来（先 _send_tx 再记账）"
                "（＝投递一返回就可能回调销账，而账还没记上）",
                src.replace(
                    "        _mark_mic_open_scheduled()\n"
                    '        if not _send_tx(cmd, "MIC_OPEN", on_done=_on_mic_open_done):\n'
                    "            return None\n"
                    "        return cmd\n",
                    '        if not _send_tx(cmd, "MIC_OPEN", on_done=_on_mic_open_done):\n'
                    "            return None\n"
                    "        _mark_mic_open_scheduled()\n"
                    "        return cmd\n", 1))

    # 反例 4d（P2-2）：「连排队都没排上」那条路不再回调
    #（＝那笔回声账永远挂着，下一条真按键可能被误吞）
    _expect_red("「连排队都没排上」那条路不再回调 on_done(False)",
                src.replace(
                    "            _fire(False)          # 保证 on_done 恰好被调用一次\n",
                    "", 1))

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

    # ── 反例 12~17：P0-1 收尾归一（2026-09-29 审查报告）─────────────
    # 这几条都是"改回旧行为"的形状 —— 旧版就是这样，于是遥控器收不到
    # MIC_CLOSE、会话结束后又推了 52 秒。

    # 反例 12：收尾不发 MIC_CLOSE（＝旧版真机行为）
    _expect_red("收尾不调 session.close()（＝遥控器收不到关麦命令，继续推流）",
                src.replace('closed = session.close(f"会话收尾：{reason}")',
                            "closed = False", 1))

    # 反例 13：收尾不撤 atvv 的推流许可
    #（＝ on_audio 继续把帧喂进 UI，波形还在动）
    _expect_red("收尾不撤 atvv.state.stream_active"
                "（＝后续帧照样喂进 UI 和混音队列 → 波形还在动）",
                src.replace("        atvv.state.stream_active = False\n",
                            "        pass\n", 1))

    # 反例 14：收尾不清 pending_mic_echo（＝下一段第一下被当回声吞掉）
    _expect_red("收尾不清 pending_mic_echo"
                "（＝下一段的第一下真按键被当回声吞掉，按一下没反应）",
                src.replace("        pending_mic_echo = 0\n", "        pass\n", 1))

    # 反例 15：超时收尾改成自动发送（＝把环境音/旁人的话替用户发出去）
    _expect_red("超时收尾改成 send=True"
                "（＝把可能全是环境音的 10 分钟内容自动发进聊天框）",
                src.replace('end_voice_session("超时自动收尾", send=False)',
                            'end_voice_session("超时自动收尾", send=True)', 1))

    # 反例 16：断连/退出那条路径不接入口
    #（＝程序关了遥控器还在推流，只能等它自己超时）
    _expect_red("断连/退出那条不调 end_voice_session"
                "（＝程序都关了，遥控器还在收音）",
                src.replace('end_voice_session("断开或退出", send=False)',
                            "pass", 1))

    # 反例 17：把"遥控器还在推流"的证据记在 stream_active 那道门**之后**
    #（＝要抓的情形里它永远不刷新 → 自愈条件永远不成立，代码在、跑不到）
    _expect_red("把 _remote_frame_last_at 挪到 stream_active 门**之后**"
                "（＝残留推流时它永远不刷新 → 自愈永远不触发）",
                src.replace(
                    "        _remote_frame_last_at = time.time()\n"
                    "        if not atvv.state.stream_active:\n"
                    "            return",
                    "        if not atvv.state.stream_active:\n"
                    "            return\n"
                    "        _remote_frame_last_at = time.time()", 1))

    # 反例 18：去掉 MIC_CLOSE 宽限期（＝每次正常收尾都误报「收尾漏发了命令」，
    #          真正的残留推流反而淹没在噪声里）
    _expect_red("去掉自愈的宽限期判断（＝告警在每次正常收尾时都响）",
                src.replace("(_now_v - _mic_close_sent_at) <= _MIC_CLOSE_GRACE",
                            "False", 1))

    # 反例 19：真落地时不再挪关麦时刻（＝宽限期按"排队时刻"起算，
    #          落地慢 1 秒就白白多等/少等一秒）
    _expect_red("_on_mic_close_done 成功时不再挪关麦时刻",
                src.replace(
                    "        nonlocal _mic_close_sent_at\n"
                    "        if ok:\n"
                    "            _mic_close_sent_at = time.time()\n"
                    "            return\n",
                    "        nonlocal _mic_close_sent_at\n"
                    "        if ok:\n"
                    "            return\n", 1))

    # 反例 20：把那句误报文案加回去
    _expect_red("把「正常情况下不该出现」那句误报加回告警里",
                src.replace("这说明它**真的**没停下来",
                            "正常情况下不该出现，这说明它**真的**没停下来", 1))

    # 反例 21：宽限期内改用 warning 打（＝正常现象又变回噪声）
    _expect_red("宽限期内改用 warning 打（＝正常现象又变回噪声）",
                src.replace('logger.debug(\n                        "（正常）刚发过',
                            'logger.warning(\n                        "（正常）刚发过',
                            1))

    # 反例 22（v1.0.25）：④ 的分母退回 `_remote_frame_last_at`（一帧都没收到时
    #          它是 0 ⇒ 条件永不成立 ⇒ 又变回"代码在、永远跑不到"）
    _expect_red("④ 的分母去掉 `or voice_started_at`（＝一帧都没收到时永不成立）",
                src.replace("_remote_frame_last_at or voice_started_at",
                            "_remote_frame_last_at", 1))

    # 反例 23（v1.0.25）：漏掉 global 声明（＝复位写进局部名，模块级那个不动）
    _expect_red("on_control 漏声明 global _remote_frame_last_at"
                "（＝开会话时的复位静默失效）",
                src.replace(
                    "        global _audio_frames, _audio_peak, _echo_swallowed, "
                    "_remote_frame_last_at",
                    "        global _audio_frames, _audio_peak, _echo_swallowed", 1))

    # 反例 24（v1.0.25）：开会话时不复位（＝拿着上一段的旧值算出「已静默很久」，
    #          新会话刚开就被自己收掉）
    _expect_red("开会话时不再复位「遥控器最后一次推帧」",
                src.replace("                    _remote_frame_last_at = 0.0\n",
                            "", 1))

    # 反例 25（v1.0.25）：④ 绕过唯一收尾入口（＝不撤推流许可、不清回声记账）
    _expect_red("④ 绕过 end_voice_session 收尾（裸写 state.update(streaming=False)）",
                src.replace('end_voice_session("遥控器已停止推流", send=True)',
                            'state.update(streaming=False)', 1))

    if n_red < 22:
        ok = False

    print()
    print("PASS" if ok else "FAIL —— 打 ❌ 的那几条会让「按一下、刚开口，"
                          "输入法就被程序自己关掉」复发，"
                          "或让「关了还显示收音中」复发")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
