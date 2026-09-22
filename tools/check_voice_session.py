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
1'. 防自激分支**也不许**刷新 `mic_reopen_at` —— 窗口**只能靠时间过期**，
   任何事件都不许改它。（清零与刷新是同一个坑的两个方向，见下。）
1''. **顺序铁律：先算 `is_echo`，再补发 MIC_OPEN。** 补开麦成功时调用方会顺手
   记 `mic_reopen_at = 现在`；它一旦排在判定之前，**这一次（真按键）自己就落进了
   回声窗口**、当场被吞掉。返回值**区间**也要管：窗口宽度 `MIC_REOPEN_GRACE`
   必须落在 [0.5, 1.2] 秒（实测回响 +22ms/+300ms，真按键 ≥1s）。
2. 挡掉的事件**不许静默**（挡了多少次要能看见 —— 不然这次排查根本看不见它）。
3. **松手（audio_stop）绝不结束会话** —— 它只能"结算统计 + 补开麦"。
   `voice_hotkey_up()` 只允许出现在 `audio_start` 的结束分支里。
4. 帧计数不在 `audio_start` 分支顶部清零，**且必须在判回声之后**
   （回响也是 audio_start；放它前面就把本段帧数抹成 0 → 又变成零帧误触）。

## v1.0.13 的回归：把"只挡一次"改成了"永不过期"

前一轮为了修"第 2 个回响把会话关掉"，把清零去掉了 —— 方向对，但**顺序**没动：
补开麦仍在 `is_echo` 判定**之前**，并顺手 `mic_reopen_at = time.time()`。
于是：

```
第 1 次按键 → Audio START → 补发 MIC_OPEN（mic_reopen_at = 现在）
                        └→ 紧接着判窗口：now - mic_reopen_at ≈ 0ms < 1.5s
                           ⇒ **这一次（真按键）自己把自己判成回声、当场吞掉**
吞掉时又不清零 → 遥控器每 ~1.3s 一个 audio_start 都把窗口续上
                ⇒ mic_reopen_at 永不失效 ⇒ **每一次按键全被吞**
```

真机证据（2026-09-22 23:34–23:35）：日志里只剩
`Audio START →(17 帧)→ Audio STOP` 原地循环，**一行 `🎤 voice hotkey TAP` 都没有**
—— 热键一次都没注入，输入法从头到尾没被叫起来。用户的原话：

> 「怎么又有问题，连语音输入都用不了，」

⚠ 这是一种**静默失效**：不崩、不报错、UI 上波形还在跳（那 17 帧），
用户却一个字都输不进去。所以顺序必须由闸门钉死，不能靠自觉。

带 3 条反例自证：①把"挡一次就清零"加回去 ②让松手也结束会话
③把补开麦挪到判回声之前（＝ v1.0.13 的真 bug）。

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
    m_grace = re.search(r"^MIC_REOPEN_GRACE\s*=\s*([0-9.]+)", src, re.M)
    grace = float(m_grace.group(1)) if m_grace else 0.0
    # 窗口宽度是**双向**代价（真机实测）：回响 +22ms/+300ms，真按键 ≥1s。
    #   < 0.5  → 回响漏过去 → 会话被自己关掉
    #   > 1.2  → 真按键被当成回响吞掉 → 用户当场用不了（v1.0.13 的 1.5 就是这样）
    c.append((0.5 <= grace <= 1.2,
              f"① 窗口宽度在安全区间 [0.5, 1.2] 秒（实测 {grace}s）"
              "—— 太窄漏回响、太宽吞真按键"))
    m_echo = re.search(r"if is_echo:(.*?)elif not voice_active:", code, re.S)
    c.append((bool(m_echo), "① 找得到「挡自动重开麦回响」这个分支"))
    echo_body = m_echo.group(1) if m_echo else ""
    c.append((not re.search(r"mic_reopen_at\s*=", echo_body),
              "① 回声分支里**从不**改 mic_reopen_at"
              "（清零＝只挡一次 → 第 2 个回响落进结束分支 → 会话被自己关掉；"
              "刷新＝窗口永不过期 → 按键全被吞 → v1.0.13）"))
    c.append(("_echo_swallowed" in echo_body,
              "① 挡掉的事件记了数（吞了多少次要看得见，不然排查时它是个黑洞）"))

    # ── ①' 顺序铁律：**先判回声，再补开麦** ─────────────────────────
    # 这是 v1.0.13 翻车的地方，也是本轮回归的病根，必须单独钉死：
    # 补开麦成功时 `ensure_mic_open()` 会顺手让调用方记 `mic_reopen_at = 现在`。
    # 一旦它排在 `is_echo` 判定**之前**，**这一次（真按键）自己就落进了回声窗口**、
    # 当场被吞掉 → 热键一次都不注入 → 输入法从头到尾没被叫起来
    # → 用户感受就是"语音输入完全用不了"。
    m_asblk = _block(code, r'elif event\["type"\] == "audio_start":',
                     r'elif event\["type"\] == "audio_stop":')
    i_echo_calc = m_asblk.find("is_echo =")
    i_micopen = m_asblk.find("ensure_mic_open()")
    c.append((i_micopen >= 0,
              "①' audio_start 分支里补发了 MIC_OPEN"
              "（遥控器按语音键只发 AUDIO_START、从不发 START_SEARCH；"
              "少了它遥控器一帧都不推 → 连续「0 个音频帧」）"))
    c.append((i_echo_calc >= 0 and i_micopen > i_echo_calc,
              "①' 顺序：先算 is_echo，**再**补发 MIC_OPEN"
              "（顺序反了＝真按键把自己判成回声，当场吞掉 → v1.0.13 的「用不了」）"))

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
        # 还得在**判回声之后**：回响也是 audio_start，会先走到这里。
        # 若清零在它前面，回响那一下就把本段的帧数抹成 0 —— 于是收尾时
        # 传给零帧保护的永远是 0，自动发送永远不触发（老病换个由头复发）。
        c.append((i_reset >= 0 and m_as.find("is_echo =") >= 0
                  and i_reset > m_as.find("is_echo ="),
                  "④ 帧计数清零也在判回声**之后**"
                  "（否则回响那一下把本段帧数抹成 0）"))
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

    # 反例 3：把补开麦挪到判回声**之前** —— 这就是 v1.0.13 的真 bug。
    # 补发成功后顺手记的 mic_reopen_at 会让**这一次（真按键）**自己落进
    # 回声窗口、当场被吞掉：热键一次不注入 → 输入法从头到尾没被叫起来
    # → 用户看到的「语音输入完全用不了」。
    anchor = "                now = time.time()\n                is_echo ="
    if anchor in src:
        bad3 = src.replace(
            anchor,
            "                if session.ensure_mic_open():\n"
            "                    mic_reopen_at = time.time()\n"
            + anchor, 1)
        n = sum(1 for g, _ in audit(bad3) if not g)
        if n:
            n_red += 1
            print(f"  ✅ 反例：把补开麦挪到判回声**之前**"
                  f"（＝ v1.0.13 真按键把自己判成回声、当场吞掉） → 报了 {n} 项红")
        else:
            print("  ❌ 反例：顺序颠倒了居然全绿 —— 这道闸拦不住它复发")
            ok = False
    else:
        print("  ❌ 反例 3 没构造出来（锚点没找到）")
        ok = False

    if n_red < 3:
        ok = False

    print()
    print("PASS" if ok else "FAIL —— 打 ❌ 的那几条会让「按一下、刚开口，"
                          "输入法就被程序自己关掉」复发")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
