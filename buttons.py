"""
Button mapper — resolves Chromecast Remote HID events to Windows actions.
"""

from __future__ import annotations
import logging
from typing import Callable, Optional

logger = logging.getLogger("rvb.btn")

from config import CHROMECAST_BUTTONS, DEFAULT_KEYMAP, Config
from keys import send_key, trigger_voice_hotkey, handle_mute_hold

# ── Resolution ────────────────────────────────────────────────────────────────
# (vk_name, is_hold_action)
VK_DEFAULTS = {
    "up":      ("up",      False),
    "down":    ("down",    False),
    "left":    ("left",    False),
    "right":   ("right",   False),
    "ok":      ("enter",   False),
    "back":    ("escape",  False),
    "home":    ("win+d",   False),
    "mute":    ("",        True),   # hold → backspace loop
    "vol_up":  ("volumeup",   False),
    "vol_down":("volumedown", False),
    "youtube": ("",          False),
    "netflix": ("",          False),
    "power":   ("",          False),
    "input":   ("",          False),
    "voice":   ("voice",     False),  # special
}


def resolve_button(
    button_id: str,
    event_type: str,          # "down" | "up"
    keymap: dict[str, str] | None = None,
    on_voice: Callable[[bool], None] | None = None,
) -> bool:
    """
    Handle a remote button event.
    Returns True if the event was handled (should suppress system default).
    """
    cfg  = keymap or Config.load().keymap
    mapped = cfg.get(button_id, "")

    # Voice button
    if button_id == "voice" or mapped == "voice":
        if on_voice:
            on_voice(event_type == "down")
        return True

    # Disabled
    if not mapped:
        return False

    # Combo key (e.g. "ctrl+win")
    if "+" in mapped:
        if event_type == "down":
            trigger_voice_hotkey(keys=mapped.split("+"))
        return True

    # Check if mapped key matches a VK default for this button
    default_vk, default_hold = VK_DEFAULTS.get(button_id, ("", False))

    # Mute hold
    if button_id == "mute" and default_hold:
        if event_type == "down":
            handle_mute_hold(True)
        else:
            handle_mute_hold(False)
        return True

    # Normal tap
    if event_type == "down":
        send_key(mapped)
    return True
