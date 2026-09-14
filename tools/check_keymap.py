"""
按键映射表自检 —— 确认下拉框里每一个目标动作都真的发得出去。

为什么需要：映射目标是 config.MAPPING_TARGETS 里的一串字符串
（"win+d"、"ctrl+shift+esc"…），最终由 keys._resolve_key() 翻译成扫描码/VK。
两者是分离的两张表，加目标时很容易只在 MAPPING_TARGETS 里写一行，
而 keys 那边根本不认识这个名字 —— 表现出来是"选项能选、按键没反应"，
而且不报错，非常难查。

同时校验 DEFAULT_KEYMAP 的键集合与 CHROMECAST_BUTTONS 完全一致，
避免新增遥控器按键后忘了给默认值。

还有一道**回归防线**：把 keyboard 库能报出的每一个键名都过一遍
normalize_combo_part → _resolve_key。踩过的坑是库把右 Alt 报成 `right menu`，
而归一化表里没这条 —— 录制出来的组合键解析不了，SendInput 静默跳过，
用户看到的现象是「自定义快捷键设了完全没用」，而且全程不报错。

用法： python tools/check_keymap.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _utf8 import setup as _setup_utf8  # noqa: E402  （下面是中文输出，先钉住编码）

_setup_utf8()

from config import (  # noqa: E402
    CHROMECAST_BUTTONS, DEFAULT_KEYMAP, MAPPING_TARGETS, VIRTUAL_TARGETS,
)
from keys import _resolve_key, combo_bad_parts, normalize_combo_part  # noqa: E402

# 虚拟目标（禁用 / 原样直通 / 语音键 / 按住说话）不是"要下发某个组合键"，
# 由程序内部逻辑接管，不参与键名解析校验。清单统一放在 config.VIRTUAL_TARGETS，
# 免得新增虚拟目标时这里漏改、自检误报。
_SPECIAL = set(VIRTUAL_TARGETS)

# 录制器里用户可能按到的、必须能解析的键名（keyboard 库的原始叫法）。
# 这张表照着库的键名表抄的，加键名时先在这里加一条。
_RECORDER_MUST_WORK = [
    "alt", "left menu", "right menu", "alt gr",      # Alt 的三种叫法
    "ctrl", "left ctrl", "right ctrl",
    "shift", "left shift", "right shift",
    "left windows", "right windows", "windows",
    "esc", "enter", "space", "spacebar", "tab", "backspace", "delete",
    "caps lock", "page up", "page down", "home", "end", "print screen",
    "up", "down", "left", "right",
    "volume up", "volume down", "volume mute",
    "play/pause media", "next track", "previous track",
    "a", "z", "0", "9", "f1", "f12",
]


def _keyboard_lib_names() -> list[str]:
    """从 keyboard 库源码里抓出它可能报出的所有键名。

    直接读源码而不是 import 它的内部表 —— 内部表名在各版本里改过，
    读文件反而更稳，也能在库没装上时干净地跳过。
    """
    try:
        import keyboard._winkeyboard as w
    except Exception:  # noqa: BLE001
        return []
    try:
        import re
        src = open(w.__file__, encoding="utf-8", errors="replace").read()
    except Exception:  # noqa: BLE001
        return []
    names = set(re.findall(r"\(\s*'([^']+)'\s*,\s*(?:True|False)\s*\)", src))
    names |= set(re.findall(r"\(\s*'([^']+)'\s*,\s*\)", src))
    return sorted(names)


def main() -> int:
    errors: list[str] = []

    # 1. 每个目标动作的每个组成部分都必须能被解析
    for value, desc in MAPPING_TARGETS.items():
        if value in _SPECIAL:
            continue
        for part in value.split("+"):
            part = part.strip().lower()
            if not part:
                errors.append(f"目标 {value!r}（{desc}）里有空的键名")
                continue
            if _resolve_key(part) is None:
                errors.append(f"目标 {value!r}（{desc}）里的键名 {part!r} 无法解析 —— "
                              f"选了这个目标不会有任何反应")

    # 2. 默认映射表必须覆盖所有遥控器按键
    missing = [b for b in CHROMECAST_BUTTONS if b not in DEFAULT_KEYMAP]
    extra = [b for b in DEFAULT_KEYMAP if b not in CHROMECAST_BUTTONS]
    if missing:
        errors.append(f"DEFAULT_KEYMAP 缺少按键：{missing}")
    if extra:
        errors.append(f"DEFAULT_KEYMAP 有多余按键（遥控器上没有）：{extra}")

    # 3. 默认值本身也要合法
    for btn, value in DEFAULT_KEYMAP.items():
        if value not in MAPPING_TARGETS:
            errors.append(f"按键 {btn} 的默认值 {value!r} 不在 MAPPING_TARGETS 里")

    # 4. 录制器：用户按得出来的键，归一化之后必须都能下发
    for raw in _RECORDER_MUST_WORK:
        norm = normalize_combo_part(raw)
        if _resolve_key(norm) is None:
            errors.append(f"录制键名 {raw!r} 归一化成 {norm!r} 后无法解析 —— "
                          f"用户录这个键会静默失效")

    # 5. 库里能报出的名字里，凡是"正常人会去录"的，都不该解析不了。
    #    这里只报告，不当作硬失败 —— 库里有 ime kana mode 这类冷门键，
    #    发不出去也无所谓，但列出来便于发现新的漏网之鱼。
    soft: list[str] = []
    for raw in _keyboard_lib_names():
        norm = normalize_combo_part(raw)
        if len(norm) == 1 or norm[:1] == "f" and norm[1:].isdigit():
            if _resolve_key(norm) is None:
                errors.append(f"库键名 {raw!r} 归一化成 {norm!r} 后无法解析")
            continue
        if _resolve_key(norm) is None and norm not in _SPECIAL:
            soft.append(f"{raw} → {norm}")

    if errors:
        for e in errors:
            print(f"FAIL {e}")
        return 1
    print(f"OK {len(MAPPING_TARGETS)} 个目标动作 / {len(DEFAULT_KEYMAP)} 个按键 / "
          f"{len(_RECORDER_MUST_WORK)} 个录制键名")
    if soft:
        print(f"[提示] {len(soft)} 个冷门键名未覆盖（不影响使用）："
              + "、".join(soft[:12]) + ("…" if len(soft) > 12 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
