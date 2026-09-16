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
                  "hidinfo.py", "hidwatch.py", "pairing.py", "remote_hid.py",
                  "tools/_utf8.py",
                  "tools/check_version.py", "tools/check_keymap.py",
                  "tools/smoke_console.py", "tools/test_recorder.py",
                  "tools/check_ble_callback_thread.py",
                  "tools/diag_remote.py", "tools/check_packaging.py",
                  "tools/check_levels.py", "tools/check_mix_persist.py",
                  "tools/watch_reports.py", "tools/check_hidinfo.py",
                  "tools/check_failure_visibility.py", "tools/check_pairing.py",
                  "tools/check_remote_hid.py", "tools/check_injection.py",
                  "tools/check_all.py"]),
    ("版本号一致性", [PY, "tools/check_version.py"]),
    # spec / installer.iss 的一致性：诊断工具必须真的被打进安装包。
    # v1.0.5 就是漏了这一步 —— 工具写好了、也发了版，但安装版用户拿不到
    # （机器上没 Python，仓库里的 .bat 跑不起来）。纯静态，几毫秒。
    ("打包入口一致性", [PY, "tools/check_packaging.py"]),
    # 三路电平/波形"有消费者、没有生产者"的洞：v1.0.7 真机上「遥控器麦克风」
    # 波形在动、状态却永远卡在「等待语音」，根因就是**没有任何产品代码喂**
    # state.remote_level_db。纯静态 + 几行真跑 state 模块，带反例自检。
    ("电平生产者一致性", [PY, "tools/check_levels.py"]),
    # 「界面上关了、后端还在用」—— 音频页的开关以前只改内存、不落盘，
    # 设置页一动或重连就被 config.json 悄悄回滚。这是最难查的一类 bug：
    # 一点声音都没有，用户只会说"它自己不听话"。沙箱 APPDATA，不碰真配置。
    ("混音开关落盘", [PY, "tools/check_mix_persist.py"]),
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
    # 遥控器的 HID 判定链。v1.0.8 查「除语音键外所有按键都没反应」时，
    # 真机挖出一条以前没人写下来的硬事实：遥控器暴露了**两个厂商自定义
    # 用法页**（0xFF01/0xFF80，各 21 字节输入报告），Windows 对它们
    # 不做任何处理。这条事实直接决定"该修什么"，所以判定表 + 报告判读
    # 分支都用反例锁住。纯静态 + 假数据，CI 里可跑。
    ("HID 判定链", [PY, "tools/check_hidinfo.py"]),
    # 故障必须"看得见"且"说人话"。
    # v1.0.9 的真机事故：桥线程用 print 报异常，而主 exe 是 console=False ——
    # print 的流向是空的，于是桥每 3 秒崩一次、日志一行报错都没有、
    # 托盘还写着「按遥控器任意键唤醒」，把 OSError: E_INVALIDARG 捂了两小时。
    # 纯静态 + 反例，不需要真机。
    ("故障可见性", [PY, "tools/check_failure_visibility.py"]),
    # 蓝牙配对判定链（pairing.py）。
    # v1.0.10 加的这一关，治的是「换 USB 口 → 本地蓝牙地址变 → 配对记录作废」：
    # 程序显示「未连接」、Windows 显示「已配对」、设置里还删不掉。
    # 判定逻辑读的是真机注册表，CI 里没有蓝牙棒 —— 所以抽成纯函数 + 假数据，
    # 把真机形态（记录绑旧地址、AEP 节点也是旧地址）钉成 STALE_ADDR，
    # 再拿反例锁住两处最容易退化的地方（PnP 实例 ID 的 USB\ 前缀、
    # 搬家时保留注册表值类型）。
    ("蓝牙配对判定", [PY, "tools/check_pairing.py"]),
    # 厂商页按键解码（v1.0.11：按键映射全靠这一路）。
    # 解码表错一位就是「按上键出来的是返回」这种全串位的事故，
    # 而报告格式的假设（reportID=0x01、第 2 字节是用法码、0=松手）
    # 一旦错了，现象和"遥控器没连上"一模一样 —— 必须先用纯单测钉死，
    # 真机上按下键之后只要对焦"报告来没来"这一件事。
    ("厂商页按键解码", [PY, "tools/check_remote_hid.py"]),
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
