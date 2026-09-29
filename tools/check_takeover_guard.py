"""闸：接管 0x1812 的那个工具，必须"默认不动、目标对、恢复有保证"。

## 为什么要有这道闸

`tools/takeover_hid_reports.py` 是这个项目里**唯一会去改系统设备状态**的东西：
它要临时 `Disable-PnpDevice` 掉一个节点，好让 Windows 的 HOGP 栈放开 0x1812。

一件"改坏不可逆、又要靠它下结论"的事，风险全在三个地方：
它默认就动手（用户只是想看看）、它动错了节点（结论直接错）、它没恢复回来（设备哑掉）。

### ① 默认只读 —— 不写进闸里就会被"顺手改成默认执行"

不写这道闸，后面谁把 `--go` 去掉"方便一点"，用户跑一次体检就可能动了设备。
所以：`--go` 必须是 `store_true`（默认 False），且"不 go"的分支必须在
`is_admin` 检查和 `Popen` **之前**就 `return`。

### ② 目标必须是 BTHLEDEVICE —— 这条是**真踩过**的

`0x1812` 底下不是一个节点，是一父 + 若干子：

    BTHLEDEVICE\\...\\9&XXXX&N&YYYY   HIDClass  mshidumdf   ← 握着 ATT/GATT 会话的
    HID\\...&COL01\\...               Keyboard  kbdhid
    HID\\...&COL03\\...               Mouse     mouhid
    HID\\...&COL02/04/05\\...         HIDClass  (无服务)

第一版写的是 `Get-PnpDevice | Where ... | Select-Object -First 1` —— 它拿到的是
枚举顺序里第一个，实测是 `COL05`（一个无服务的空集合）。禁掉它，会话还在父节点手里，
**一条报告也收不到**，然后工具会理直气壮地判"软件到头了、遥控器不发按键"。
那就是拿一个假阴性去关掉最后一条路 —— 所以这道闸钉死：目标只能是 `BTHLEDEVICE*`，
找不到就 `return 3`，绝不退而求其次。

### ③ 恢复必须攥在 PowerShell 那一侧

禁用 → 睡 → 启用，写在**同一条** PowerShell 命令里，用 `Popen` 起（不是 `run`，
否则等它睡完就没时间监听了）。这样 Python 这边崩了、被 Ctrl+C 了，设备照样回来。
再加一道：收尾回读节点状态，不是 `OK` 就大声喊人。

### ④ 一键入口必须退役（2026-09-29 审查报告第六节 7）

仓库根目录的 `run-0x1812-takeover.bat` 以前是个**一键管理员入口**：自己弹 UAC、
`taskkill` 掉桥程序、再跑 `--go`。而 ③ 之后我们知道这条路**被 Windows 封死**
（`DevNodeStatus` 里没有 `DN_DISABLEABLE`），它每次必然以 `DISABLE_FAIL` 收场。

⇒ 一个"要管理员 + 强杀桥程序 + 动系统设备状态"的入口，去回答一个**已经有答案**的
问题，只有风险没有收益。它现在是个**什么都不做的退役壳**（只打印"已退役 + 为什么"，
不提权、不 taskkill、不传 `--go`），文件留着只是给"记得有这个 bat"的人一个明确去处。

用法： python tools/check_takeover_guard.py
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _utf8 import setup as _setup_utf8  # noqa: E402
_setup_utf8()

import _bat  # noqa: E402  （读 .bat 的唯一入口，见 tools/_bat.py）

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(_ROOT, "tools", "takeover_hid_reports.py")
BAT = os.path.join(_ROOT, "run-0x1812-takeover.bat")


def _block(src: str, name: str) -> str:
    """取出 `NAME = r\"\"\"...\"\"\"` 的正文。"""
    m = re.search(name + r'\s*=\s*r?"""(.*?)"""', src, re.S)
    return m.group(1) if m else ""


def _code_only(block: str) -> str:
    """剥掉注释行和 say(...) 输出行，只留真正会执行的东西。

    ⚠ 不剥就会自己绊自己：只读分支里**打印给人看的说明文字**里就有
    `Disable-PnpDevice` 这个词（`say("  （`Disable-PnpDevice`，需要管理员）")`），
    直接 `in` 匹配会把它当成"分支里真的在禁用"。
    （`check_audio_watchdog.py` 的 `except queue.Full` 也栽在同一件事上。）
    """
    keep = []
    for ln in block.splitlines():
        s = ln.strip()
        if s.startswith("#") or s.startswith("say(") or s.startswith("say "):
            continue
        keep.append(ln)
    return "\n".join(keep)


def _ps_code(block: str) -> str:
    """剥掉 PowerShell 注释行（`#`）只留代码。

    ⚠ 同理：`PS_FIND` 的注释里就写着 `BTHLEDEVICE\\...\\9&XXXX&N&YYYY`
    作为"节点长什么样"的说明。不去注释，`"BTHLEDEVICE" in find_ps` 永远为真，
    反例（把过滤拿掉）就报不出红 —— 这道闸第一版正是这么自证的。
    """
    return "\n".join(ln for ln in block.splitlines()
                     if not ln.strip().startswith("#"))


def audit(src: str) -> list[tuple[bool, str]]:
    c: list[tuple[bool, str]] = []

    # ── ① 默认只读 ────────────────────────────────────────────────
    c.append(('"--go", action="store_true"' in src or
              ('"--go"' in src and "store_true" in src),
              "① --go 是开关（默认 False，不带就只体检）"))
    m_no = re.search(r"if not a\.go:(.*?)(?=\n    if not admin|\n    hold = )",
                     src, re.S)
    no_go = m_no.group(1) if m_no else ""
    c.append((bool(m_no) and "return 0" in no_go,
              "① 不带 --go 的分支自己 return 掉（不会往下走去动设备）"))
    c.append(("Disable-PnpDevice" not in _code_only(no_go),
              "① 那个分支里**没有**真去 Disable-PnpDevice 的代码（说明文字不算）"))
    i_no = src.find("if not a.go:")
    i_pop = src.find("subprocess.Popen(")
    c.append((0 <= i_no < i_pop if (i_no >= 0 and i_pop >= 0) else False,
              "① 「不 go」的判断在 Popen 之前"))

    # ── ② 目标必须是 BTHLEDEVICE ──────────────────────────────────
    find_ps = _ps_code(_block(src, "PS_FIND"))
    c.append((bool(find_ps), "② 取得到 PS_FIND"))
    c.append(("BTHLEDEVICE" in find_ps,
              "② PS_FIND 代码里明确只要 BTHLEDEVICE 父节点（注释里写着不算）"))
    # 反例形态：任何"挑一个节点"的行只要没限定 BTHLEDEVICE，就会禁到 COLxx 子集合
    sel_lines = [ln for ln in find_ps.splitlines() if "Select-Object -First 1" in ln]
    c.append((bool(sel_lines) and all("BTHLEDEVICE" in ln for ln in sel_lines),
              "② 挑目标那一行必须**同时**限定 BTHLEDEVICE"
              "（否则禁到 COLxx 空集合 → 收不到报告 → 错判「软件到头了」）"))
    c.append(("TARGETKIND=NONE" in find_ps,
              "② 找不到父节点时输出 TARGETKIND=NONE（Python 侧才能拦住）"))
    m_none = re.search(r'if kind == "NONE":(.*?)(?=\n    if not dev_id)', src, re.S)
    c.append((bool(m_none) and "return 3" in (m_none.group(1) if m_none else ""),
              "② Python 侧见到 NONE 就直接 return 3，不退而求其次"))

    # ── ③ 恢复有保证 ─────────────────────────────────────────────
    cyc = _ps_code(_block(src, "PS_CYCLE"))
    c.append((bool(cyc), "③ 取得到 PS_CYCLE"))
    i_dis = cyc.find("Disable-PnpDevice")
    i_sle = cyc.find("Start-Sleep")
    i_ena = cyc.find("Enable-PnpDevice")
    c.append((i_dis >= 0 and i_ena >= 0 and i_dis < i_ena,
              "③ 禁用＋启用都在同一条 PowerShell 命令里"))
    c.append((i_dis < i_sle < i_ena,
              "③ Start-Sleep 夹在中间（先禁用、睡够、再启用）"))
    c.append(("enable" not in cyc.lower() or "try" in cyc,
              "③ 启用包在 try 里"))
    c.append(("ENABLE_FAIL" in cyc,
              "③ 启用失败会输出 ENABLE_FAIL（不静默）"))
    c.append(("subprocess.Popen(" in src,
              "③ 用 Popen 起（用 run 会把监听时间睡掉）"))
    c.append(('"OK"' in src or "'OK'" in src,
              "③ 收尾回读节点状态，检查是不是 OK"))
    c.append(("没恢复成 OK" in src,
              "③ 没恢复成 OK 时会大声报出来（要求人工去设备管理器启用）"))

    # ── 收尾：报告要说清"这轮能不能下结论" ────────────────────────
    c.append(("没测成" in src and "不能" in src,
              "收尾区分「收到/没测成/订上了但 0 条」，没测成不许下结论"))
    return c


def _bat_code(text: str) -> str:
    """只留会**真的执行**的行（去掉 REM / :: 注释和 echo）。

    ⚠ 判据只看会执行的行：退役壳里"以前会提权 / 会 taskkill / 会 --go"
    全都是 `echo` 出来给人看的说明文字，拿整份文件判必然假红
    （和 `_code_only` / `_ps_code` 是同一个坑，第三次踩了）。
    """
    return "\n".join(ln for ln in text.splitlines()
                     if not re.match(r"^\s*(?:rem\b|::|echo\b)", ln, re.I))


def audit_bat(text: str) -> list[tuple[bool, str]]:
    """一键入口必须是个「什么都不做」的退役壳。"""
    code = _bat_code(text)
    return [
        ("已退役" in text and "退役" in text,
         "④ 一键入口写明「已退役」并说清为什么"),
        ("RunAs" not in code and "Start-Process" not in code,
         "④ 它**不再提权**（会执行的行里没有 Start-Process -Verb RunAs）"),
        ("taskkill" not in code,
         "④ 它**不再强杀桥程序**（会执行的行里没有 taskkill）"),
        ("--go" not in code,
         "④ 它**不再传 --go**（不会去动系统设备状态）"),
        (re.search(r"exit\s*/b\s+0", code, re.I) is not None,
         "④ 跑完是正常退出（不是「装完就静默结束」）"),
        ("takeover_hid_reports.py" in text,
         "④ 给出了历史取证的只读去处（别让人绕过它去直接跑 --go）"),
    ]


def main() -> int:
    src = open(SRC, encoding="utf-8").read()
    checks = audit(src)

    ok = True
    print("=" * 74)
    print(" 闸：接管 0x1812 —— 默认不动 / 目标对 / 恢复有保证 / 一键入口已退役")
    print("=" * 74)
    for good, name in checks:
        print(f"  {'✅' if good else '❌'} {name}")
        if not good:
            ok = False

    # ── ④ 一键入口已退役 ──────────────────────────────────────────
    # ⚠ 编码走 _bat.read：这个 bat 2026-09-29 从 UTF-8 转成了 GBK
    #   （配套 chcp 936，为的是绕开 cmd.exe 在 chcp 65001 下读批处理文件
    #   会按字节偏移错位的毛病）。写死 utf-8 会解出乱码、判据全变成
    #   "找不到那句话"，而报出来的结论会指向完全错误的地方。
    bat = _bat.read(BAT)
    bat_checks = audit_bat(bat)
    for good, name in bat_checks:
        print(f"  {'✅' if good else '❌'} {name}")
        if not good:
            ok = False

    # ── 反例自证：把目标改回「随便取第一个」必须报红 ──────────────
    print()
    print("── 反例（改坏后应当报红）──")
    n_fail = 0
    mutated = src.replace(
        "$d = $all | Where-Object { $_.InstanceId -like 'BTHLEDEVICE*' } | "
        "Select-Object -First 1",
        "$d = Get-PnpDevice -PresentOnly | "
        "Where-Object { $_.InstanceId -like '*00001812*' } | "
        "Select-Object -First 1")
    if mutated == src:
        print("  ❌ 反例没构造出来（锚点没找到）—— 这道闸的负向验证无效")
        ok = False
    else:
        bad = [g for g, _ in audit(mutated) if not g]
        if bad:
            n_fail += 1
            print(f"  ✅ 反例：改回老写法 `Get-PnpDevice | Select-Object -First 1`"
                  f"（＝禁到 COLxx 空集合，然后错判「软件到头了」）"
                  f" → 报了 {len(bad)} 项红")
        else:
            print("  ❌ 反例：改回老写法居然全绿 —— 这道闸没用")
            ok = False

    mutated2 = src.replace('if not a.go:', 'if False:')
    bad2 = [g for g, _ in audit(mutated2) if not g]
    if bad2:
        n_fail += 1
        print(f"  ✅ 反例：把「不带 --go 就只体检」去掉 → 报了 {len(bad2)} 项红")
    else:
        print("  ❌ 反例：去掉默认只读居然全绿 —— 这道闸没用")
        ok = False

    # 反例：把一键入口改回老样子（提权 + taskkill + --go）必须报红
    old_bat = (
        "@echo off\n"
        "chcp 65001 >nul\n"
        "title 0x1812 takeover test\n"
        'cd /d "%~dp0"\n'
        "net session >nul 2>&1\n"
        "if not errorlevel 1 goto elevated\n"
        'powershell -NoProfile -Command "Start-Process -FilePath \'%~f0\' -Verb RunAs"\n'
        "exit /b\n"
        ":elevated\n"
        "taskkill /F /IM RemoteVoiceBridge.exe >nul 2>&1\n"
        '"%PY%" "tools\\takeover_hid_reports.py" --go --seconds 60\n'
        "pause\n"
    )
    bad3 = [g for g, _ in audit_bat(old_bat) if not g]
    if bad3:
        n_fail += 1
        print(f"  ✅ 反例：一键入口改回「提权 + taskkill + --go」→ 报了 {len(bad3)} 项红")
    else:
        print("  ❌ 反例：老的一键入口居然全绿 —— ④ 那几条没用")
        ok = False

    if n_fail < 3:
        ok = False

    print()
    print("PASS" if ok else "FAIL —— 打 ❌ 的那几条会让「体检」动到你不想动的东西，"
                          "或者让结论错得看不出来")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
