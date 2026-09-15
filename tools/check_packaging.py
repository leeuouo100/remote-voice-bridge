"""打包一致性检查 —— 防止「诊断工具没打进安装包」这类洞再次发生。

背景：v1.0.5 加了遥控器诊断工具，但它只是仓库里的 .py + .bat，
没进 Setup.exe。安装版用户机器上通常没有 Python，那个工具等于不存在。
根因是只在源码环境验证过就发了版。

这里做四件事（纯静态、不跑构建，CI 里几毫秒）：

  ① spec 里确实有第二个 EXE 入口（诊断工具），且入口脚本文件存在
  ② 诊断 EXE 是 console 版 —— 主程序是 console=False，
     诊断要命令行交互，做成 GUI 版双击起来什么都看不见
  ③ COLLECT 真的收了诊断 EXE —— 少一行的话 exe 不会被放进 dist/
  ④ installer.iss 里定义的名字与 spec 一致、并且开始菜单真的引用了它
     （名字对不上，装了也点不开）
  ⑤ 蓝牙配对修复（--fix-pairing → pairing.py）确实进了包：
     模块被 hiddenimports 收、winrt 收进来（真机验收要用）、
     开始菜单有入口、.bat 存在且被 [Files] 拷进安装目录

第 ⑤ 条是 v1.0.10 补的：它治的是「换 USB 口之后程序**完全连不上**」，
那正是用户最需要工具的时候 —— 这种能力要是只在源码树里，等于没做。

用法： python tools/check_packaging.py
"""
from __future__ import annotations

import os
import re
import sys

from _utf8 import setup as _setup_utf8  # 下面是中文输出，先钉住编码

_setup_utf8()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _section(text: str, name: str) -> str:
    """取 .iss 里某个 [Section] 的正文。"""
    m = re.search(rf"^\[{name}\]\s*$([\s\S]*?)(?=^\[|\Z)", text, re.M)
    return m.group(1) if m else ""


def main() -> int:
    spec = _read("remote-voice-bridge.spec")
    iss = _read("installer.iss")

    checks: list[tuple[bool, str]] = []

    # ① spec 里的诊断入口
    m = re.search(r"name\s*=\s*['\"]([^'\"]*Diag[^'\"]*)['\"]", spec)
    diag_name = m.group(1) if m else ""
    checks.append((bool(m), f"spec 有诊断 EXE 入口（{diag_name or '没找到'}）"))

    entry = "tools/diag_remote.py"
    has_entry = entry in spec
    file_ok = os.path.isfile(os.path.join(ROOT, entry))
    checks.append((has_entry and file_ok,
                   f"spec 收集的是 {entry}，且文件存在"))

    # ② 诊断 EXE 必须是 console 版
    checks.append((bool(re.search(r"console\s*=\s*True", spec)),
                   "诊断 EXE 是 console 版（能打印交互内容）"))

    # ③ COLLECT 收了诊断 EXE
    m = re.search(r"COLLECT\(([\s\S]*?)\n\)", spec)
    collected = m.group(1) if m else ""
    checks.append(("diag_exe" in collected, "COLLECT 里包含 diag_exe"))

    # ④ 与 installer.iss 对得上
    # 注意两边写法本来就不同：spec 的 EXE(name=) 不带扩展名，
    # iss 里指的是磁盘上的文件名，带 .exe。比对时把后缀去掉再比。
    m = re.search(r"#define\s+MyDiagExeName\s+\"([^\"]+)\"", iss)
    iss_name = m.group(1) if m else ""
    iss_stem = re.sub(r"\.exe$", "", iss_name, flags=re.I)
    checks.append((bool(m) and bool(diag_name) and iss_stem == diag_name,
                   f"installer.iss 定义的诊断 EXE 名与 spec 一致"
                   f"（iss={iss_name or '没找到'} / spec={diag_name or '没找到'}）"))

    icons = _section(iss, "Icons")
    checks.append(("{#MyDiagExeName}" in icons,
                   "开始菜单里真的有诊断入口（{#MyDiagExeName}）"))

    # ⑤ 蓝牙配对修复能力进包（v1.0.10 加的闸）
    # 为什么单列：它解决的是「换 USB 口之后**程序完全连不上**」——
    # 那正是用户最需要工具的时候。这个能力如果只在源码树里，等于没做。
    pair_mod = os.path.isfile(os.path.join(ROOT, "pairing.py"))
    checks.append((pair_mod, "仓库根目录有 pairing.py（修复逻辑本体）"))

    # 取 diag 那个 Analysis 的正文（变量名叫 diag，不是 name='diag' ——
    # 第一版就是照 name='diag' 去找的，结果三条断言全 FAIL，白跑一轮）
    m = re.search(r"^\s*diag\s*=\s*Analysis\(([\s\S]*?)\n\)", spec, re.M)
    diag_block = m.group(1) if m else ""
    checks.append((bool(diag_block), "spec 里能定位到 diag 那个 Analysis 块"))

    checks.append(("'pairing'" in diag_block,
                   "诊断 EXE 的 hiddenimports 里有 pairing（否则 --fix-pairing 直接崩）"))
    checks.append(("winrt" in diag_block.split("hiddenimports")[0]
                   and "winrt" in diag_block,
                   "诊断 EXE 收进了 winrt（真机验收要用它把 BLE 设备打开一次）"))

    checks.append(("--fix-pairing" in _read("tools/diag_remote.py"),
                   "diag_remote.py 把 --fix-pairing 转给了 pairing"))
    checks.append(("--fix-pairing" in icons,
                   "开始菜单有「修复蓝牙配对」入口（Parameters: --fix-pairing）"))

    m = re.search(r"#define\s+MyFixBatName\s+\"([^\"]+)\"", iss)
    bat_name = m.group(1) if m else ""
    bat_ok = bool(bat_name) and os.path.isfile(os.path.join(ROOT, bat_name))
    checks.append((bat_ok, f".bat 入口存在且名字对得上（{bat_name or '没找到'}）"))
    # iss 里引用的是宏，不是字面文件名 —— 所以比的是 {#MyFixBatName}
    checks.append(("{#MyFixBatName}" in _section(iss, "Files"),
                   "[Files] 里真的把它拷进安装目录"))

    bad = [msg for ok, msg in checks if not ok]
    for ok, msg in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {msg}")

    if bad:
        print(f"PACKAGING CHECK FAILED（{len(bad)} 项）")
        print("  提示：诊断工具没进安装包 = 安装版用户根本拿不到它。")
        return 1
    print("PACKAGING OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
