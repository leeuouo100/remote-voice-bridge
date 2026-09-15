"""回归自检：遥控器的 HID 判定链。

## 为什么值得单独一关

2026-09-15 武哥报「遥控器上除了语音键，其他所有按键都没有作用」。
查到底发现一条**以前没人写下来的硬事实**：

    遥控器（VID 0x18D1 / PID 0x9450）只暴露 5 个 HID 集合，其中两个是
    **厂商自定义用法页**（0xFF01 / 0xFF80，各 21 字节输入报告），
    Windows 对它们**不做任何处理**。

这条事实直接决定"该修什么"：按键若发在厂商页，改映射表**永远没用**，
只能自己去读那一路。所以代码里那几个判定（厂商页下界、集合角色表、
报告里的判读分支）是**结论的载体**，改错一个，报告就会指向错误的修法，
用户会照着错的方向白折腾一轮。

本关就守这几条：静态查判定表还在，动态造假数据跑两条判读分支，
再用**反例**证明"厂商页"这个判定真的在起作用（而不是碰巧印出来的字）。
"""

from __future__ import annotations

import io
import os
import re
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _utf8 import setup as _setup_utf8  # noqa: E402

_setup_utf8()

import hidinfo      # noqa: E402
import hidwatch     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ── 假数据：遥控器真机抓下来的那份（5 个集合，两个厂商页）───────────────
def _fake_snap(vendor_handled: bool = False) -> dict:
    return {
        "cols": [
            {"hwid": "…_Dev_VID&0218d1_PID&9450_REV&011b_f196a263671c&Col01",
             "instance": "a&1&0&0000", "col": "COL01", "hogp": True,
             "service": "kbdhid", "desc": "HID Keyboard Device", "config_flags": 0,
             "guid": "{4d36e96b-…}", "bt_addr": "F1:96:A2:63:67:1C",
             "is_keyboard": True, "is_mouse": False, "enabled": True},
            {"hwid": "…_Dev_VID&0218d1_PID&9450_REV&011b_f196a263671c&Col04",
             "instance": "a&1&0&0003", "col": "COL04", "hogp": True,
             "service": "", "desc": "HID-compliant vendor-defined device",
             "config_flags": 0, "guid": "{745a17a0-…}",
             "bt_addr": "F1:96:A2:63:67:1C",
             "is_keyboard": False, "is_mouse": False, "enabled": True},
        ],
        "live": [
            {"path": r"\\?\hid#…col01", "vid": 0x18D1, "pid": 0x9450,
             "usage_page": 0x01, "usage": 0x06, "in_len": 9, "is_google": True},
            {"path": r"\\?\hid#…col04", "vid": 0x18D1, "pid": 0x9450,
             "usage_page": 0xFF01, "usage": 0x01, "in_len": 21, "is_google": True},
            {"path": r"\\?\hid#…col05", "vid": 0x18D1, "pid": 0x9450,
             "usage_page": 0xFF80, "usage": 0x00, "in_len": 21, "is_google": True},
        ],
        "ble": [{"kind": "低功耗 BLE", "key": "…", "friendly": "Chromecast Remote",
                 "desc": "", "enabled": True}],
    }


def _fake_watch(kb_n: int, vendor_n: int) -> hidwatch.ReportWatcher:
    import time
    now = time.time()
    w = hidwatch.ReportWatcher()
    w.key_events = [(now, "keyboard", "enter", 0x1C)] * kb_n
    c = hidwatch.HidCollection(r"\\?\hid#…col04", 0x18D1, 0x9450, 0xFF01, 0x01, 21)
    c.opened = True
    c.reports = [(now, b"\x01\x00\x00" + b"\x00" * 18)] * vendor_n
    w.collections = [c]
    return w


def main() -> int:
    checks: list[tuple[bool, str]] = []

    def A(ok: bool, msg: str) -> None:
        """收一条断言。用函数而不是 `checks.append` ——
        后者只收一个参数，写成 `A(条件, "说明")` 会直接 TypeError。"""
        checks.append((bool(ok), msg))

    # ── 静态：判定表 ────────────────────────────────────────────────────
    A(hidinfo._VENDOR_PAGE_MIN == 0xFF00,
      "厂商自定义页下界 = 0xFF00（USB HID 规范）")

    role_vendor = hidinfo.collection_role(0xFF01, 0x01)
    A(role_vendor[1] is False, "0xFF01/0x01 判定为 Windows 不处理")
    A("不做任何事" in role_vendor[2] or "不做任何" in role_vendor[2],
      "0xFF01 的说明写明 Windows 不做任何事")

    role_kbd = hidinfo.collection_role(0x01, 0x06)
    A(role_kbd[1] is True, "0x01/0x06（键盘）判定为 Windows 会处理")

    role_cons = hidinfo.collection_role(0x0C, 0x01)
    A(role_cons[1] is True, "0x0C/0x01（消费类控制）判定为 Windows 会处理")

    src_hid = _read("hidinfo.py")
    A("00001812" in src_hid, "按 HOGP 服务 UUID 识别蓝牙低功耗集合")
    A("BTHLE" in src_hid and "BTHENUM" in src_hid, "读蓝牙设备清单（BTHLE + BTHENUM）")
    A("0x000B0193" in src_hid, "取报告描述符的 IOCTL 码是 0x000B0193（实测值）")

    # 设备路径必须与 cbSize 偏移无关 —— 这是踩过的真坑：
    # 64 位该写 8、32 位该写 6，写错 API 不报错，只给一条**残缺**路径，
    # 后续 CreateFileW 直接 ERROR_INVALID_NAME(123)，现象是"什么都读不到"。
    # 用真实的 UTF-16 缓冲区、故意把路径放在偏移 6，看能不能取出来。
    import ctypes
    path = "\\\\?\\hid#vid_18d1&pid_9450&col04#a&1&0&0003"
    body = b"\x00" * 6 + path.encode("utf-16-le") + b"\x00\x00"
    body += b"\x00" * (128 - len(body))
    _buf = ctypes.create_string_buffer(body, 128)
    A(hidinfo._extract_path(_buf) == path,
      "设备路径从任意偏移都能取出（不依赖 cbSize，绕开 32/64 位差异）")

    src_main = _read("main.py")
    A("无名" in src_main, "main.py 已补上「键名为空」的日志盲区")

    src_diag = _read(os.path.join("tools", "diag_remote.py"))
    A("import hidinfo" in src_diag and "import hidwatch" in src_diag,
      "诊断工具已接入 HID 探测与集合监听")
    A('"--hid"' in src_diag, "诊断工具支持 --hid（不用按键、不用退程序）")

    # ── 动态：报告生成 ──────────────────────────────────────────────────
    text = hidinfo.format_report(_fake_snap(), indent="")
    A("结论 0" in text, "报告含【结论 0】硬件身份")
    A("厂商自定义" in text, "报告点名厂商自定义集合")
    A("输入报告 21 字节" in text, "报告写出厂商集合的输入报告长度")
    A("HID Keyboard Device" in text, "报告写出键盘集合的绑定情况")

    w_kbd = _fake_watch(kb_n=2, vendor_n=0)
    t_kbd = "\n".join(w_kbd.summary_lines())
    A("键盘事件收到了" in t_kbd, "有键盘事件 → 判读指向程序层")

    w_ven = _fake_watch(kb_n=0, vendor_n=1)
    t_ven = "\n".join(w_ven.summary_lines())
    A("只出现在厂商自定义页" in t_ven,
      "只有厂商页有报告 → 判读指向「要自己解报告」")

    # err=5 对键盘/鼠标集合是预期结果，报告不能吓人
    c = hidwatch.HidCollection(r"\\?\hid#…col01", 0x18D1, 0x9450, 0x01, 0x06, 9)
    c.open_error = "打开失败 err=5"
    A("系统独占" in c.status, "键盘集合打不开(err=5)被解释为系统独占、属正常")
    c2 = hidwatch.HidCollection(r"\\?\hid#…col04", 0x18D1, 0x9450, 0xFF01, 0x01, 21)
    c2.open_error = "打开失败 err=5"
    A("系统独占" not in c2.status, "厂商集合打不开不当成正常（要暴露出来）")

    # ── 反例：厂商判定必须是"起作用"的，不能是碰巧印出来的字 ──
    #
    # ⚠ 标记串必须**各自唯一**。一开始这里用的是"厂商自定义"，
    #   结果两条反例都"失败"了 —— 因为结论 0 里另有一句与判定无关的
    #   硬编码说明也含这四个字，拿掉判定它照样在，反例就测不出东西。
    #   现在各自钉死：
    #     · "另注意"          ← 只由 _VENDOR_PAGE_MIN 决定（哪些集合算厂商页）
    #     · "不会产生键盘/鼠标/媒体事件" ← 只由 _COLLECTION_ROLES 决定（角色的说明）
    VENDOR_MARK = "另注意"
    ROLE_MARK = "不会产生键盘/鼠标/媒体事件"

    org = hidinfo._VENDOR_PAGE_MIN
    try:
        hidinfo._VENDOR_PAGE_MIN = 0x10000
        t2 = hidinfo.format_report(_fake_snap(), indent="")
        A(VENDOR_MARK not in t2,
          "反例：抬走「厂商页」下界后，厂商页警告随之消失（判定真的在起作用）")
    finally:
        hidinfo._VENDOR_PAGE_MIN = org
    A(VENDOR_MARK in hidinfo.format_report(_fake_snap(), indent=""),
      "恢复下界后厂商页警告重新出现")

    # 反例二：把**所有**集合的角色都说成"Windows 会处理"，那条说明必须消失。
    # ⚠ 这里必须换掉整个 collection_role，不能只改 _COLLECTION_ROLES 里的一条：
    #   遥控器有**两个**厂商页（0xFF01 和 0xFF80），后者不在表里、走的是
    #   "下界判断"那条兜底分支，只改一条的话它照样会把同样的说明印出来 ——
    #   反例就永远"失败"（我第一版就是这么写错的）。
    org_fn = hidinfo.collection_role
    try:
        hidinfo.collection_role = lambda p, u: (  # type: ignore[assignment]
            "键盘", True, "（被改坏的判定）")
        t3 = hidinfo.format_report(_fake_snap(), indent="")
        A(ROLE_MARK not in t3,
          "反例：角色全改成「会处理」后，那条说明消失（角色判定真的在起作用）")
    finally:
        hidinfo.collection_role = org_fn          # type: ignore[assignment]
    A(ROLE_MARK in hidinfo.format_report(_fake_snap(), indent=""),
      "恢复角色表后说明重新出现")

    # ── 输出 ────────────────────────────────────────────────────────────
    bad = [msg for ok, msg in checks if not ok]
    for ok, msg in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {msg}")
    if bad:
        print(f"HIDINFO CHECK FAILED（{len(bad)} 项）")
        return 1
    print("HIDINFO CHECK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
