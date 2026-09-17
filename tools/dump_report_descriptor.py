"""把遥控器各 HID 集合的**原始报告描述符** dump 出来并解析。

这是"按键到底长什么样"的唯一权威来源 —— 描述符是设备自己报的，
不经过 Windows 的翻译，也不受"报告有没有流"影响。

用法： python tools/dump_report_descriptor.py
产出： %APPDATA%\\remote-voice-bridge\\hid-descriptor.txt
"""
import os
import sys
import ctypes
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _utf8 import setup as _setup_utf8  # noqa: E402
_setup_utf8()

import hidinfo                                     # noqa: E402
from config import CONFIG_DIR                      # noqa: E402

_IOCTL = hidinfo._IOCTL_HID_GET_REPORT_DESCRIPTOR
OUT = CONFIG_DIR / "hid-descriptor.txt"


def dump(path: str, n: int) -> bytes | None:
    k32, h = hidinfo._open_hid(path)
    if h is None:
        return None
    try:
        k32.DeviceIoControl.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p]
        buf = ctypes.create_string_buffer(8192)
        ret = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(ctypes.c_void_p(h), _IOCTL, None, 0,
                                 buf, 8192, ctypes.byref(ret), None)
        if not ok:
            print(f"   ❌ IOCTL 失败 err={ctypes.get_last_error()}")
            return None
        return buf.raw[:ret.value]
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


SIZE_OF = {0: 0, 1: 1, 2: 2, 3: 4}


def parse(raw: bytes) -> list[str]:
    out, i = [], 0
    page = 0
    while i < len(raw):
        b = raw[i]
        size = SIZE_OF[b & 3]
        typ = (b >> 2) & 3
        tag = (b >> 4) & 0xF
        data = raw[i + 1:i + 1 + size]
        val = int.from_bytes(data, "little") if data else 0
        sval = int.from_bytes(data, "little", signed=True) if data else 0
        d = ""
        if typ == 1 and tag == 0x0:
            page = val
            d = f"UsagePage  0x{val:04X}"
        elif typ == 1 and tag == 0x1:
            d = f"LogicalMin {sval}"
        elif typ == 1 and tag == 0x2:
            d = f"LogicalMax {sval}"
        elif typ == 1 and tag == 0x3:
            d = f"PhysicalMin {sval}"
        elif typ == 1 and tag == 0x4:
            d = f"PhysicalMax {sval}"
        elif typ == 1 and tag == 0x7:
            d = f"ReportSize  {val} bit"
        elif typ == 1 and tag == 0x8:
            d = f"***** ReportID = {val} *****"
        elif typ == 1 and tag == 0x9:
            d = f"ReportCount {val}"
        elif typ == 1 and tag == 0xA:
            d = "Push"
        elif typ == 1 and tag == 0xB:
            d = "Pop"
        elif typ == 2 and tag == 0x0:
            d = f"Usage      0x{page:04X}/0x{val:04X}"
        elif typ == 2 and tag == 0x1:
            d = f"UsageMin   0x{page:04X}/0x{val:04X}"
        elif typ == 2 and tag == 0x2:
            d = f"UsageMax   0x{page:04X}/0x{val:04X}"
        elif typ == 0 and tag == 0x8:
            d = f"◆◆ INPUT    flags=0x{val:02X}"
        elif typ == 0 and tag == 0x9:
            d = f"◆◆ OUTPUT   flags=0x{val:02X}"
        elif typ == 0 and tag == 0xB:
            d = f"◆◆ FEATURE  flags=0x{val:02X}"
        elif typ == 1 and tag == 0xC:
            d = "EndCollection"
        elif typ == 1 and tag == 0xA and size == 0:
            d = "Push"
        else:
            d = f"(typ={typ} tag={tag:#x} val=0x{val:X})"
        out.append(f"{i:04X}: {b:02X} {data.hex(' '):<9} {d}")
        i += 1 + size
    return out


def main() -> int:
    L: list[str] = []

    def say(s=""):
        print(s)
        L.append(s)

    say("=" * 74)
    say(" 遥控器 HID 报告描述符（设备自报，权威）")
    say("=" * 74)
    for d in hidinfo.live_hid_collections():
        if not d.get("is_google"):
            continue
        say()
        say("─" * 74)
        say(f" 集合 0x{d['usage_page']:04X}/0x{d['usage']:04X}  "
            f"in_len={d['in_len']}  {hidinfo.collection_role(d['usage_page'], d['usage'])[0]}")
        say(f" path={d['path']}")
        say("─" * 74)
        raw = dump(d["path"], d["in_len"])
        if raw is None:
            say("   （打不开，拿不到描述符 —— 键盘/鼠标被系统独占，属正常）")
            continue
        say(f" 描述符 {len(raw)} 字节：")
        for ln in parse(raw):
            say("   " + ln)

    text = "\n".join(L) + "\n"
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text, encoding="utf-8")
        print(f"\n已写出：{OUT}")
    except Exception as e:                          # noqa: BLE001
        print(f"\n⚠ 写文件失败：{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
