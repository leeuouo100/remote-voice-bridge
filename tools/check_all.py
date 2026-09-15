"""跑一遍全部静态校验，把结果汇总到一个 UTF-8 报告里（供 CI / 本地一键验收）。

用法： python tools/check_all.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

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
                  "tools/check_ble_callback_thread.py",
                  "tools/diag_remote.py", "tools/check_packaging.py",
                  "tools/check_injection.py", "tools/check_all.py"]),
    ("版本号一致性", [PY, "tools/check_version.py"]),
    # spec / installer.iss 的一致性：诊断工具必须真的被打进安装包。
    # v1.0.5 就是漏了这一步 —— 工具写好了、也发了版，但安装版用户拿不到
    # （机器上没 Python，仓库里的 .bat 跑不起来）。纯静态，几毫秒。
    ("打包入口一致性", [PY, "tools/check_packaging.py"]),
    ("按键映射表", [PY, "tools/check_keymap.py"]),
    ("控制台冒烟", [PY, "tools/smoke_console.py"]),
    ("录制器逻辑", [PY, "tools/test_recorder.py"]),
    # BLE 回调线程没有事件循环 —— v1.0.3 事故的回归闸。
    # 纯标准库、不需要真机，所以放 CI 里跑。
    ("BLE 回调线程", [PY, "tools/check_ble_callback_thread.py"]),
    # 诊断脚本的真机部分要人按键，没法自动化；但报告生成器是纯函数式的，
    # 用假数据把两条分支（能区分 / 不能区分）都验一遍 —— 不然那段代码
    # 第一次运行就是在用户机器上。不需要 keyboard 库。
    ("遥控器诊断报告", [PY, "tools/diag_remote.py", "--selftest"]),
    # 注入自检会真的发按键，无桌面会话里自己会 SKIPPED（不算失败）。
    # 它是唯一能拦住"SendInput 静默失效"和"组合键顺序错"的一关。
    #
    # ⚠ 它是**唯一受机器负载影响**的一关（读的是系统实时按键状态，而低级键盘钩子
    #   有超时机制）。刚跑完前面几个会起进程/线程的步骤、机器还没静下来时跑它，
    #   就会出现"钩子没收到"这一类假 FAIL。所以：
    #     ① settle：先等 2 秒，让前一步的进程彻底退干净
    #     ② retries：整步再跑一次（脚本内部本身也已经重试 6 轮）
    #   settle/retries 只加在它身上，别的步骤该红就红。
    ("按键注入自检", [PY, "tools/check_injection.py"],
     {"settle": 2.0, "retries": 1, "retry_hint": "钩子没收到"}),
]

STEP_DEFAULTS = {"settle": 0.0, "retries": 0, "retry_hint": ""}


def _run_step(cmd: list[str]) -> tuple[bool, str]:
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()


def main() -> int:
    fails = 0
    for step in STEPS:
        name, cmd = step[0], step[1]
        opt = {**STEP_DEFAULTS, **(step[2] if len(step) > 2 else {})}

        if opt["settle"]:
            time.sleep(opt["settle"])

        ok, out = _run_step(cmd)
        # 只在"失败原因是已知的环境噪声"时才重试 ——
        # 真回归（结果不对）重试也是浪费，而且掩盖不了任何东西。
        for _ in range(opt["retries"]):
            if ok:
                break
            if opt["retry_hint"] and opt["retry_hint"] not in out:
                break
            print(f"[.... ] {name}：疑似机器忙，重跑一次…")
            time.sleep(1.5)
            ok, out = _run_step(cmd)

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
