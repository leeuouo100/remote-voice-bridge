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


def _library_reported_names() -> set:
    """keyboard 库**可能报出**的键名（归一化之后）—— 权威清单。

    取 `_winkeyboard.to_name` 里每条的 `names[0]`：那正是钩子回调
    `e.name` 的来源（`process_key` 里 `name = names[0]`），
    再过一遍 `normalize_name`，与 `KeyboardEvent.__init__` 完全一致。

    拿不到库时返回**空集合**，调用方据此跳过这项检查 ——
    "拿不到清单"不等于"键名有问题"，不许把没测成说成测出来是坏的。
    """
    try:
        import keyboard._winkeyboard as w
        from keyboard._canonical_names import normalize_name as nn
        w._setup_name_tables()
        out = set()
        for names in w.to_name.values():
            if names:
                out.add(nn(names[0]))
        return out
    except Exception:  # noqa: BLE001
        return set()


def _main_key_map_names() -> list[str]:
    """从 main.py 源码里取出 KEY_MAP 的键名。

    用 ast 解析源码而不是 import main —— import 会启动整个程序。
    """
    import ast
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "main.py")
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
        tree = ast.parse(src)
    except Exception:  # noqa: BLE001
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "KEY_MAP":
                    return [k.value for k in node.value.keys
                            if isinstance(k, ast.Constant)
                            and isinstance(k.value, str)]
    return []


def audit_key_map(names, known) -> list[str]:
    """KEY_MAP 的键名体检。抽成纯函数是为了能给反例（见 main 里第 6 步）。

    为什么非要有这一项：键名写错的失效是**绝对静默**的 ——
    那个按键永远匹配不上，而日志一个字都不说。遥控器按键本来就不来，
    两件事叠在一起就再也查不出来了 —— `escape`（库只报 `esc`）
    就是这么躺了整整一版。
    """
    out: list[str] = []
    for n in names:
        if len(n) == 1:
            # 库用 MapVirtualKeyW(vk, VK_TO_CHAR) 兜底给多媒体键起了字母名：
            # mute→'d'、vol_up→'b'、play/pause media→'g'…
            # 映射它们等于劫持物理键盘的字母键（打字变调音量）。
            out.append(f"KEY_MAP 里有单字母键名 {n!r} —— 会劫持物理键盘，必须删掉")
            continue
        if known and n not in known:
            out.append(f"KEY_MAP 里的 {n!r} 是 keyboard 库**永远不会报出**的名字"
                       f"（写错就是静默失效：按了没反应、日志也不留痕）")
    return out


# 每个遥控器按键都必须有一条能命中的映射（否则那个键永远没动作）。
# 这里写的是"库真正会报的名字"，不是想当然的写法。
_KEY_MAP_MUST_HAVE = ("home", "enter", "esc", "browser back",
                      "up", "down", "left", "right")


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

    # 6. main.KEY_MAP 的键名必须都是 keyboard 库真会报出来的写法。
    #    这一项守的是 `escape` vs `esc` 那类**静默失效**：
    #    映射表里写着一个永远匹配不上的名字，按键就永远没动作，且不留痕。
    km = _main_key_map_names()
    known = _library_reported_names()
    if not km:
        errors.append("没能从 main.py 里解析出 KEY_MAP（源码结构变了？）")
    else:
        errors.extend(audit_key_map(km, known))
        for need in _KEY_MAP_MUST_HAVE:
            if need not in km:
                errors.append(f"KEY_MAP 少了 {need!r} —— 遥控器对应的按键将永远没动作")

        # ── 反例自证：把检查本身也测一遍 ──
        # 不测的话，哪天 `_library_reported_names()` 悄悄返回空集合，
        # 上面的断言会**全都不报**，这道闸就变成了摆设（本项目反复踩的坑）。
        bad = audit_key_map(["escape"], known)
        if known and not any("escape" in b for b in bad):
            errors.append("反例失败：把死键名 'escape' 放进去居然没被拦下")
        bad2 = audit_key_map(["b"], known)
        if not any("单字母" in b for b in bad2):
            errors.append("反例失败：单字母键名居然没被拦下")
        if known and audit_key_map(["esc", "home", "enter"], known):
            errors.append("反例失败：合法键名被误报")

    if errors:
        for e in errors:
            print(f"FAIL {e}")
        return 1
    print(f"OK {len(MAPPING_TARGETS)} 个目标动作 / {len(DEFAULT_KEYMAP)} 个按键 / "
          f"{len(_RECORDER_MUST_WORK)} 个录制键名 / "
          f"KEY_MAP {len(km)} 个键名（含 3 条反例自证）")
    if soft:
        print(f"[提示] {len(soft)} 个冷门键名未覆盖（不影响使用）："
              + "、".join(soft[:12]) + ("…" if len(soft) > 12 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
