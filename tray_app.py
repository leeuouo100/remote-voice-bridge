"""
remote-voice-bridge — 托盘入口

· 后台线程跑 BLE/ATVV 桥（main.run_bridge），断线自动重连
· 系统托盘图标反映连接 / 推流状态
· 右键菜单 → 控制台：开本地 Web 控制台（对标 vRemoter 的界面）

为什么控制台不是 tkinter
------------------------
要做到 vRemoter 那种精致度（暗色卡片、三路电平表、遥控器示意图、细排版），
tkinter 的控件体系表达不出来 —— 硬做只能得到"一堆灰按钮"。
现在控制台是本地起的 Web UI（见 console_server.py + ui/），
只用标准库 http.server，不新增依赖，视觉上也不再受限。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
import winreg

import pystray
from PIL import Image, ImageDraw

from config import APP_VERSION, CONFIG_DIR, INPUT_METHODS, Config
import state

APP_NAME  = "Remote Voice Bridge"
LOG_FILE  = CONFIG_DIR / "bridge.log"
RUN_KEY   = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "RemoteVoiceBridge"
REPO_URL  = "https://github.com/leeuouo100/remote-voice-bridge"

_stop = threading.Event()
_icon: "pystray.Icon | None" = None


# ── DPI ───────────────────────────────────────────────────────────────────────
def _enable_dpi_awareness() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ── 图标 ──────────────────────────────────────────────────────────────────────
def _icon_image(size: int = 64, connected: bool = False, streaming: bool = False):
    """手绘麦克风图标。未连接=灰，已连接=暖橙，推流中=橙红。"""
    if streaming:
        body = (232, 96, 48, 255)
    elif connected:
        body = (238, 158, 58, 255)
    else:
        body = (150, 150, 150, 255)

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    w = h = size
    cx = w * 0.5
    lw = max(2, int(w * 0.075))

    head_w   = w * 0.24
    head_top = h * 0.15
    head_bot = h * 0.55
    d.rounded_rectangle(
        [cx - head_w / 2, head_top, cx + head_w / 2, head_bot],
        radius=head_w / 2, fill=body,
    )
    d.arc([cx - w * 0.27, head_bot - h * 0.12, cx + w * 0.27, h * 0.80],
          start=0, end=180, fill=body, width=lw)
    d.line([cx, h * 0.80, cx, h * 0.90], fill=body, width=lw)
    d.line([cx - w * 0.15, h * 0.90, cx + w * 0.15, h * 0.90], fill=body, width=lw)
    return img


# ── 开机启动 ──────────────────────────────────────────────────────────────────
def _launch_cmd() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def _is_autostart() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return bool(v)
    except OSError:
        return False


def _set_autostart(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ,
                              subprocess.list2cmdline(_launch_cmd()))
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except OSError:
                pass


# ── 动作 ──────────────────────────────────────────────────────────────────────
def _status_text(item=None) -> str:
    """菜单标题。pystray 会把 MenuItem 自身作为首个参数传给 callable text。"""
    s = state.get()
    dev = s.device or "遥控器"
    if s.streaming:
        return f"● 语音中 · {dev}"
    if s.connected:
        return f"● 已连接 · {dev}"
    return "○ 未连接（按遥控器任意键唤醒）"


def _make_im_action(key: str):
    def _act(icon, item):
        cfg = Config.load()
        cfg.input_method = key
        cfg.voice_hotkey = []
        cfg.save()
        icon.update_menu()
    return _act


def _toggle_autostart(icon, item):
    _set_autostart(not _is_autostart())
    icon.update_menu()


def _request_reconnect(icon=None, item=None):
    """让桥线程断开重连，而不是再起一个进程。

    早前的「重新连接」是 Popen 一个新进程再退出自己 —— 两个实例会同时抢同一个
    BLE 连接和同一个托盘图标，谁赢不确定，表现为"点了重连就时好时坏"。
    """
    try:
        import main
        main.request_reconnect()
    except Exception:
        pass
    if icon:
        icon.update_menu()


def _open_console(icon=None, item=None):
    try:
        import console_server
        url = console_server.open_console()
        print(f"[console] {url}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[console] 启动失败：{e}", flush=True)


def _open_log(icon=None, item=None):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        LOG_FILE.write_text("", encoding="utf-8")
    os.startfile(str(LOG_FILE))            # noqa: S606


def _open_config_dir(icon=None, item=None):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.startfile(str(CONFIG_DIR))          # noqa: S606


def _quit(icon, item):
    _stop.set()
    state.reset()
    try:
        import console_server
        console_server.ensure_started().stop()
    except Exception:
        pass
    try:
        icon.stop()
    except Exception:
        pass


# ── 菜单 ──────────────────────────────────────────────────────────────────────
def build_menu(icon) -> pystray.Menu:
    im_items = [
        pystray.MenuItem(
            meta["desc"],
            _make_im_action(key),
            checked=lambda item, k=key: Config.load().input_method == k,
            radio=True,
        )
        for key, meta in INPUT_METHODS.items()
    ]

    return pystray.Menu(
        pystray.MenuItem(_status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("控制台", _open_console, default=True),
        pystray.MenuItem("输入法 / 语音触发键", pystray.Menu(*im_items)),
        pystray.MenuItem("重新连接", _request_reconnect),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机启动", _toggle_autostart,
                         checked=lambda item: _is_autostart()),
        pystray.MenuItem("打开配置文件夹", _open_config_dir),
        pystray.MenuItem("打开日志", _open_log),
        pystray.MenuItem("项目主页", lambda i, it: webbrowser.open(REPO_URL)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _quit),
    )


# ── 桥线程 ────────────────────────────────────────────────────────────────────
def _bridge_worker():
    import asyncio
    from main import run_bridge

    while not _stop.is_set():
        try:
            asyncio.run(run_bridge())
        except Exception as e:  # noqa: BLE001
            print(f"[bridge] {e}", flush=True)
        if _stop.is_set():
            break
        time.sleep(3)


# ── 入口 ──────────────────────────────────────────────────────────────────────
def main():
    global _icon
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _enable_dpi_awareness()

    threading.Thread(target=_bridge_worker, daemon=True).start()

    _icon = pystray.Icon(APP_NAME, _icon_image(),
                         f"{APP_NAME} v{APP_VERSION}（启动中…）")
    _icon.menu = build_menu(_icon)

    def _refresh(icon=None):
        s = state.get()
        _icon.icon = _icon_image(connected=s.connected, streaming=s.streaming)
        _icon.title = f"{APP_NAME} — {_status_text()}"
        try:
            _icon.update_menu()
        except Exception:
            pass
        if not _stop.is_set():
            threading.Timer(0.8, _refresh).start()

    threading.Timer(0.8, _refresh).start()

    # 首次启动自动弹一次控制台：装完就能看见界面，
    # 不用去翻右键菜单 —— 第一次用的人根本不知道托盘里有什么。
    if os.environ.get("RVB_NO_AUTO_CONSOLE") != "1":
        threading.Timer(2.0, _open_console).start()

    _icon.run()


if __name__ == "__main__":
    main()
