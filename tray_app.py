"""
remote-voice-bridge — Windows 托盘 GUI 入口（对标 vRemoter 的菜单栏应用）

启动后：
  · 后台线程跑 BLE/ATVV 桥（main.run_bridge），断线自动重连
  · 系统托盘图标反映连接 / 推流状态
  · 右键菜单：状态 / 输入法 / 设备 / 开机启动 / 控制台 / 日志 / 退出
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
import winreg
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from config import CONFIG_DIR, INPUT_METHODS, Config
import state

APP_NAME  = "Remote Voice Bridge"
LOG_FILE  = CONFIG_DIR / "bridge.log"
RUN_KEY   = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "RemoteVoiceBridge"

_stop = threading.Event()
_icon: "pystray.Icon | None" = None


# ── 图标 ──────────────────────────────────────────────────────────────────────
def _icon_image(size: int = 64, connected: bool = False, streaming: bool = False):
    """手绘麦克风图标。未连接=灰，已连接=暖橙，推流中=暖橙红。"""
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
    lw = max(2, int(w * 0.075))          # 线宽

    head_w   = w * 0.24
    head_top = h * 0.15
    head_bot = h * 0.55
    # 话筒头（胶囊）
    d.rounded_rectangle(
        [cx - head_w / 2, head_top, cx + head_w / 2, head_bot],
        radius=head_w / 2, fill=body,
    )
    # U 形支架
    d.arc([cx - w * 0.27, head_bot - h * 0.12, cx + w * 0.27, h * 0.80],
          start=0, end=180, fill=body, width=lw)
    # 立柱 + 底座
    d.line([cx, h * 0.80, cx, h * 0.90], fill=body, width=lw)
    d.line([cx - w * 0.15, h * 0.90, cx + w * 0.15, h * 0.90], fill=body, width=lw)
    return img


# ── 开机启动（注册表 Run 键，无需管理员权限）────────────────────────────────
def _launch_cmd() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def _launch_cmd_str() -> str:
    parts = _launch_cmd()
    return subprocess.list2cmdline(parts)


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
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, _launch_cmd_str())
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except OSError:
                pass


# ── 菜单动作 ──────────────────────────────────────────────────────────────────
def _status_text(item=None) -> str:
    """菜单标题。pystray 会把 MenuItem 自身作为首个参数传给 callable text，
    因此必须接受一个可选参数。"""
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
        cfg.save()
        icon.update_menu()
    return _act


def _toggle_autostart(icon, item):
    _set_autostart(not _is_autostart())
    icon.update_menu()


def _relaunch(icon, item):
    subprocess.Popen(_launch_cmd(), close_fds=True)
    _quit(icon, item)


def _open_log(icon, item):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        LOG_FILE.write_text("", encoding="utf-8")
    os.startfile(str(LOG_FILE))


_console_lock = threading.Lock()
_console_open = False


def _open_console(icon=None, item=None):
    """控制台窗口在独立线程里建自己的 Tk root。"""
    global _console_open
    with _console_lock:
        if _console_open:
            return
        _console_open = True
    threading.Thread(target=_console_main, daemon=True).start()


def _quit(icon, item):
    _stop.set()
    state.reset()
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
        pystray.MenuItem("输入法", pystray.Menu(*im_items)),
        pystray.MenuItem("设备", pystray.Menu(
            pystray.MenuItem("重新连接", _relaunch),
        )),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机启动", _toggle_autostart,
                         checked=lambda item: _is_autostart()),
        pystray.MenuItem("打开控制台", _open_console),
        pystray.MenuItem("打开日志", _open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _quit),
    )


# ── 控制台窗口 ────────────────────────────────────────────────────────────────
def _console_main():
    import tkinter as tk
    from tkinter import scrolledtext

    root = tk.Tk()
    root.title(f"{APP_NAME} · 控制台")
    root.geometry("720x460")
    root.minsize(560, 360)

    # 状态区
    top = tk.Frame(root)
    top.pack(fill="x", padx=12, pady=(12, 6))

    lbl_status = tk.Label(top, text="—", font=("Microsoft YaHei UI", 11, "bold"))
    lbl_status.pack(side="left")

    lbl_im = tk.Label(top, text="", font=("Microsoft YaHei UI", 9), fg="#666")
    lbl_im.pack(side="right")

    # 电平条
    lvl_frame = tk.Frame(root)
    lvl_frame.pack(fill="x", padx=12, pady=(0, 8))
    tk.Label(lvl_frame, text="电平", font=("Microsoft YaHei UI", 9),
             fg="#666").pack(side="left")
    canvas = tk.Canvas(lvl_frame, height=10, bg="#eeeeee", highlightthickness=0)
    canvas.pack(side="left", fill="x", expand=True, padx=(8, 0))

    # 日志区
    txt = scrolledtext.ScrolledText(root, font=("Consolas", 9), wrap="none")
    txt.pack(fill="both", expand=True, padx=12, pady=(0, 12))
    txt.configure(state="disabled")

    # 按钮区
    btns = tk.Frame(root)
    btns.pack(fill="x", padx=12, pady=(0, 12))
    tk.Button(btns, text="重新连接", command=lambda: subprocess.Popen(_launch_cmd())
              ).pack(side="left")
    tk.Button(btns, text="清空日志",
              command=lambda: (LOG_FILE.write_text("", encoding="utf-8"))
              ).pack(side="left", padx=(8, 0))
    tk.Button(btns, text="打开日志文件", command=lambda: _open_log()
              ).pack(side="left", padx=(8, 0))

    last_size = [0]

    def refresh():
        s = state.get()
        if s.streaming:
            lbl_status.configure(text=f"● 语音中 · {s.device or '遥控器'}", fg="#e86030")
        elif s.connected:
            lbl_status.configure(text=f"● 已连接 · {s.device or '遥控器'}", fg="#ea8c1e")
        else:
            lbl_status.configure(text="○ 未连接", fg="#999999")

        cfg = Config.load()
        im = INPUT_METHODS.get(cfg.input_method, {})
        lbl_im.configure(text=f"输入法：{im.get('desc', cfg.input_method)}")

        # 电平
        canvas.delete("bar")
        cw = max(1, canvas.winfo_width())
        fill_w = int(cw * min(100, max(0, s.level)) / 100)
        if fill_w > 0:
            canvas.create_rectangle(0, 0, fill_w, 10, fill="#e86030",
                                    outline="", tags="bar")

        # 日志增量追加
        try:
            if LOG_FILE.exists():
                size = LOG_FILE.stat().st_size
                if size < last_size[0]:
                    last_size[0] = 0
                    txt.configure(state="normal")
                    txt.delete("1.0", "end")
                    txt.configure(state="disabled")
                if size > last_size[0]:
                    with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_size[0])
                        chunk = f.read()
                    last_size[0] = size
                    txt.configure(state="normal")
                    txt.insert("end", chunk)
                    txt.see("end")
                    txt.configure(state="disabled")
        except Exception:
            pass

        root.after(400, refresh)

    def on_close():
        global _console_open
        with _console_lock:
            _console_open = False
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh()
    root.mainloop()


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

    threading.Thread(target=_bridge_worker, daemon=True).start()

    _icon = pystray.Icon(APP_NAME, _icon_image(), f"{APP_NAME}（启动中…）")
    _icon.menu = build_menu(_icon)

    def _refresh(icon=None):
        """定时刷新图标与菜单，反映最新状态。"""
        s = state.get()
        _icon.icon = _icon_image(connected=s.connected, streaming=s.streaming)
        _icon.title = _status_text()
        try:
            _icon.update_menu()
        except Exception:
            pass
        if not _stop.is_set():
            threading.Timer(0.8, _refresh).start()

    _icon.menu = build_menu(_icon)
    threading.Timer(0.8, _refresh).start()

    _icon.run()


if __name__ == "__main__":
    main()
