"""电平/波形"有消费者、没生产者"检查 —— 防止某一格读数永远不动。

背景（v1.0.8 的真机事故，原因很典型）：
  控制台「遥控器麦克风」卡片读的是 state.remote_level_db，而它只由
  state.push_levels(remote_db=…) 写入 —— 但全项目**没有任何一处产品代码传过
  remote_db**（只有 tools/serve_console.py 那个假数据演示服务器传过）。
  结果真机上它永远是初始值 -96 dBFS：
      波形在动（走 push_audio 的 _wave），状态却死死卡在「等待语音」、
      电平条一格都不亮 —— 看着就是"这一路完全没反应"。
  根因不是音频，是"只有消费者、没有生产者"。这类洞静态就能查出来，
  所以在这里一次性钉死。

检查项：
  ① 三路电平各自都有生产者（在真实产品代码里，而不是演示脚本里）
       · 电脑麦克风   ← mixer.SystemMic 的采集回调
       · 遥控器麦克风 ← main.py 的 ATVV 解码回调 on_audio
       · 混合输出     ← main.py 的播放回调 cb
  ② 控制台真的在读这三个字段（有人改了名字 → UI 会静默变成永远 -96）
  ③ 动态行为：push_levels(remote_db=) 能生效；clear_audio() 会把它归零；
     push_audio() 会真的往遥控器波形里写点
     （②③ 一起才能证明"波形有、电平没有"这种半死状态不会再出现）

自检：把生产者那行去掉后本检查必须报 FAIL —— 没有这一条，
本脚本可能只是个永远绿的摆设，那比没有更糟。

用法： python tools/check_levels.py
"""
from __future__ import annotations

import os
import re
import sys

from _utf8 import setup as _setup_utf8  # 下面是中文输出，先钉住编码

_setup_utf8()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _section(text: str, func: str) -> str:
    """取某个顶层函数的正文（到下一个顶层 def/class 或文件尾）。"""
    m = re.search(rf"^def {func}\(.*?(?=^def |^class |\Z)", text, re.S | re.M)
    return m.group(0) if m else ""


# (通道名, 生产者所在文件, 生产者必须出现的写法)
CHANNELS = [
    ("电脑麦克风",   "mixer.py",  "sys_db="),
    ("遥控器麦克风", "main.py",   "remote_db="),
    ("混合输出",     "main.py",   "mix_db="),
]


def static_checks(files: dict[str, str]) -> list[tuple[bool, str]]:
    """纯静态部分。files 是 {相对路径: 正文}，自检时换成篡改过的版本。"""
    checks: list[tuple[bool, str]] = []

    for label, rel, marker in CHANNELS:
        body = files.get(rel, "")
        checks.append((marker in body,
                       f"{label}电平有生产者（{rel} 里的 push_levels({marker}…)）"))

    # ② 控制台真的在读这三路 —— 只查遥控器那一路就够醒目，
    #    但三路一起查成本一样，索性查全，改名漏改一处就报警。
    console = files.get("console_server.py", "")
    for field in ("sys_level_db", "remote_level_db", "mix_level_db"):
        checks.append((f"s.{field}" in console,
                       f"控制台在读 {field}（改名后不会静默变成永远 -96）"))

    # 生产者的调用方与字段本身要对得上：state 里得有这个字段
    st = files.get("state.py", "")
    checks.append(("remote_level_db" in st, "state.BridgeState 里有 remote_level_db 字段"))
    checks.append(("remote_level_db" in _section(st, "clear_audio"),
                   "clear_audio() 会把遥控器电平归零（新会话从没声音开始画）"))
    return checks


def dynamic_checks() -> list[tuple[bool, str]]:
    """跑一遍真实 state 模块：证明写入真的生效、清空真的清空。"""
    import state

    checks: list[tuple[bool, str]] = []

    state.push_levels(remote_db=-20.0)
    checks.append((abs(state.get().remote_level_db - (-20.0)) < 0.01,
                   "push_levels(remote_db=) 真的写进去了"))

    # 波形这条路径本来就是好的 —— 正是"波形有、电平没有"那种半死状态最坑人，
    # 所以两条一起验：谁断了都算这颗闸没过。
    state.clear_audio()
    state.push_audio([100, -200, 300, -400], 50, 1, 400, 16000, 128)
    wave = state.waves_payload()["remote"]
    checks.append((len(wave) >= 4, f"push_audio() 会往遥控器波形里写点（现有 {len(wave)} 点）"))
    checks.append((abs(state.get().remote_level_db + 96.0) < 0.01,
                   "只推音频帧不会伪造电平（电平只能由 push_levels 给）"))

    state.clear_audio()
    checks.append((abs(state.get().remote_level_db + 96.0) < 0.01,
                   "clear_audio() 之后遥控器电平回到 -96 dBFS"))

    state.reset()
    return checks


def _selftest() -> int:
    """自检：把生产者删掉，检查必须报 FAIL；否则这颗闸形同虚设。"""
    files = {rel: _read(rel) for _, rel, _ in CHANNELS}
    files["console_server.py"] = _read("console_server.py")
    files["state.py"] = _read("state.py")

    # 篡改：删掉遥控器电平的生产者那一行
    dirty = dict(files)
    dirty["main.py"] = re.sub(r"push_levels\(remote_db=[^)]*\)", "", files["main.py"])
    bad = [m for ok, m in static_checks(dirty) if not ok]
    if not bad:
        print("  FAIL 自检：拿掉了遥控器电平的生产者，检查居然还是绿的")
        return 1
    if not any("遥控器" in m for m in bad):
        print(f"  FAIL 自检：报错信息没指到遥控器那一路：{bad}")
        return 1
    print(f"  OK   自检：拿掉生产者后确实报 FAIL（{len(bad)} 项）")

    # 篡改：删掉 clear_audio 里的归零
    dirty2 = dict(files)
    dirty2["state.py"] = files["state.py"].replace(
        "_state.remote_level_db = -96.0", "", 1)
    bad2 = [m for ok, m in static_checks(dirty2) if not ok]
    if not bad2:
        print("  FAIL 自检：拿掉 clear_audio 里的归零，检查居然还是绿的")
        return 1
    print(f"  OK   自检：拿掉 clear_audio 里归零后确实报 FAIL（{len(bad2)} 项）")
    return 0


def main() -> int:
    files = {rel: _read(rel) for _, rel, _ in CHANNELS}
    files["console_server.py"] = _read("console_server.py")
    files["state.py"] = _read("state.py")

    checks = static_checks(files) + dynamic_checks()

    bad = [m for ok, m in checks if not ok]
    for ok, m in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {m}")

    print("  --- 自检 ---")
    rc = _selftest()

    if bad or rc:
        print(f"LEVELS CHECK FAILED（{len(bad)} 项）")
        print("  提示：界面上的每一格读数都必须有人在喂它，"
              "否则用户看到的是「一直没反应」，而不是「没声音」。")
        return 1
    print("LEVELS OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
