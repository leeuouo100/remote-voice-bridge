"""
Keyboard bridge — synthetic key injection for Windows.
Uses Interception driver when available; falls back to keyboard library.
"""

from __future__ import annotations
import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("rvb.keys")

try:
    import interception_wrapper as ip
    _INTERCEPTION_OK = True
except ImportError:
    _INTERCEPTION_OK = False
    logger.warning("interception_wrapper not found — falling back to keyboard.send")


def ensure_ip() -> bool:
    if not _INTERCEPTION_OK:
        return False
    if not getattr(ip, "_context", None):
        try:
            ip.init()
        except Exception:
            return False
    return True


# ── Key senders ───────────────────────────────────────────────────────────────
def send_key(name: str, hold: bool = False, duration: float = 0.02) -> None:
    """点按一个键（或 "ctrl+shift+s" 形式的组合键）。

    优先走 SendInput：它直接下发 VK/扫描码，语义确定，
    而且是我们唯一的"自己发的键"登记点（回声防护靠它，见 was_self_injected）。
    只有键名 SendInput 认不出来时才回退到 interception / keyboard 通道。
    """
    if not hold and tap_key(name):
        return
    mark_injected([p for p in str(name).split("+") if p.strip()])
    if not ensure_ip():
        try:
            import keyboard
            keyboard.press(name)
            time.sleep(duration)
            if not hold:
                keyboard.release(name)
        except Exception as e:
            logger.error(f"keyboard.send failed: {e}")
        return
    try:
        ip.press_key(name)
        if not hold:
            time.sleep(duration)
            ip.release_key(name)
    except Exception as e:
        logger.error(f"Interception send_key failed: {e}")


def send_combo(keys: list[str], duration: float = 0.05) -> None:
    """组合键点按，例如 ['ctrl', 'shift', 's']。同样优先 SendInput。"""
    if keys and tap_key("+".join(keys)):
        return
    mark_injected(keys)
    if not ensure_ip():
        try:
            import keyboard
            keyboard.press(keys[0])
            for k in keys[1:]:
                keyboard.press(k)
            time.sleep(duration)
            for k in reversed(keys):
                keyboard.release(k)
        except Exception as e:
            logger.error(f"combo fallback failed: {e}")
        return
    try:
        for k in keys:
            ip.press_key(k)
        time.sleep(duration)
        for k in reversed(keys):
            ip.release_key(k)
    except Exception as e:
        logger.error(f"Interception combo failed: {e}")


def trigger_voice_hotkey(keys: list[str] | None = None) -> None:
    """Tap the voice-trigger hotkey (点按一次)。"""
    from config import Config
    cfg = Config.load()
    if keys is None:
        keys = cfg.trigger_keys_windows()
    if not keys:
        return
    logger.info(f"🎤 Trigger voice hotkey (tap): {'+'.join(keys)}")
    send_combo(keys)


# ── Press-and-hold hotkey (Windows SendInput) ────────────────────────────────
# 为什么必须"按住"而不是"点一下"：
#   微信输入法 / 豆包输入法的语音输入都是「长按说话、松开结束识别」。
#   点按一次只会录到几十毫秒的空气，识别结果必然是空的。
#   遥控器的语音键本身就是 PTT —— 按下=开麦，松开=停麦，
#   与"按住快捷键"天然一一对应，所以用 key-down / key-up 而不是 tap。
#
# 默认是哪一组键：
#   微信输入法「设置 → 语音输入」里的**「按住说话」= Ctrl + Win**
#   （同一页那条「启动语音输入」= 左Win+左Ctrl+左Shift，是切换模式，本程序不用）。
#   豆包输入法的默认才是**右 Alt**（它给 右Alt / 右Alt+空格 / 左Ctrl+Win 三选一）。
#   ⚠ 早前这里写的是"微信输入法默认长按右 Alt"，那是错的。
#   另：微信 PC 客户端的 Ctrl+Win 是系统级可用，不是只在微信窗口里。
#
#   ⚠ 而 Ctrl+Win 恰好是本文件里最需要小心的一组键 —— 见 order_press 的说明。
#
# 不用 keyboard 库做这件事：它的键名是字符串（"win" 未必能解析），
# 且 press/release 需要自己配对；SendInput 直接下发 VK/扫描码，语义确定。
import ctypes
from ctypes import wintypes

_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP       = 0x0002
_KEYEVENTF_SCANCODE    = 0x0008
_INPUT_KEYBOARD        = 1

# 扩展键（必须带 EXTENDEDKEY 标志，否则左/右 Win、方向键会被系统认成小键盘）
_EXTENDED_VKS = {0x5B, 0x5C, 0x5D, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
                 0x2D, 0x2E, 0x2C,
                 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}

# 左右分体的修饰键 → (扫描码, 是否扩展键)
#
# 为什么这几个键不能走虚拟键（VK）：**豆包输入法**的语音唤起键是
# **右 Alt**（微信输入法用的是 Ctrl+Win，不涉及这条）—— 注意是右侧那个 Alt，
# 和左 Alt 不是同一个键。若只下发通用 VK_MENU(0x12)，输入法会当成左 Alt
# 而直接忽略，表现就是「按了没反应」。走 KEYEVENTF_SCANCODE 下发真实硬件的
# 扫描码，系统会像处理物理按键一样自行推导出 VK_RMENU，与真人按下去的
# 事件完全一致，不用去猜输入法认哪个 VK。
_SCANCODE_MAP: dict[str, tuple[int, bool]] = {
    "lalt":   (0x38, False),
    "ralt":   (0x38, True),    # AltGr = 右侧 Alt ← 豆包输入法用它
    "altgr":  (0x38, True),
    "lctrl":  (0x1D, False),
    "rctrl":  (0x1D, True),
    "lshift": (0x2A, False),
    "rshift": (0x36, False),
    # Win 键必须走扫描码，不能走 VK。踩过的坑：用 wVk=0x5B + KEYEVENTF_EXTENDEDKEY
    # 下发，钩子能看到事件、GetAsyncKeyState 却查不到按键 —— Win 根本没按住，
    # 于是微信的 Ctrl+Win 语音键完全唤不起来（只有 Ctrl 生效）。
    # 走扫描码让系统自己推导 VK，和真人按键完全一致。
    "win":    (0x5B, True),
    "lwin":   (0x5B, True),
    "rwin":   (0x5C, True),
}

_VK_MAP: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11,
    "shift": 0x10,
    "alt": 0x12, "option": 0x12,
    "win": 0x5B, "windows": 0x5B, "lwin": 0x5B, "cmd": 0x5B, "command": 0x5B,
    "rwin": 0x5C,
    "space": 0x20, "enter": 0x0D, "return": 0x0D,
    "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "capslock": 0x14, "backspace": 0x08,
    # 方向键 —— 映射界面里要能选到。（遥控器原生方向键走 HID，所以方向键的
    # 默认映射是「原样直通」；但用户完全可能把某个键改成方向键用。）
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    # 编辑 / 导航 —— 按键映射界面的目标动作要用（对标 vRemoter 的 deleteBackward、
    # pageUp/pageDown、home/end 等）。不补齐的话这些目标只能靠 interception 或
    # keyboard 库兜底，SendInput 直通路径会静默跳过。
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "home": 0x24, "end": 0x23, "printscreen": 0x2C,
    "applications": 0x5D, "menu": 0x5D,
    "numlock": 0x90, "scrolllock": 0x91, "pause": 0x13,
    # 标点：录制器把它们换成词（见 _COMBO_ALIAS），这里给上对应的 VK，
    # 否则用户录一个 Ctrl+/ 之类的组合会提示"发不出去"。
    "plus": 0xBB, "equal": 0xBB, "minus": 0xBD,
    "comma": 0xBC, "period": 0xBE, "slash": 0xBF, "backslash": 0xDC,
    "semicolon": 0xBA, "quote": 0xDE, "grave": 0xC0,
    "bracketleft": 0xDB, "bracketright": 0xDD, "asterisk": 0x6A,
    # 系统媒体键（遥控器的音量/静音键原生就是这些，映射界面里可以改指向）
    "volumeup": 0xAF, "vol_up": 0xAF, "volume_up": 0xAF,
    "volumedown": 0xAE, "vol_down": 0xAE, "volume_down": 0xAE,
    "mute": 0xAD, "volumemute": 0xAD, "volume_mute": 0xAD,
    "playpause": 0xB3, "play_pause": 0xB3, "mediaplaypause": 0xB3,
    "nexttrack": 0xB0, "prevtrack": 0xB1, "stopmedia": 0xB2,
    # 浏览器 / 系统功能键 —— 键盘多媒体区上的那些
    "browserback": 0xA6, "browserforward": 0xA7, "browserrefresh": 0xA8,
    "browserstop": 0xA9, "browsersearch": 0xAA, "browserfavorites": 0xAB,
    "browserhome": 0xAC,
    "selectmedia": 0xB5, "startmail": 0xB4,
    "launchapp1": 0xB6, "launchapp2": 0xB7,
    "select": 0x29, "print": 0x2A, "execute": 0x2B, "help": 0x2F,
    "sleep": 0x5F, "clear": 0x0C, "break": 0x03,
}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _VK_MAP[_c] = ord(_c.upper())
for _d in "0123456789":
    _VK_MAP[_d] = ord(_d)
for _i in range(1, 25):
    # VK_F1=0x70 … VK_F12=0x7B，紧跟着 VK_F13=0x7C … VK_F24=0x87。
    # 早前这里写成 `0x87 + (i-13)`（等于把 F13 当成 0x87），整个 F13–F24
    # 都偏了一截 —— 映射界面上选了 F13 以上，实际发出去的是别的键。
    _VK_MAP[f"f{_i}"] = 0x70 + (_i - 1) if _i <= 12 else 0x7C + (_i - 13)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32 = ctypes.WinDLL("user32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None

if _user32 is not None:
    _GetAsyncKeyState = _user32.GetAsyncKeyState
    _GetAsyncKeyState.restype = ctypes.c_short
    _GetAsyncKeyState.argtypes = [ctypes.c_int]
else:
    _GetAsyncKeyState = None

_held_keys: list[str] = []


# ── "系统到底认没认这个键" ────────────────────────────────────────────────────
# GetAsyncKeyState 返回值的最高位 = 此刻是否按下。SendInput 无声失败时，
# 钩子可能看得到事件、这一位却是 0 —— 输入法读的正是这一位。
_POLL_VKS: dict[str, tuple[int, ...]] = {
    # 修饰键要同时查"通用码"和"左右分体码"：
    #   ctrl 走的是通用 VK_CONTROL(0x11)，lctrl 走扫描码（系统推导出 0xA2）。
    #   只查一边会把"其实按住了"误判成失败，于是白白重发（还会打出假警告）。
    #   右分体的键只查它自己那个码，避免左侧按着时被误认成右侧。
    "ctrl":  (0x11, 0xA2), "lctrl": (0xA2, 0x11), "rctrl": (0xA3,),
    "shift": (0x10, 0xA0), "lshift": (0xA0, 0x10), "rshift": (0xA1,),
    "alt":   (0x12, 0xA4), "lalt": (0xA4, 0x12), "ralt": (0xA5,), "altgr": (0xA5,),
    "win":   (0x5B,), "lwin": (0x5B,), "rwin": (0x5C,),
    "cmd":   (0x5B,), "command": (0x5B,), "windows": (0x5B,),
}

_VERIFY_RETRY = 3          # 修饰键最多下发几次
_VERIFY_SETTLE = 0.03      # 每次下发后等多久再查状态
_WARN_COOLDOWN = 60.0      # 同一个键的"被吞"警告最多每分钟一条
_warned_at: dict[str, float] = {}


def _async_down(name: str) -> bool:
    """这个键此刻在系统看来是否处于按下状态。"""
    if _GetAsyncKeyState is None:
        return True                        # 查不了就不拦，交给调用方继续
    n = normalize_combo_part(name)
    vks = _POLL_VKS.get(n)
    if vks is None:
        vk = _VK_MAP.get(n)
        vks = (vk,) if vk else ()
    for vk in vks:
        if _GetAsyncKeyState(vk) & 0x8000:
            return True
    return False


def _verify_pressed(name: str) -> bool:
    """确认修饰键真的按住了；没按住就补发。返回最终是否生效。

    为什么要补发：实测「补发一次」确实能把被系统吞掉的按下救回来
    （单次下发 0/6，补发之后 6/6）。所以这不是"发出去就算完成"的事。
    总共最多下发 _VERIFY_RETRY 次，最后一次仍查不到就记一条警告（带冷却，
    免得用户按一次遥控器就刷一串同样的日志）。
    """
    for attempt in range(_VERIFY_RETRY):
        if _async_down(name):
            return True
        if attempt + 1 < _VERIFY_RETRY:
            time.sleep(_VERIFY_SETTLE)
            _send_key(name, up=False)      # 补发（同一个键重复按下是无害的）
    now = time.time()
    if now - _warned_at.get(name, 0) > _WARN_COOLDOWN:
        _warned_at[name] = now
        logger.warning(
            f"⚠ '{name}' 注入后系统没有记录按下状态 —— 被系统吞了。"
            f"键盘钩子仍能看到事件，但依赖 GetAsyncKeyState 判定的程序会认不到"
        )
    return False


# ── 自注入回声防护 ────────────────────────────────────────────────────────────
# 为什么必须要有：遥控器走的是 HID 键盘通道，Windows 上它和物理键盘**无法区分**，
# 所以 main.py 的 `keyboard` 钩子既能看见用户的物理按键，也能看见**我们自己注入的键**。
# 一旦映射目标又落回同一个键名（最典型：确认键 → Enter，而遥控器原生发的也是 Enter），
# 钩子就会把自注入的 Enter 再喂回 resolve_button → 再注入一次 → 无限自激。
#
# 做法：注入时把键名登记一个短暂的过期时间，钩子在处理事件前先问一句
# "这是我刚自己发的吗"，是就直接丢掉。
_ECHO_TTL = 0.30
_injected_at: dict[str, float] = {}


def _mark_injected(name: str) -> None:
    """登记"这个键是我发的"。

    同时登记归一化后的名字：我们发出去用的是本模块的键名（`win`、`ralt`），
    而键盘钩子回调里拿到的是 keyboard 库的叫法（`left windows`、`right menu`）——
    两边字符串对不上，只登记原文的话回声防护对这些键等于没生效。
    """
    raw = str(name).strip().lower()
    if not raw:
        return
    exp = time.time() + _ECHO_TTL
    _injected_at[raw] = exp
    norm = normalize_combo_part(raw)
    if norm and norm != raw:
        _injected_at[norm] = exp


def mark_injected(names) -> None:
    """批量登记（供 tap_key / send_combo 等使用）。"""
    for n in names or ():
        _mark_injected(n)


def was_self_injected(name: str) -> bool:
    """该键事件是不是我们自己刚注入的？顺手清掉过期项。

    归一化后再比对，理由同上：钩子报的是库的名字，我们登记的是自己的名字。
    """
    now = time.time()
    for k in [k for k, exp in _injected_at.items() if exp < now]:
        _injected_at.pop(k, None)
    raw = str(name or "").strip().lower()
    if not raw:
        return False
    return raw in _injected_at or normalize_combo_part(raw) in _injected_at


# keyboard 库报出来的键名 → 本模块认识的键名。
#
# 两套命名必须对齐，否则「录制」出来的组合键会解析不出来，SendInput 静默跳过 ——
# 用户看到的现象就是「自定义快捷键设了完全没用」。
#
# ⚠ 这张表是照着 keyboard 库的键名表逐条对齐的，不是凭印象写的。
#   踩过的坑：库把**右侧 Alt** 报成 `right menu`（VK 0xA5），不是 `right alt`。
#   之前只处理了 `alt gr`，于是录制右 Alt 得到 `right menu` → 解析不出 → 静默失效。
#   同样漏掉的还有 `caps lock` / `page up` / `print screen` / `volume up` 这些带空格的。
_COMBO_ALIAS = {
    # 修饰键
    "control": "ctrl",
    "windows": "win", "left windows": "lwin", "right windows": "rwin",
    "left ctrl": "lctrl", "right ctrl": "rctrl",
    "left shift": "lshift", "right shift": "rshift",
    "left menu": "lalt", "right menu": "ralt",   # menu = Alt（Windows 的叫法）
    "alt gr": "ralt", "altgr": "ralt",
    "option": "alt", "command": "win", "cmd": "win", "meta": "win",
    # 带空格的标准名
    "caps lock": "capslock", "capital": "capslock",
    "num lock": "numlock", "scroll lock": "scrolllock",
    "page up": "pageup", "page down": "pagedown",
    "print screen": "printscreen",
    "spacebar": "space", "space bar": "space",
    "escape": "esc", "return": "enter", "back": "backspace",
    "arrow up": "up", "arrow down": "down",
    "arrow left": "left", "arrow right": "right",
    "arrowup": "up", "arrowdown": "down",
    "arrowleft": "left", "arrowright": "right",
    # 媒体键
    "volume up": "volumeup", "volume down": "volumedown", "volume mute": "mute",
    "next track": "nexttrack", "previous track": "prevtrack",
    "play/pause media": "playpause", "stop media": "stopmedia",
    "select media": "selectmedia", "start mail": "startmail",
    # 浏览器 / 系统功能键（键盘上的多媒体区）
    "browser back": "browserback", "browser forward": "browserforward",
    "browser refresh": "browserrefresh", "browser stop": "browserstop",
    "browser search key": "browsersearch", "browser favorites": "browserfavorites",
    "browser start and home": "browserhome",
    "start application 1": "launchapp1", "start application 2": "launchapp2",
    "select": "select", "print": "print", "execute": "execute",
    "help": "help", "sleep": "sleep", "clear": "clear",
    "control-break processing": "break",
    # 标点：组合键是靠 "+" 拼的，必须先把这些字符换成词，否则 "ctrl++" 会被
    # 当成三段切开。换成词以后 send_combo 的 split("+") 才安全。
    "+": "plus", "-": "minus", "=": "equal",
    ",": "comma", ".": "period", "/": "slash", "\\": "backslash",
    ";": "semicolon", "'": "quote", "`": "grave",
    "[": "bracketleft", "]": "bracketright", "*": "asterisk",
}


# 修饰键家族：同一个家族里的不同名字指向**同一只物理键的不同叫法**。
# 组合键里不允许同族重复，否则会下发多余的按下事件（例如 `alt+ralt+space`）。
_MOD_FAMILY = {
    "ctrl": "ctrl", "lctrl": "ctrl", "rctrl": "ctrl",
    "alt": "alt", "lalt": "alt", "ralt": "alt",
    "shift": "shift", "lshift": "shift", "rshift": "shift",
    "win": "win", "lwin": "win", "rwin": "win",
}


def normalize_combo_part(name: str) -> str:
    """把键盘库的键名归一化成 config/MAPPING_TARGETS 用的键名。"""
    n = str(name or "").strip().lower()
    return _COMBO_ALIAS.get(n, n)


def is_modifier(name: str) -> bool:
    """归一化之后判断是不是修饰键。

    录制器必须先归一化再判断 —— 直接拿库的原始名去比 `mods` 集合，会漏掉
    `right menu`（右 Alt）这类名字，于是「按住右 Alt」被当成"主键已按"，
    组合键在 Alt 落下的瞬间就结束了，根本录不到后面的键。
    """
    return normalize_combo_part(name) in _MOD_FAMILY


def modifier_family(name: str) -> str:
    """修饰键所属家族；不是修饰键返回空串。"""
    return _MOD_FAMILY.get(normalize_combo_part(name), "")


# 修饰键的**下发顺序**（小的先按）。这不是审美问题，是硬性要求：
# Win 必须排在 Ctrl 前面，否则 Ctrl 先落下时 Win 的按下事件会被系统吞掉。
# 详见 hotkey_down 的注释和 tools/check_injection.py 里的用例。
_MOD_PRIORITY = {
    "win": 0, "lwin": 0, "rwin": 0, "cmd": 0, "command": 0, "windows": 0,
    "ctrl": 1, "lctrl": 1, "rctrl": 1, "control": 1,
    "shift": 2, "lshift": 2, "rshift": 2,
    "alt": 3, "lalt": 3, "ralt": 3, "altgr": 3, "menu": 3, "option": 3,
}


def order_press(keys: list[str]) -> list[str]:
    """把一组键排成"该按下去的顺序"：修饰键按 Win→Ctrl→Shift→Alt，
    主键保持调用方给的先后、统一放最后。

    松开时用 reversed(按下顺序) 即可，与真实键盘的习惯一致。
    """
    mods, mains = [], []
    for i, k in enumerate(keys):
        n = normalize_combo_part(k)
        (mods if n in _MOD_PRIORITY else mains).append((i, str(k), n))
    mods.sort(key=lambda it: (_MOD_PRIORITY[it[2]], it[0]))
    return [it[1] for it in mods] + [it[1] for it in mains]


def combo_bad_parts(combo: str) -> list[str]:
    """组合键里解析不出来的键名（空列表 = 全部可用）。

    用途：录制完 / 保存配置前做一次校验。与其写进配置再静默失效，
    不如当场告诉用户「这个键我发不出去」。
    """
    bad: list[str] = []
    for part in str(combo or "").split("+"):
        part = part.strip()
        if part and _resolve_key(part) is None:
            bad.append(part)
    return bad


# 「通用名」和「明确的左键名」指的是同一只键，配对时按等价处理。
#
# 为什么需要：录制器读的是 keyboard 库给的名字，库里左 Ctrl 叫 `left ctrl`
# → 归一化成 `lctrl`；而预设表里写的是通用的 `ctrl`。不做等价判断的话，
# 用户明明录的就是 Ctrl+Win，下拉框却显示成「自定义…」，看着像没设上。
#
# ⚠ 右 Alt / 右 Ctrl / 右 Shift 刻意**不参与**这种配对 ——
#   它们和左边那个是物理上不同的键，豆包的语音键就只认右 Alt。
_GENERIC_LEFT_EQUIV = {
    "ctrl": "lctrl", "shift": "lshift", "alt": "lalt", "win": "lwin",
}


def combo_key_eq(a: str, b: str) -> bool:
    """两个键名是不是同一只键（含"通用名 ↔ 左键名"的等价）。"""
    x, y = normalize_combo_part(a), normalize_combo_part(b)
    if x == y:
        return True
    return (_GENERIC_LEFT_EQUIV.get(x) == y) or (_GENERIC_LEFT_EQUIV.get(y) == x)


def combo_equivalent(a, b) -> bool:
    """两组键是不是同一组（忽略顺序）。"""
    xs = [normalize_combo_part(k) for k in (a or []) if str(k).strip()]
    ys = [normalize_combo_part(k) for k in (b or []) if str(k).strip()]
    if len(xs) != len(ys):
        return False
    rest = list(ys)
    for k in xs:
        for i, other in enumerate(rest):
            if combo_key_eq(k, other):
                rest.pop(i)
                break
        else:
            return False
    return True



def _resolve_key(name: str) -> tuple[int, int, int] | None:
    """键名 → (wVk, wScan, 基础 flags)。不认识的键返回 None。

    入口先过一遍 `normalize_combo_part`：本函数是所有下发路径的**唯一收口**，
    在这里归一化，等于把「keyboard 库的叫法」和「本模块的键名」两套命名的对齐
    收敛到一处。否则 `right menu`（右 Alt 在库里的名字）这类值只要漏进配置，
    就会在 SendInput 那里静默跳过 —— 用户完全看不出哪一步错了。
    """
    n = normalize_combo_part(name)
    if n in _SCANCODE_MAP:
        scan, ext = _SCANCODE_MAP[n]
        return 0, scan, _KEYEVENTF_SCANCODE | (_KEYEVENTF_EXTENDEDKEY if ext else 0)
    vk = _VK_MAP.get(n)
    if vk is None:
        return None
    return vk, 0, (_KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0)


def _send_key(name: str, up: bool) -> bool:
    """下发一次按下/抬起。键名不认识则返回 False。"""
    if _user32 is None:
        return False
    spec = _resolve_key(name)
    if spec is None:
        logger.warning(f"unknown key name '{name}' — skipped")
        return False
    vk, scan, flags = spec
    if up:
        flags |= _KEYEVENTF_KEYUP
    inp = _INPUT(type=_INPUT_KEYBOARD)
    inp.u.ki.wVk         = vk
    inp.u.ki.wScan       = scan
    inp.u.ki.dwFlags     = flags
    inp.u.ki.time        = 0
    inp.u.ki.dwExtraInfo = None
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    # 登记"这个键是我发的"，供 main.py 的键盘钩子识别回声（见 was_self_injected）
    _mark_injected(name)
    return True


def tap_key(name: str) -> bool:
    """用 SendInput 点按一个键或组合键（"ctrl+shift+s"）。

    返回 True 表示已由本函数处理；False 表示键名不认识，调用方应回退到
    keyboard / interception 通道。组合键按 修饰键→主键 顺序按下、逆序抬起。
    """
    if _user32 is None:
        return False
    parts = [p.strip().lower() for p in str(name).split("+") if p.strip()]
    if not parts:
        return False
    for p in parts:
        if _resolve_key(p) is None:
            return False                    # 有认不出的键 → 整体交给兜底通道
    if len(parts) > 4:
        return False                        # 明显不是组合键（可能是误配）→ 兜底
    # 顺序同样要过 order_press：Ctrl+Win 这种组合点按也会踩"Win 被吞"的坑
    parts = order_press(parts)
    for p in parts:
        _send_key(p, up=False)
        time.sleep(0.008)
    time.sleep(0.02)
    for p in reversed(parts):
        _send_key(p, up=True)
        time.sleep(0.008)
    return True


def hotkey_down(keys: list[str]) -> None:
    """按下并保持一组键（不自动释放）。

    Win 是这批键里最麻烦的一个，下面两点都是实测（tools/check_injection.py --raw）
    逼出来的，不是凭经验拍的：

    ① **含 Win 的组合必须 Win 最先落下，没有例外。**
       实测：Ctrl 先按下之后，再注入 Win，键盘钩子收到的 vkCode 是 **0xFC**，
       而不是 0x5B —— 也就是 Windows 把"Ctrl 已按下时的 Win"从钩子里藏掉了
       （输入法认的就是钩子事件，认不到 0x5B 就等于这个键没按）。
       把 Win 排到最前面，钩子才能收到一个货真价实的 `vk=0x5B/scan=0x5B`。
       间隔从 0 拉到 80ms 都救不回来，所以这不是"慢一点就行"，只能换顺序。

    ② **含 Win 的组合不要拿 GetAsyncKeyState 当判据。**
       实测：Ctrl 和 Win 同时按住时，系统只会让它俩里的一个出现在
       GetAsyncKeyState 里（谁先按谁留下；Ctrl+Shift+Win 时连 Shift 也会消失）。
       这是 Windows 把 Win+Ctrl 当系统和弦处理的结果，不是我们发坏了 ——
       拿它当判据只会得到永远红着的假警报，还白等几十毫秒、刷一屏日志。
       不含 Win 的组合（最常用的「右 Alt」）照旧核对，那一套是准的，
       也是唯一能拦住"SendInput 静默失效"的手段。
    """
    global _held_keys
    if _user32 is None:
        logger.warning("SendInput unavailable — press-and-hold not supported")
        return
    if _held_keys:                     # 上一轮没释放干净 → 先松开，防止残留
        hotkey_up()

    ordered = order_press([str(x) for x in keys])
    has_win = any(_MOD_PRIORITY.get(normalize_combo_part(k)) == 0 for k in ordered)

    done: list[str] = []
    for k in ordered:
        if not _send_key(k, up=False):
            continue                       # 键名不认识，_send_key 已经记过日志
        done.append(k)
        if not has_win and is_modifier(k):
            _verify_pressed(k)             # ②
        time.sleep(0.015)
    if not done:
        return
    _held_keys = done
    logger.info(f"🎤 voice hotkey DOWN (hold): {'+'.join(done)}")


def hotkey_up() -> None:
    """释放上一次 hotkey_down 按住的键（按下的逆序松开，和真人习惯一致）。

    逆序松开还有个副作用是好的：Win 最后才抬起，这时别的修饰键已经松了，
    但前面确实有别的键按下过，系统不会把它当成"单按 Win"去弹开始菜单。
    """
    global _held_keys
    if not _held_keys:
        return
    for k in reversed(_held_keys):
        _send_key(k, up=True)
        time.sleep(0.015)
    _held_keys = []
    logger.info("🎙️ voice hotkey UP")


def voice_hotkey_down(keys: list[str] | None = None) -> None:
    """语音开始。hold 模式=按住；tap 模式=点一下（端点式输入法）。"""
    from config import Config
    cfg = Config.load()
    if keys is None:
        keys = cfg.trigger_keys_windows()
    if not keys:
        logger.warning("voice hotkey 为空 — 请在 config.json 里配置")
        return
    if getattr(cfg, "hotkey_mode", "hold") == "tap":
        logger.info(f"🎤 voice hotkey TAP (toggle 模式): {'+'.join(keys)}")
        send_combo(keys)
    else:
        hotkey_down(keys)


def voice_hotkey_up() -> None:
    """语音结束。hold 模式=松开；tap 模式=再点一下结束并上屏。"""
    from config import Config
    cfg = Config.load()
    if getattr(cfg, "hotkey_mode", "hold") == "tap":
        keys = cfg.trigger_keys_windows()
        if keys:
            logger.info(f"🎤 voice hotkey TAP (toggle 模式): {'+'.join(keys)}")
            send_combo(keys)
    else:
        hotkey_up()


# ── Mute-hold backspace loop ─────────────────────────────────────────────────
_mute_held = False
_mute_thread: Optional[threading.Thread] = None


def _mute_loop():
    """按住静音键 = 连续退格。

    ⚠ 退出时必须保证 BACKSPACE 处于"已释放"状态。
    原来的写法把整个 down/up 一起包在 try 里、异常直接 pass：
    一旦 `send_key_up` 抛错（Interception 驱动被拔、权限被收回等），
    异常被吞掉、循环继续；下一轮又 send_key_down —— 若此时用户正好松手，
    循环在"已按下、未释放"的状态下退出，系统层面 BACKSPACE 就一直按着，
    之后用户每碰一下键盘都在连续删除。属于必须堵掉的脏状态。
    """
    global _mute_held
    try:
        while _mute_held:
            if not ensure_ip():
                break
            ip.send_key_down(0x0E)   # BACKSPACE
            time.sleep(0.02)
            try:
                ip.send_key_up(0x0E)
            except Exception as e:  # noqa: BLE001
                # 释放失败 → 立刻停手，绝不能带着"按下未释放"继续 down 下一轮
                logger.error(f"BACKSPACE 释放失败，已停止连按以免键状态卡住：{e}")
                break
            time.sleep(0.05)
    finally:
        # 无论怎么退出，都把键补一次释放（幂等，重复 release 无害）
        try:
            ip.send_key_up(0x0E)
        except Exception:
            pass


def handle_mute_hold(down: bool) -> None:
    global _mute_held, _mute_thread
    if down and not _mute_held:
        _mute_held = True
        _mute_thread = threading.Thread(target=_mute_loop, daemon=True)
        _mute_thread.start()
    elif not down and _mute_held:
        _mute_held = False
        _mute_thread = None
