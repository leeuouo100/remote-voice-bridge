"""
遥控器 HID 报告监听（命令行）—— 一次按键，看清它发在哪一路。

用法：
    python tools/watch_reports.py              监听 40 秒
    python tools/watch_reports.py --seconds 60
    python tools/watch_reports.py --all        连非 Google 的 HID 集合一起看

产出：
    · 屏幕上实时打印每一条事件
    · 报告写到 %APPDATA%\\remote-voice-bridge\\remote-hidwatch.txt

⚠ 跑之前**先退出桥接程序**（托盘右键 → 退出）：它开着时遥控器原生按键会被
  它的拦截钩子吞掉，报告会显示"一条都没收到"，看着像遥控器坏了。
  这与 tools/diag_remote.py 的规矩一致。

## 怎么读结果

脚本最后会给出【判读】。三种典型结果对应三种完全不同的修法：

  · 键盘钩子有计数           → 按键到了 Windows，问题在映射层
  · 只有厂商页有计数         → Windows 不翻译那一页，必须自己解报告
  · 只有鼠标集合有计数       → 按键当鼠标发，键盘映射对它无效
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _utf8 import setup as _setup_utf8  # noqa: E402

_setup_utf8()

import hidinfo      # noqa: E402
import hidwatch     # noqa: E402
from config import APP_VERSION, CONFIG_DIR  # noqa: E402

REPORT = CONFIG_DIR / "remote-hidwatch.txt"


def _bridge_running() -> str:
    """桥接程序还在跑吗？理由同 diag_remote：带着它测出来的报告一定是错的。"""
    import subprocess
    try:
        p = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq RemoteVoiceBridge.exe", "/NH"],
            capture_output=True, text=True, timeout=6,
            creationflags=0x08000000,
        )
        if "RemoteVoiceBridge.exe" in (p.stdout or ""):
            return "进程 RemoteVoiceBridge.exe 正在运行"
    except Exception:                                   # noqa: BLE001
        pass
    return ""


def main() -> int:
    if not hidwatch.available():
        print("SKIPPED（非 Windows）")
        return 0

    seconds = 40
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--seconds" and i + 1 < len(argv):
            try:
                seconds = max(5, min(600, int(argv[i + 1])))
            except ValueError:
                pass
        if a in ("-h", "--help"):
            print(__doc__)
            return 0
    only_google = "--all" not in argv
    force = "--force" in argv

    running = _bridge_running()
    if running and not force:
        print()
        print(f"  ⛔ 桥接程序还在运行（{running}）。")
        print()
        print("     为什么必须先退出它：它的拦截钩子会把遥控器原生按键吞掉，")
        print("     报告会显示「一条都没收到」—— 看着像遥控器坏了，其实是被拦了。")
        print("     （确实要带着它测、并接受结论可能不准：加 --force）")
        return 2

    print("=" * 68)
    print(f" 遥控器 HID 报告监听  ·  remote-voice-bridge v{APP_VERSION}")
    print("=" * 68)
    print()

    # 先来一段不用按键的硬件身份，出了事好对照
    snap = hidinfo.probe()
    print(hidinfo.format_report(snap))
    print()

    w = hidwatch.ReportWatcher(only_google=only_google)
    opened = w.start(with_hooks=True)
    print(f"已打开 {opened} 路 HID 集合，键盘钩子{'已' if w._hooks else '未'}挂上。")
    print()
    print("=" * 68)
    print(f" 现在开始监听 {seconds} 秒 —— 请**依次按遥控器的每个键**，每个按 1~2 下：")
    print("   方向上下左右 → 确认 → 返回 → 主页 → 音量＋ → 音量－ → 静音")
    print("   （语音键会走蓝牙音频通道，这里看不到，属正常）")
    print("=" * 68)
    print()

    t0 = time.time()
    last_line = 0.0
    try:
        while time.time() - t0 < seconds:
            # 有事件就即时打出来（on_event 已经在收集，这里做增量显示）
            time.sleep(0.2)
            if time.time() - last_line > 1.0:
                last_line = time.time()
                n = sum(len(c.reports) for c in w.collections) + len(w.key_events)
                print(f"  … 已监听 {time.time() - t0:4.1f}s / {seconds}s，"
                      f"共收到 {n} 条", end="\r")
    except KeyboardInterrupt:
        print("\n  （被 Ctrl+C 中断，照样出报告）")

    print()
    print()
    w.stop()

    lines = [
        "=" * 68,
        " remote-voice-bridge 遥控器 HID 报告监听",
        f" 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f" 程序版本：v{APP_VERSION}",
        f" 监听时长：{time.time() - t0:.1f} 秒",
        "=" * 68,
        "",
        hidinfo.format_report(snap, indent=""),
        "",
        w.summary(),
        "",
        "【明细】每条事件（前 80 条）",
        "",
    ]
    merged: list[tuple[float, str]] = []
    for ts, kind, name, scan in w.key_events:
        merged.append((ts, f"⌨ 键盘事件  name={name!r} scan={scan}"))
    for c in w.collections:
        for ts, data in c.reports:
            merged.append((ts, f"📦 {c.key}  {data.hex(' ')}"))
    for ts, text in sorted(merged)[:80]:
        lines.append(f"  [{ts - t0:7.3f}s] {text}")
    if not merged:
        lines.append("  （没有收到任何事件）")

    text = "\n".join(lines) + "\n"
    try:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(text, encoding="utf-8")
        print(f"报告已写出：{REPORT}")
        print("把这个文件发出来即可。")
    except Exception as e:                              # noqa: BLE001
        print(f"⚠ 写文件失败（{e}），下面是全文：\n")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
