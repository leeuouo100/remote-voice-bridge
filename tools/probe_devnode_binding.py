"""遥控器 devnode 的驱动绑定状态（只读注册表，不用按键）。

要回答的只有一句：**HOGP 那一侧的驱动到底绑上了没有？**

诊断报告里 5 路 HID 集合里有 3 路 `Service=(未绑定)` ——
正常应该绑 `input.inf`（厂商页）/ `hidserv.inf`（消费类）。
未绑定 = devnode 建出来了但驱动没装完 → 子节点没 Start →
传输层未必会为这些报告 ID 发 GATT 订阅 → 报告永远不来。
这是"打得开、读得到、却 0 条报告"最可能的解释。

用法： python tools/probe_devnode_binding.py
"""
import os
import sys
import winreg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _utf8 import setup as _setup_utf8  # noqa: E402
_setup_utf8()

ENUM = r"SYSTEM\CurrentControlSet\Enum"
TARGETS = [
    ("HID", r"SYSTEM\CurrentControlSet\Enum\HID"),
    ("BTHLE", r"SYSTEM\CurrentControlSet\Enum\BTHLE"),
    ("BTHLEDevice", r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice"),
]


def read_vals(key_path):
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
            i = 0
            while True:
                try:
                    n, v, _t = winreg.EnumValue(k, i)
                except OSError:
                    break
                out[n] = v
                i += 1
    except OSError:
        pass
    return out


def subkeys(key_path):
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
            i = 0
            while True:
                try:
                    out.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except OSError:
        pass
    return out


def main() -> int:
    for label, root in TARGETS:
        print("=" * 76)
        print(f" {label}")
        print("=" * 76)
        for k in subkeys(root):
            low = k.lower()
            if "18d1" in low or "9450" in low or "f196a263671c" in low:
                print(f"\n▸ {k}")
                vals = read_vals(root + "\\" + k)
                for f in ("Service", "ConfigFlags", "ClassGUID", "ContainerID",
                          "FriendlyName", "DeviceDesc", "Driver"):
                    if f in vals:
                        print(f"    {f:<14}= {vals[f]}")
                for inst in subkeys(root + "\\" + k):
                    iv = read_vals(root + "\\" + k + "\\" + inst)
                    flags = iv.get("ConfigFlags")
                    prob = iv.get("Problem", iv.get("ProblemStatus"))
                    print(f"    · 实例 {inst}")
                    print(f"        Service={iv.get('Service', '(未绑定)')!r}"
                          f"  ConfigFlags={flags}"
                          f"  Problem={prob}"
                          f"  Driver={iv.get('Driver', '-')!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
