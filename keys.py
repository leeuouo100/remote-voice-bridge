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
    """Tap the voice-trigger hotkey for the configured input method."""
    from config import Config
    cfg = Config.load()
    if keys is None:
        keys = cfg.trigger_keys_windows()
    if not keys:
        return
    logger.info(f"🎤 Trigger voice hotkey: {'+'.join(keys)}")
    send_combo(keys)


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
