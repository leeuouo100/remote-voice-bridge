"""
Shared bridge state — thread-safe handoff between the asyncio bridge
thread and the tray/UI thread.

The bridge runs in its own thread (asyncio loop); the tray icon and the
console window run in others. This module is the single source of truth
both sides read from, so no locks are held across UI calls.
"""

from __future__ import annotations
import threading
import time
from dataclasses import dataclass, asdict
from typing import Callable

_lock = threading.RLock()
_listeners: list[Callable[[], None]] = []


@dataclass
class BridgeState:
    connected: bool = False
    streaming: bool = False
    device:    str  = ""
    level:     int  = 0          # 0-100, audio level meter
    last_event: str = ""
    updated_at: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


_state = BridgeState()


def get() -> BridgeState:
    """Return a snapshot copy (safe to read from any thread)."""
    with _lock:
        return BridgeState(**asdict(_state))


def update(**kw) -> None:
    """Patch one or more fields and notify listeners."""
    with _lock:
        for k, v in kw.items():
            if hasattr(_state, k):
                setattr(_state, k, v)
        _state.updated_at = time.time()
    for fn in list(_listeners):
        try:
            fn()
        except Exception:
            pass


def reset() -> None:
    """Clear transient runtime state (on disconnect / shutdown)."""
    update(connected=False, streaming=False, level=0, device="")


def on_change(fn: Callable[[], None]) -> None:
    """Register a listener invoked (on a background thread) after updates."""
    _listeners.append(fn)
