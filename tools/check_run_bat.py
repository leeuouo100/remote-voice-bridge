"""入口 `run.bat` 的三条（2026-09-29 审查报告第六节 1~3）。

`run.bat` 是源码用户唯一会双击的东西，它的三处错误都**不报错**，
只是"就是不行"：

  ① 入口是 `main.py` 而不是 `tray_app.py` —— 没有托盘、也没有「退出」这个
     正常收尾入口。用户只能关控制台窗口，那是硬杀进程：GattSession、
     Frida 注入、音频流全都不会被清理，现象是「退出再开就连不上」。
     （P1-3/P1-4 把优雅收尾做进代码了，但走 main.py 根本到不了那条路。）
  ② 框里写「系统默认麦克风已设为 CABLE Input」—— **反了**。
     VB-CABLE 两个端点命名是反的：`CABLE Input` 是**播放**端（本程序往里写），
     `CABLE Output` 才是**录音**端（系统默认麦克风 / 输入法要读的那只）。
     按错的设，输入法读的是"没有任何东西在写的那个端点" ⇒ 一点声音都没有。
  ③ pip 的输出被 `>nul 2>&1` 整个吞掉、也不看退出码 —— 依赖装不上时脚本照样
     往下跑，最后崩在一个和"依赖没装"毫无关系的 ImportError 上，用户完全
     无从下手（而失败原因就在被丢掉的那段输出里）。

用法： python tools/check_run_bat.py
输出： RUN BAT OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from _utf8 import setup as _setup_utf8

import _bat

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
BAT = ROOT / "run.bat"

_checks: list[tuple[bool, str]] = []


def A(ok, msg) -> None:
    _checks.append((bool(ok), msg))


def _read() -> str:
    # ⚠ run.bat 的编码**不是**固定的：2026-09-29 从 UTF-8 转成了 GBK（配套
    #   `chcp 936`，为的是绕开 cmd.exe 在 `chcp 65001` 下读批处理文件会
    #   按字节偏移错位的毛病）。这里写死 utf-8 的话，判据会全部变成
    #   "找不到那个字符串"，报出「run.bat 没启动 tray_app.py」这种**指向完全
    #   错误**的结论。走 _bat.read 统一解。
    return _bat.read(BAT)


# ── 判据（抽成纯函数，好让反例能**真的**把同一条判据跑红）──────────────────
def _echo_text(text: str) -> str:
    """只取**打印给用户看**的行（`echo`）。

    ⚠ 注释里会引用"错的写法"作对照（本文件顶部就有一处），拿整份文件判必然假红
    —— 和 `check_pairing_backup.py` 的 A10b 是同一个坑。
    """
    return "\n".join(ln for ln in text.splitlines()
                     if re.match(r"^\s*echo\b", ln, re.I))


def _code_lines(text: str) -> list[str]:
    """会**真的执行**的行（去掉 REM / :: 注释）。"""
    return [ln for ln in text.splitlines()
            if not re.match(r"^\s*(?:rem\b|::)", ln, re.I)]


def _launches_tray(text: str) -> bool:
    """入口必须是托盘，且**不许**是 main.py / pythonw（要看得见日志）。"""
    calls = re.findall(r"^\s*(\S*pythonw?\.exe)\s+(\S+\.py)\b", text,
                       re.I | re.M)
    if not calls:
        return False
    exe, script = calls[-1]          # 最后一行才是真正启动的那一次
    return (script.casefold() == "tray_app.py"
            and "pythonw" not in exe.casefold()
            and re.search(r"^\s*\S*pythonw?\.exe\s+main\.py\b", text,
                          re.I | re.M) is None)


def _mic_endpoint_ok(text: str) -> bool:
    """给用户看的那行必须是 CABLE Output，且不能把 Input 说成麦克风。"""
    shown = _echo_text(text)
    return (re.search(r"默认麦克风[^\n]*CABLE\s+Output", shown) is not None
            and re.search(r"默认麦克风[^\n]*CABLE\s+Input", shown) is None)


def _pip_fails_loud(text: str) -> bool:
    """pip 的输出必须落盘（不许 `>nul`），失败必须停下并打印日志尾巴。"""
    line = next((ln for ln in _code_lines(text)
                 if re.search(r"pip\s+install", ln, re.I)), "")
    if not line or re.search(r">\s*nul", line, re.I):
        return False
    return (re.search(r"pip-install\.log", text) is not None
            and re.search(r"if\s+errorlevel\s+1\s*\(", text, re.I) is not None
            and re.search(r"exit\s*/b\s+1", text, re.I) is not None)


def case_static() -> None:
    text = _read()
    A(_launches_tray(text),
      "① 最后启动的是 `tray_app.py`（不是 main.py），且用 python.exe 而不是"
      " pythonw.exe —— 直接跑 main.py 没有托盘，用户只能关窗口硬杀进程")
    A(re.search(r"^\s*\S*pythonw?\.exe\s+main\.py\b", text, re.I | re.M) is None,
      "①b 文件里不再有「启动 main.py」这一路")
    A(_mic_endpoint_ok(text),
      "② 系统默认麦克风写的是 `CABLE Output`（录音端）—— 写成 Input 就变成"
      "读没人写的那只端点，症状是一点声音都没有")
    A("录音端" in text or "播放端" in text,
      "②b 顺带解释了 VB-CABLE 两个端点命名是反的（免得下一个人又改回去）")
    A(_pip_fails_loud(text),
      "③ pip 输出落盘到 pip-install.log、失败即 `exit /b 1` 并打印日志尾巴"
      "（以前 `>nul 2>&1` 全吞 + 不看退出码）")
    A(re.search(r"if\s+errorlevel\s+1\s*\(", text, re.I) is not None
      and re.search(r"pause", text, re.I) is not None,
      "③b 失败时 pause 住，用户看得到原因（双击运行的窗口一关就没了）")


def case_negative() -> None:
    """反例：把每条判据真的跑红一次（用的是同一批判据函数）。"""
    text = _read()

    b1 = text.replace("tray_app.py", "main.py")
    A(_launches_tray(text) and not _launches_tray(b1),
      "[反例] 入口改回 main.py → ① 判不合格")

    b1b = text.replace(".venv\\Scripts\\python.exe tray_app.py",
                       ".venv\\Scripts\\pythonw.exe tray_app.py")
    A(b1b != text and _launches_tray(text) and not _launches_tray(b1b),
      "[反例] 换成 pythonw.exe（看不到日志）→ ① 判不合格")

    b2 = text.replace("默认麦克风已设为 CABLE Output",
                      "默认麦克风已设为 CABLE Input")
    A(b2 != text and _mic_endpoint_ok(text) and not _mic_endpoint_ok(b2),
      "[反例] 麦克风端点写反成 CABLE Input → ② 判不合格")

    b3 = text.replace("> pip-install.log 2>&1", ">nul 2>&1")
    A(b3 != text and _pip_fails_loud(text) and not _pip_fails_loud(b3),
      "[反例] pip 输出又被 `>nul` 吞掉 → ③ 判不合格")

    b3b = text.replace("pause & exit /b 1", "echo [WARN] 继续")
    A(b3b != text and _pip_fails_loud(text) and not _pip_fails_loud(b3b),
      "[反例] 依赖装不上还继续往下跑 → ③ 判不合格")


def main() -> int:
    print("=" * 74)
    print(" 闸：入口 run.bat 的三条（审查报告第六节 1~3）")
    print("=" * 74)

    if not BAT.is_file():
        print("❌  run.bat 不在了")
        return 1

    case_static()
    case_negative()

    fails = 0
    for ok, msg in _checks:
        print(f"  {'OK  ' if ok else '❌  '} {msg}")
        if not ok:
            fails += 1

    print()
    if fails:
        print(f"RUN BAT FAILED（{fails} 项）")
        print("  提示：这几条坏掉的现象是「退出再开连不上」、「一点声音都没有」、")
        print("        以及「依赖没装上也照样往下跑」。")
        return 1
    print(f"RUN BAT OK（{len(_checks)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
