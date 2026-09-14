"""
录制器测试 —— 把「keyboard 库报出来的事件」喂给真的 Recorder，验证它录出来的组合键。

为什么用合成事件，而不是真的用 SendInput 按一遍再读回来：
实测过，那条路测不出真问题。我们的注入走的是**扫描码**（这是让右 Alt、Win 这类
键真正生效的唯一办法），而 keyboard 库的键名表是按"虚拟键码 + 扫描码 + 扩展位"
四元组查表的 —— 扫描码注入过来的事件查不到表项，库会把 Win 报成 `reserved `。
于是"注入 → 读回"这个组合本身就会失真。
真实场景里用户是按物理键，Windows 给出的是正常的 VK，库能正确命名，
所以生产路径没问题，是**测试手段**不成立。

这里改成直接喂库对物理按键的报名字，把录制器的判断逻辑独立测透。
recorder 真正踩过的两个坑都在覆盖范围内：
  1. 库把右 Alt 报成 `right menu`（不是 `right alt`）→ 归一化漏了它，
     录制结果解析不出，SendInput 静默跳过，用户看到「自定义设了完全没用」。
  2. 只按修饰键的组合（微信默认的 Ctrl + Win）永远录不出来 ——
     原逻辑要等一个"非修饰键"才收尾，等不到就一直超时。

用法： python tools/test_recorder.py
输出： OK / FAIL
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

from _utf8 import setup as _setup_utf8  # 下面是中文输出，先钉住编码（CI 是 cp1252）

_setup_utf8()

_SANDBOX = tempfile.mkdtemp(prefix="rvb-rec-")
os.environ["APPDATA"] = _SANDBOX
os.environ["RVB_NO_AUTO_CONSOLE"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import keyboard as kb
except ImportError:
    print("SKIPPED（没装 keyboard 库）")
    raise SystemExit(0)

FAILS: list[str] = []


class FakeEvent:
    """只带 Recorder 真正读的两个属性。"""

    def __init__(self, event_type: str, name: str):
        self.event_type = event_type
        self.name = name


_inbox: "queue.Queue[FakeEvent]" = queue.Queue()


def _fake_read_event(suppress: bool = False) -> FakeEvent:
    """替代 keyboard.read_event：从测试脚本里取事件。

    取空时返回一个空名事件 —— Recorder 会 `continue` 掉它，
    顺便让它的 `while time.time() < self._until` 有机会检查超时/取消。
    """
    try:
        return _inbox.get(timeout=0.03)
    except queue.Empty:
        return FakeEvent("down", "")


kb.read_event = _fake_read_event          # 打补丁，必须在 Recorder 启动前

import console_server as cs  # noqa: E402


# ── 脚本：模仿用户按键时库报出的事件流 ─────────────────────────────────────────
def phys(combo: list[str], clean: list[str] | None = None) -> list[FakeEvent]:
    """按下 clean 里每个键，再逆序抬起。"""
    evs = [FakeEvent("down", n) for n in combo]
    evs += [FakeEvent("up", n) for n in reversed(clean if clean is not None else combo)]
    return evs


def feed(events: list[FakeEvent]) -> None:
    for e in events:
        _inbox.put(e)


def drive(events: list[FakeEvent], timeout: float = 4.0) -> dict:
    """启动一次真实录制 → 喂事件 → 取结果。"""
    while not _inbox.empty():                 # 清掉上一轮残留
        _inbox.get_nowait()
    cs.RECORDER.start()
    time.sleep(0.15)                          # 等录制线程起来
    feed(events)
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = cs.RECORDER.snapshot()
        if last.get("status") != "recording":
            return last
        time.sleep(0.05)
    return last


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
    return bool(cond)


def main() -> int:
    try:
        srv = cs.ensure_started(0)
    except OSError as e:
        print(f"SKIPPED（端口不可用：{e}）")
        return 0
    print(f"[recorder] 控制台端口 {srv.port}")

    try:
        cases = [
            # 右 Alt + 空格（豆包默认）：库对右 Alt 报的是 `right menu`
            ("右 Alt + 空格", phys(["right menu", "space"]), "ralt+space"),
            # AltGr 会同时报 `alt gr` 和 `right menu`，必须去重成一只键
            ("AltGr 双事件去重", phys(["alt gr", "right menu", "space"],
                                     ["right menu", "alt gr", "space"]), "ralt+space"),
            # 微信默认：纯修饰键组合，靠"松开修饰键"收尾。
            # 录到的是 lctrl/lwin（库给的名字，明确的左键），不是通用的 ctrl/win。
            ("Ctrl + Win（纯修饰键）", phys(["left ctrl", "left windows"]), "lctrl+lwin"),
            ("Ctrl + Win + Shift", phys(["left ctrl", "left windows", "left shift"]),
             "lctrl+lwin+lshift"),
            # 常规组合键：非修饰键按下即收尾
            ("Ctrl + Shift + M", phys(["left ctrl", "left shift", "m"]), "lctrl+lshift+m"),
            ("单键 F9", phys(["f9"]), "f9"),
            # 左边 Alt 不能被当成右 Alt（豆包只认右 Alt）
            ("左 Alt + 空格", phys(["left menu", "space"]), "lalt+space"),
            # 带空格的键名（库对 caps lock / page up 就是这种写法）
            ("Ctrl + Page Up", phys(["left ctrl", "page up"]), "lctrl+pageup"),
        ]

        for label, events, want in cases:
            r = drive(events)
            got = r.get("value") or ""
            check(r.get("status") == "ok", f"{label}：录制失败 {r}")
            check(got == want, f"{label}：录到 {got!r}，应为 {want!r}")

        # 录到的键必须立刻能被识别成对应的预设档 ——
        # 否则下拉框显示"自定义…"，用户以为白录了。
        from keys import combo_equivalent
        check(combo_equivalent(["lctrl", "lwin"], ["ctrl", "win"]),
              "lctrl+lwin 应等价于 ctrl+win")
        check(cs._current_preset(["lctrl", "lwin"]) == "ctrl+win",
              f"录到 lctrl+lwin 时应显示成 Ctrl + Win，实际 "
              f"{cs._current_preset(['lctrl', 'lwin'])}")
        check(cs._current_preset(["ralt", "space"]) == "ralt+space",
              "右Alt+空格 应匹配到预设档")
        check(cs._current_preset(["lalt", "space"]) == "custom",
              "左Alt+空格 不该被当成右Alt那一档（物理上是不同的键）")
        check(not combo_equivalent(["ralt", "space"], ["lalt", "space"]),
              "右 Alt 和左 Alt 不能算等价")

        # Esc 取消
        r = drive([FakeEvent("down", "esc")])
        check(r.get("status") == "cancel", f"按 Esc 应取消，实际 {r}")

        # 一个键都不按 → 超时（把录制上限临时调小，别让测试干等 10 秒）
        _old = cs.Recorder.MAX_SECONDS
        cs.Recorder.MAX_SECONDS = 0.6
        try:
            r = drive([], timeout=3.0)
        finally:
            cs.Recorder.MAX_SECONDS = _old
        check(r.get("status") == "timeout", f"无输入应超时，实际 {r}")
        check(bool(r.get("message")), "超时应该带一句人能看懂的提示")

        # 库里那些冷门键（解析不了）→ 必须报错，而不是把坏值写进配置
        cs.Recorder.MAX_SECONDS = 1.2
        try:
            r = drive([FakeEvent("down", "ime kana mode")], timeout=3.0)
        finally:
            cs.Recorder.MAX_SECONDS = _old
        check(r.get("status") == "error",
              f"解析不了的键应以 error 收场，实际 {r}")
        check("ime kana mode" in (r.get("message") or ""),
              f"错误信息里应指出是哪个键：{r.get('message')!r}")

    finally:
        try:
            srv.stop()
        finally:
            shutil.rmtree(_SANDBOX, ignore_errors=True)

    if FAILS:
        for f in FAILS:
            print(f"FAIL {f}")
        print(f"RECORDER FAILED（{len(FAILS)} 项）")
        return 1
    print("RECORDER OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
