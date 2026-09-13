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
    """Send a single key (tap by default)."""
    if not ensure_ip():
        try:
            import keyboard
            keyboard.press(name)
            if hold:
                time.sleep(duration)
            else:
                time.sleep(duration)
                keyboard.release(name)
        except Exception as e:
            logger.error(f"keyboard.send failed: {e}")
        return
    try:
        if hold:
            ip.press_key(name)
        else:
            ip.press_key(name)
            time.sleep(duration)
            ip.release_key(name)
    except Exception as e:
        logger.error(f"Interception send_key failed: {e}")


def send_combo(keys: list[str], duration: float = 0.05) -> None:
    """Press multiple keys together (tap), e.g. ['alt', 'shift', 'm']."""
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
# 为什么默认是「右 Alt」：
#   微信输入法（Windows 2.1.0+）的语音唤起键是长按**右 Alt**，全局生效；
#   豆包输入法提供「右 Alt / 右 Alt+空格 / 左 Ctrl+Win」三种，默认也是右 Alt。
#   别跟「微信 PC 客户端」搞混：那是按住 Ctrl+Win，但只在微信自己的窗口里有用，
#   在豆包、记事本、浏览器里按了没有任何反应。
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
_EXTENDED_VKS = {0x5B, 0x5C, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}

# 左右分体的修饰键 → (扫描码, 是否扩展键)
#
# 为什么这几个键不能走虚拟键（VK）：微信输入法和豆包输入法的语音唤起键都是
# **右 Alt** —— 注意是右侧那个 Alt，和左 Alt 不是同一个键。若只下发通用
# VK_MENU(0x12)，输入法会当成左 Alt 而直接忽略，表现就是「按了没反应」。
# 走 KEYEVENTF_SCANCODE 下发真实硬件的扫描码，系统会像处理物理按键一样
# 自行推导出 VK_RMENU，与真人按下去的事件完全一致，不用去猜输入法认哪个 VK。
_SCANCODE_MAP: dict[str, tuple[int, bool]] = {
    "lalt":   (0x38, False),
    "ralt":   (0x38, True),    # AltGr = 右侧 Alt ← 微信输入法 / 豆包输入法都用它
    "altgr":  (0x38, True),
    "lctrl":  (0x1D, False),
    "rctrl":  (0x1D, True),
    "lshift": (0x2A, False),
    "rshift": (0x36, False),
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
}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _VK_MAP[_c] = ord(_c.upper())
for _d in "0123456789":
    _VK_MAP[_d] = ord(_d)
for _i in range(1, 25):
    _VK_MAP[f"f{_i}"] = 0x70 + (_i - 1) if _i <= 12 else 0x87 + (_i - 13)


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

_held_keys: list[str] = []


def _resolve_key(name: str) -> tuple[int, int, int] | None:
    """键名 → (wVk, wScan, 基础 flags)。不认识的键返回 None。"""
    n = str(name).strip().lower()
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
    return True


def hotkey_down(keys: list[str]) -> None:
    """按下并保持一组键（不自动释放）。"""
    global _held_keys
    if _user32 is None:
        logger.warning("SendInput unavailable — press-and-hold not supported")
        return
    if _held_keys:                     # 上一轮没释放干净 → 先松开，防止残留
        hotkey_up()
    done: list[str] = []
    for k in keys:                     # 依次按下（修饰键在前）
        if _send_key(str(k), up=False):
            done.append(str(k))
            time.sleep(0.015)
    if not done:
        return
    _held_keys = done
    logger.info(f"🎤 voice hotkey DOWN (hold): {'+'.join(done)}")


def hotkey_up() -> None:
    """释放上一次 hotkey_down 按住的键。"""
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
    global _mute_held
    while _mute_held:
        if not ensure_ip():
            break
        try:
            ip.send_key_down(0x0E)   # BACKSPACE
            time.sleep(0.02)
            ip.send_key_up(0x0E)
        except Exception:
            pass
        time.sleep(0.05)


def handle_mute_hold(down: bool) -> None:
    global _mute_held, _mute_thread
    if down and not _mute_held:
        _mute_held = True
        _mute_thread = threading.Thread(target=_mute_loop, daemon=True)
        _mute_thread.start()
    elif not down and _mute_held:
        _mute_held = False
        _mute_thread = None
