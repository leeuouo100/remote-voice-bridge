"""跑一遍全部静态校验，把结果汇总到一个 UTF-8 报告里（供 CI / 本地一键验收）。

用法： python tools/check_all.py
"""
from __future__ import annotations

import os
import subprocess
import sys

from _utf8 import setup as _setup_utf8  # 下面是中文输出，先钉住编码（CI 是 cp1252）

_setup_utf8()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

STEPS = [
    ("编译全部模块", [PY, "-m", "py_compile",
                  "config.py", "state.py", "keys.py", "session.py", "buttons.py",
                  "mixer.py", "main.py", "console_server.py", "tray_app.py",
                  "tools/_utf8.py",
                  "tools/check_version.py", "tools/check_keymap.py",
                  "tools/smoke_console.py", "tools/test_recorder.py",
                  "tools/check_injection.py", "tools/check_all.py"]),
    ("版本号一致性", [PY, "tools/check_version.py"]),
    ("按键映射表", [PY, "tools/check_keymap.py"]),
    ("控制台冒烟", [PY, "tools/smoke_console.py"]),
    ("录制器逻辑", [PY, "tools/test_recorder.py"]),
    # 注入自检会真的发按键，无桌面会话里自己会 SKIPPED（不算失败）。
    # 它是唯一能拦住"SendInput 静默失效"和"组合键顺序错"的一关。
    ("按键注入自检", [PY, "tools/check_injection.py"]),
]


def main() -> int:
    fails = 0
    for name, cmd in STEPS:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        ok = p.returncode == 0
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
        if out:
            for line in out.splitlines():
                print("       " + line)
        if not ok:
            fails += 1
    print()
    print("ALL CHECKS PASSED" if not fails else f"{fails} 项校验未通过")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
