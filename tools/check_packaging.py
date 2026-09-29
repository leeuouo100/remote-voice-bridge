"""打包一致性检查 —— 防止「工具没打进安装包」这类洞再次发生。

背景：v1.0.5 加了遥控器诊断工具，但它只是仓库里的 .py + .bat，
没进 Setup.exe。安装版用户机器上通常没有 Python，那个工具等于不存在。
根因是只在源码环境验证过就发了版。

这里做六件事（纯静态、不跑构建，CI 里几毫秒）：

  ① spec 里确实有第二个 EXE 入口（诊断工具），且入口脚本文件存在
  ② 诊断 EXE 是 console 版 —— 主程序是 console=False，
     诊断要命令行交互，做成 GUI 版双击起来什么都看不见
  ③ COLLECT 真的收了诊断 EXE —— 少一行的话 exe 不会被放进 dist/
  ④ installer.iss 里定义的名字与 spec 一致、并且开始菜单真的引用了它
     （名字对不上，装了也点不开）
  ⑤ 蓝牙配对修复（--fix-pairing → pairing.py）确实进了包：
     模块被 hiddenimports 收、winrt 收进来（真机验收要用）、
     开始菜单有入口、.bat 存在且被 [Files] 拷进安装目录
  ⑥ 按键旁路（v1.0.20：注入 WUDFHost 读 HID 报告）确实进了包：
     `frida_tap.js` 作为 data 收进主 EXE、`frida_hid` 在 hiddenimports 里

第 ⑤ 条是 v1.0.10 补的：它治的是「换 USB 口之后程序**完全连不上**」，
那正是用户最需要工具的时候 —— 这种能力要是只在源码树里，等于没做。

第 ⑥ 条是 v1.0.20 补的，**和 ⑤ 是同一类洞**：旁路是真机上唯一能拿到遥控器
按键的那一路，它的文件（`frida_tap.js`）漏进包里的表现是
「**语音完全正常、除语音键外的按键一个都不灵**」—— 用户会以为"按键又坏了"，
而日志里只有一行"读不到 frida_tap.js"的 warning。这正是本闸门存在的理由。

用法：
    python tools/check_packaging.py             # 正常检查
    python tools/check_packaging.py --selftest  # 闸门自检（含反例）
"""
from __future__ import annotations

import os
import re
import sys

from _utf8 import setup as _setup_utf8  # 下面是中文输出，先钉住编码

_setup_utf8()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str, root: str = ROOT) -> str:
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return f.read()


def _section(text: str, name: str) -> str:
    """取 .iss 里某个 [Section] 的正文。"""
    m = re.search(rf"^\[{name}\]\s*$([\s\S]*?)(?=^\[|\Z)", text, re.M)
    return m.group(1) if m else ""


def _analysis_block(spec: str, var: str) -> str:
    """取 spec 里 `var = Analysis(...)` 的正文。

    ⚠ 必须按**变量名**定位，不能按 `name='…'` 去找 —— 第一版就是那么写的，
    结果三条断言全 FAIL、白跑一轮（变量叫 `diag`，不叫 `name='diag'`）。
    """
    m = re.search(rf"^\s*{var}\s*=\s*Analysis\(([\s\S]*?)\n\)", spec, re.M)
    return m.group(1) if m else ""


def _datas_line(block: str) -> str:
    """取 Analysis 块里 `datas=` 那一行的右值。"""
    m = re.search(r"^\s*datas\s*=\s*(.+)$", block, re.M)
    return m.group(1) if m else ""


def collect_checks(spec: str, iss: str, root: str = ROOT) -> list[tuple[bool, str]]:
    """把 spec / iss 文本过一遍，返回 [(是否通过, 说明)]。

    抽成纯函数（不读盘、不打印）只为一件事：`--selftest` 能把文本改坏再喂进来，
    验证这道闸**自己会红**。闸门不会红的闸门等于没有闸门。
    """
    checks: list[tuple[bool, str]] = []

    # ① spec 里的诊断入口
    m = re.search(r"name\s*=\s*['\"]([^'\"]*Diag[^'\"]*)['\"]", spec)
    diag_name = m.group(1) if m else ""
    checks.append((bool(m), f"spec 有诊断 EXE 入口（{diag_name or '没找到'}）"))

    entry = "tools/diag_remote.py"
    has_entry = entry in spec
    file_ok = os.path.isfile(os.path.join(root, entry))
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
    pair_mod = os.path.isfile(os.path.join(root, "pairing.py"))
    checks.append((pair_mod, "仓库根目录有 pairing.py（修复逻辑本体）"))

    diag_block = _analysis_block(spec, "diag")
    checks.append((bool(diag_block), "spec 里能定位到 diag 那个 Analysis 块"))

    checks.append(("'pairing'" in diag_block,
                   "诊断 EXE 的 hiddenimports 里有 pairing（否则 --fix-pairing 直接崩）"))
    checks.append(("winrt" in diag_block.split("hiddenimports")[0]
                   and "winrt" in diag_block,
                   "诊断 EXE 收进了 winrt（真机验收要用它把 BLE 设备打开一次）"))

    checks.append(("--fix-pairing" in _read("tools/diag_remote.py", root),
                   "diag_remote.py 把 --fix-pairing 转给了 pairing"))
    checks.append(("--fix-pairing" in icons,
                   "开始菜单有「修复蓝牙配对」入口（Parameters: --fix-pairing）"))

    m = re.search(r"#define\s+MyFixBatName\s+\"([^\"]+)\"", iss)
    bat_name = m.group(1) if m else ""
    bat_ok = bool(bat_name) and os.path.isfile(os.path.join(root, bat_name))
    checks.append((bat_ok, f".bat 入口存在且名字对得上（{bat_name or '没找到'}）"))
    # iss 里引用的是宏，不是字面文件名 —— 所以比的是 {#MyFixBatName}
    checks.append(("{#MyFixBatName}" in _section(iss, "Files"),
                   "[Files] 里真的把它拷进安装目录"))

    # ⑥ 按键旁路进包（v1.0.20 加的闸）
    #
    # 和 ⑤ 是同一类洞，但更隐蔽：旁路靠**两个**东西才跑得起来 ——
    #   ① `frida_tap.js`：Frida 脚本本体，必须是主 EXE 的 data（spec 的 tap_datas）；
    #   ② `frida_hid`：模块本体，必须被 hiddenimports 收（它在 run_bridge 内部
    #      才 import，PyInstaller 对函数内的 import 只给 warning，有时会漏收）。
    # 少任何一个，现象都是「语音一切正常、除语音键外的按键一个都不灵」——
    # 用户只会觉得"按键又坏了"，而日志里就一行 warning。
    # ⚠ 比对用带引号的 `'frida_hid'`：spec 的**注释里**也写着 frida_hid.py
    #   （反引号包着），不加引号会连注释一起匹配上 ⇒ 漏收也照样绿。
    tap_js = os.path.isfile(os.path.join(root, "frida_tap.js"))
    tap_py = os.path.isfile(os.path.join(root, "frida_hid.py"))
    checks.append((tap_js, "仓库根目录有 frida_tap.js（Frida 脚本本体）"))
    checks.append((tap_py, "仓库根目录有 frida_hid.py（旁路的 Python 侧）"))

    tap_def = re.search(r"^tap_datas\s*=\s*\[([^\]]*)\]", spec, re.M)
    checks.append((bool(tap_def) and "frida_tap.js" in tap_def.group(1),
                   "spec 定义了 tap_datas 且指向 frida_tap.js"))

    a_block = _analysis_block(spec, "a")
    checks.append((bool(a_block), "spec 里能定位到 a（主程序）那个 Analysis 块"))
    checks.append(("tap_datas" in _datas_line(a_block),
                   "主 EXE 的 datas 里真的加上了 tap_datas（否则 js 不进包）"))
    checks.append(("'frida_hid'" in a_block,
                   "主 EXE 的 hiddenimports 里有 'frida_hid'（否则运行时 import 直接失败）"))

    # ⑦ 安装器把整个 bundle 目录拷进去 —— 上面那些 data 才能落到 {app}
    # 少了 recursesubdirs 只拷一层的话，PyInstaller onedir 的内部结构会缺文件。
    files_sec = _section(iss, "Files")
    checks.append(("recursesubdirs" in files_sec,
                   "[Files] 递归拷整个 bundle（否则包内的 data/子目录会缺）"))

    return checks


def _selftest(spec: str, iss: str) -> int:
    """闸门自检：把输入改坏，对应断言必须变红。"""
    print("check_packaging 自检 —— 闸门自己不许永远亮绿灯")

    fails: list[str] = []

    def _find(checks: list[tuple[bool, str]], needle: str) -> bool | None:
        for ok, msg in checks:
            if needle in msg:
                return ok
        return None

    def _case(label: str, mutated: tuple[str, str], needle: str) -> None:
        checks = collect_checks(mutated[0], mutated[1], ROOT)
        got = _find(checks, needle)
        if got is None:
            print(f"  FAIL {label} —— 连「{needle}」这条断言都没找到")
            fails.append(label)
        elif got:
            print(f"  FAIL {label} —— 改坏了却还是绿的")
            fails.append(label)
        else:
            print(f"  OK   {label}")

    # ① 原样 → 0 红
    base = collect_checks(spec, iss, ROOT)
    n_bad = sum(1 for ok, _ in base if not ok)
    if n_bad:
        print(f"  FAIL 原样 → 0 红（实际 {n_bad}）")
        fails.append("原样")
    else:
        print(f"  OK   原样 → 0 红（共 {len(base)} 条断言）")

    # ② 反例：datas 里去掉 `+ tap_datas` ⇒ frida_tap.js 不进包
    _case("datas 漏掉 tap_datas → 必须红",
          (re.sub(r"\+\s*tap_datas", "", spec), iss),
          "datas 里真的加上了 tap_datas")

    # ③ 反例：hiddenimports 里去掉 'frida_hid' ⇒ 运行时 import 失败
    _case("hiddenimports 漏掉 'frida_hid' → 必须红",
          (spec.replace("'frida_hid', 'frida',", "'frida',"), iss),
          "hiddenimports 里有 'frida_hid'")

    # ④ 反例：tap_datas 指向了不存在的文件名
    _case("tap_datas 指向错文件名 → 必须红",
          (spec.replace("('frida_tap.js', '.')", "('tap.js', '.')"), iss),
          "spec 定义了 tap_datas 且指向 frida_tap.js")

    # ⑤ 反例：把「递归拷整个 bundle」删掉
    _case("[Files] 去掉 recursesubdirs → 必须红",
          (spec, iss.replace(" recursesubdirs", "")),
          "递归拷整个 bundle")

    if fails:
        print(f"PACKAGING SELFTEST FAILED（{len(fails)} 项）")
        return 1
    print("PACKAGING SELFTEST OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    spec = _read("remote-voice-bridge.spec")
    iss = _read("installer.iss")

    if "--selftest" in argv:
        return _selftest(spec, iss)

    checks = collect_checks(spec, iss, ROOT)
    bad = [msg for ok, msg in checks if not ok]
    for ok, msg in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {msg}")

    if bad:
        print(f"PACKAGING CHECK FAILED（{len(bad)} 项）")
        print("  提示：工具没进安装包 = 安装版用户根本拿不到它。")
        return 1
    print("PACKAGING OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
