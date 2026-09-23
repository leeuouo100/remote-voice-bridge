"""仓库根目录 `.bat` 的两条一致性闸 —— 两条都是 2026-09-23 真机上真踩过的。

**① 找 Python 的顺序必须完全一致**

真机实况：本机**没有** `.venv`，PATH 上那个 python（托管 3.13.12）**没装项目依赖**
（`bleak` / `winsdk` / `keyboard` 都没有），唯一装齐的是 WorkBuddy 托管环境里那个
`%USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\rvb\\Scripts\\python.exe`。

原来的样子是"每个 bat 各写各的"：`测遥控器按键通道.bat` 会去找 rvb 那个，
而 `diag-remote.bat` / `修复蓝牙配对.bat` 只写 `set PY=python`。
于是同一台机器上，一个工具能跑、另一个报"当前 Python 跑不起来" ——
**两件事看起来无关，其实在同一个用户路径上一前一后**（先跑修复、再跑复测）。
所以这里钉成一条：凡是**自己挑解释器**的 bat，候选列表**逐项、按序**相同。

**② 会打印中文的 `.bat` 必须自己声明 `chcp`**

`chcp 936` ⇒ 字节得能用 GBK 解；`chcp 65001` ⇒ 得能用 UTF-8 解。
**没写 chcp** 的时候，中文 Windows 的 cmd 默认 936，UTF-8 的中文 echo 会打成乱码。

两条边界都按实测收窄过，别当它是"越严越好"：
  · 只算**非注释行**（`REM` 里的中文不打印，谈不上乱码）；
  · 只算**自己挑解释器**的 bat（见下面的豁免名单）。

踩过一次坑：按"中文 Windows 一律 GBK"的经验，把一个 `chcp 65001` 的文件转成了 GBK，
框线字符（`╔═╗`）直接编不出来。**文件自己的 chcp 才是准的**，不要凭经验。

用法： python tools/check_bat_env.py  ／  --selftest（造反例验闸门自己会不会红）
"""
from __future__ import annotations

import glob
import os
import re
import sys

from _utf8 import setup as _setup_utf8  # 下面是中文输出，先钉住编码

_setup_utf8()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 唯一正确的候选顺序（从最可信到最兜底）。改这里 = 有意改全局约定。
PROBE_WANT = [
    r"%USERPROFILE%\.workbuddy\binaries\python\envs\rvb\Scripts\python.exe",
    r".venv\Scripts\python.exe",
]
FALLBACK_LINE = "if not defined PY set PY=python"
CHCP_CODEC = {"936": "gbk", "65001": "utf-8"}

# 明确豁免 —— **不是漏掉，是不该拿同一条规则管**。豁免也要打出来，免得变成黑洞。
EXEMPT = {
    "run.bat": "开发环境引导：它自己 `python -m venv .venv` 造 venv 再跑 main.py，"
               "本来就该用裸 python，不能逼它去用托管环境",
}

_CAND_RE = re.compile(r'if\s+exist\s+"([^"]+python\.exe)"', re.I)
_CHCP_RE = re.compile(r"chcp\s+(\d+)", re.I)
_SETPY_RE = re.compile(r'set\s+"?PY"?\s*=', re.I)
# 直接调某个 .py 的样子（`python x.py` / `pythonw.exe x.py`）
_CALL_PY_RE = re.compile(r"^\s*(?:python|pythonw|\S*pythonw?\.exe)\s+\S+\.py\b",
                         re.I | re.M)


def _rem(line: bytes) -> bool:
    s = line.strip().lower()
    return s.startswith(b"rem") or s.startswith(b"@rem")


def visible_nonascii_lines(raw: bytes) -> list[int]:
    """返回**会打印的行**里含非 ASCII 的行号（1 起）。

    在字节层面判，不先去解码 —— 否则"要先知道编码才能判断编码对不对"，
    绕成死循环。
    """
    out = []
    for i, ln in enumerate(raw.split(b"\n"), 1):
        if _rem(ln):
            continue
        if any(b > 127 for b in ln):
            out.append(i)
    return out


def parse(name: str, raw: bytes) -> dict:
    """把一份 .bat 的字节解析成可检查的事实（只描述，不判断）。"""
    head = raw.decode("latin-1")     # 只用它读 ASCII 结构（chcp / 路径 / 变量名）
    m = _CHCP_RE.search(head)
    chcp = m.group(1) if m else ""
    codec = CHCP_CODEC.get(chcp)
    decodable: bool | None = None
    if codec:
        try:
            raw.decode(codec)
            decodable = True
        except UnicodeDecodeError:
            decodable = False
    picks_py = bool(_SETPY_RE.search(head)) or bool(_CALL_PY_RE.search(head))
    return {
        "name": name,
        "chcp": chcp,
        "codec": codec,
        "decodable": decodable,
        "cands": [c.strip() for c in _CAND_RE.findall(head)],
        "has_fallback": FALLBACK_LINE.lower() in head.lower(),
        "picks_py": picks_py and name not in EXEMPT,
        "vis_lines": visible_nonascii_lines(raw),
    }


def inspect(files: dict[str, bytes]) -> tuple[list[tuple[bool, str]], list[str]]:
    """纯函数：给 {文件名: 字节}，返回 (逐项检查, 失败项文案)。

    抽成"吃 dict"而不是"自己扫目录"，是为了让反例走**完全同一条**代码路径 ——
    否则反例验的是另一套逻辑，等于没验。
    """
    checks: list[tuple[bool, str]] = []
    parsed = [parse(n, b) for n, b in sorted(files.items())]
    pickers = [p for p in parsed if p["picks_py"]]

    for n, why in sorted(EXEMPT.items()):
        if n in files:
            checks.append((True, f"{n}：**豁免**（{why}）"))

    if not pickers:
        checks.append((False, "一个「自己挑解释器」的 .bat 都没找到 —— 判据写错了吧"))
        return checks, [m for ok, m in checks if not ok]

    # ── ① 找 Python 的顺序，所有 bat 逐项一致 ───────────────────────
    want = [w.lower() for w in PROBE_WANT]
    for p in pickers:
        got = [g.lower() for g in p["cands"]]
        if got == want:
            msg = f"{p['name']}：Python 候选顺序与约定一致（{len(got)} 项）"
            ok = True
        elif sorted(got) == sorted(want):
            msg = (f"{p['name']}：候选**集合**对了但**顺序**不同 —— "
                   f"实测 {p['cands']}，约定 {PROBE_WANT}（顺序即优先级，不许乱）")
            ok = False
        else:
            missing = [w for w in PROBE_WANT if w.lower() not in got]
            msg = (f"{p['name']}：Python 候选与约定不符，实测 {p['cands']}；"
                   f"缺 {missing or '（无，但多了别的）'}")
            ok = False
        checks.append((ok, msg))

    for p in pickers:
        checks.append((p["has_fallback"],
                       f"{p['name']}：有最后兜底 `{FALLBACK_LINE}`"))

    # ── ② 有中文打印行 ⇒ 必须自己声明 chcp，且字节要对得上 ───────────
    for p in parsed:
        if not p["vis_lines"]:
            checks.append((True, f"{p['name']}：没有会打印的非 ASCII 行，编码无所谓"))
        elif not p["chcp"]:
            checks.append((False,
                           f"{p['name']}：第 {p['vis_lines'][:3]} 行会打印非 ASCII"
                           f"（共 {len(p['vis_lines'])} 行）却**没声明 chcp** —— "
                           f"中文 Windows 的 cmd 默认 936，UTF-8 的中文会打成乱码"))
        elif p["codec"] is None:
            checks.append((False,
                           f"{p['name']}：chcp {p['chcp']} 不在已知表里，无法判断编码"))
        else:
            checks.append((bool(p["decodable"]),
                           f"{p['name']}：chcp {p['chcp']} ⇒ 用 {p['codec']} 解码"
                           f"（{'通过' if p['decodable'] else '失败：文件字节和它自己的 chcp 打架'}）"))

    return checks, [m for ok, m in checks if not ok]


# ── 造反例用的假 .bat ───────────────────────────────────────────────
def _fake(probe: bool = True, swapped: bool = False, chcp: str = "65001",
          codec: str = "utf-8", body: str = "echo  ╔══╗ 中文") -> bytes:
    a, b = PROBE_WANT
    if swapped:
        a, b = b, a
    lines = ["@echo off", f"chcp {chcp} >nul" if chcp else "@rem 故意不写 chcp"]
    if probe:
        lines += [f'if exist "{a}" set "PY={a}"', f'if exist "{b}" set PY={b}',
                  FALLBACK_LINE]
    else:
        # ⚠ 兜底那行**要留着**：反例只该动"有没有去探测 rvb"这一个变量。
        #   头一版把兜底也一起删了，于是同时红 2 项 —— 条件没隔离，
        #   验出来的东西就不是我想验的那个了。
        lines += ["set PY=python", FALLBACK_LINE]
    lines += [body, "%PY% tools\\diag_remote.py"]
    return ("\r\n".join(lines) + "\r\n").encode(codec)


def selftest() -> int:
    """反例：把每一类错误造一遍，断言检查**必须**红。"""
    fails: list[str] = []

    def case(label: str, files: dict[str, bytes], want_bad: int) -> None:
        _c, bad = inspect(files)
        ok = len(bad) == want_bad
        print(f"  {'OK  ' if ok else 'FAIL'} {label}（期望 {want_bad} 项红，实际 {len(bad)}）")
        if not ok:
            fails.append(label)
            for m in bad:
                print(f"        · {m}")

    good = {n: _fake() for n in ("a.bat", "b.bat", "c.bat")}
    case("三份全合规 → 0 红", good, 0)

    bad1 = dict(good)
    bad1["b.bat"] = _fake(probe=False)
    case("有一份不探测 rvb 托管路径 → 必须红", bad1, 1)

    bad2 = dict(good)
    bad2["c.bat"] = _fake(swapped=True)
    case("候选顺序被调换 → 必须红", bad2, 1)

    bad3 = dict(good)
    bad3["b.bat"] = _fake(codec="gbk")
    case("chcp 65001 但字节是 GBK → 必须红", bad3, 1)

    bad4 = dict(good)
    bad4["b.bat"] = _fake(chcp="", codec="utf-8")
    case("有中文打印行却没写 chcp → 必须红", bad4, 1)

    ok5 = dict(good)
    ok5["b.bat"] = _fake(chcp="936", codec="gbk")
    case("chcp 936 + GBK 字节（合法）→ 0 红", ok5, 0)

    # 反例 6：中文只在 REM 里 ⇒ **不许**红（否则就是假红，会逼人乱改文件）
    ok6 = dict(good)
    ok6["b.bat"] = _fake(chcp="", codec="utf-8", body="REM  只有注释里有中文，不打印")
    case("非 ASCII 只在注释里 → 0 红（不许假红）", ok6, 0)

    if fails:
        print(f"\n  SELFTEST FAILED（{len(fails)} 项）")
        return 1
    print("\n  SELFTEST PASS")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        print("check_bat_env 自检 —— 闸门自己不许永远亮绿灯")
        return selftest()

    files = {os.path.basename(p): open(p, "rb").read()
             for p in sorted(glob.glob(os.path.join(ROOT, "*.bat")))}
    if not files:
        print("  FAIL 仓库根目录一个 .bat 都没有 —— 路径错了吧")
        return 1

    checks, bad = inspect(files)
    for ok, msg in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {msg}")

    print(f"\n  扫描到 {len(files)} 个 .bat：{', '.join(sorted(files))}")
    if bad:
        print(f"BAT ENV CHECK FAILED（{len(bad)} 项）")
        print("  提示：这两个坑都会让「用户双击一下」直接失败，")
        print("        而失败的往往正是他此刻最需要的那一步。")
        return 1
    print("BAT ENV OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
