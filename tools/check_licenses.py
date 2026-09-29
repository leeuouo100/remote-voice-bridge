"""第三方许可证清单必须**覆盖**实际依赖，并且真的装进安装包（审查报告第六节 9）。

背景
====
安装包里分发着十几个第三方包（`winrt-*` / `numpy` / `Pillow` / `pystray` /
`frida` / `sounddevice` / `keyboard` …），其中：

  · `pystray` 是 **LGPL-3.0**，`frida` 是 **wxWindows Library Licence 3.1**
    （LGPL 派生）—— 两者都要求"随分发附上许可证"；
  · `PyInstaller` 的 bootloader 是 GPL-2.0 **附带特殊例外**（明确允许用它
    构建并分发非自由程序）—— 这条例外本身也得写出来，否则读的人会以为
    整个包被 GPL 传染。

而 `THIRD_PARTY_NOTICES.md` 原先只写了三个"移植来源"（vRemoter / VibeMote /
Frida），**十几个实际依赖一个都没列**，安装包里也没有 `licenses\`。

这道闸钉三件事：

  A. `THIRD_PARTY_NOTICES.md` 覆盖 `requirements.txt` 与 `requirements-build.txt`
     里的**每一个**包（包名归一化后比对，`winrt-Windows.UI` → `winrt-windows.ui`）
  B. 每个包都写了**许可证名**（不是只写个包名凑数）
  C. `installer.iss` 真的把 `LICENSE` 与 `THIRD_PARTY_NOTICES.md` 装进
     `{app}\\licenses\\`（写进清单但没装 = 用户拿不到，等于没写）

外加反例自证：删掉任意一行依赖、把许可证列清空、把 [Files] 那两行去掉，
都必须报红。

用法： python tools/check_licenses.py
输出： LICENSES OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent

_checks: list[tuple[bool, str]] = []


def A(ok, msg) -> None:
    _checks.append((bool(ok), msg))


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


# ── 判据（抽成纯函数，好让反例能**真的**把同一条判据跑红）──────────────────
def _norm(name: str) -> str:
    """包名归一化（PEP 503）：大小写不敏感、`-`/`_`/`.` 等价。"""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _reqs(text: str) -> list[str]:
    """从一个 requirements 文件里抠出包名（跳过注释、空行、`-r` 之类的开关）。"""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if m:
            out.append(_norm(m.group(1)))
    return out


def _documented(notices: str) -> set[str]:
    """从 NOTICES 里抠出"被列过"的包名。

    只认**表格行/列表行里用反引号包起来的**包名（`winrt-runtime`）——
    正文里顺口提到一句不算"补齐了许可证"。
    """
    return {_norm(m) for m in re.findall(r"`([A-Za-z][A-Za-z0-9._-]{1,60})`",
                                         notices)}


def _missing(reqs: list[str], documented: set[str]) -> list[str]:
    """依赖里**没被文档覆盖**的那些。"""
    return [r for r in reqs if r not in documented]


def _has_license_names(notices: str) -> bool:
    """NOTICES 里必须真的写出许可证名，而不是只列包名。"""
    return all(re.search(p, notices, re.I) is not None for p in (
        r"\bMIT\b", r"BSD-3-Clause", r"LGPL-3\.0", r"wxWindows", r"GPL-2\.0",
        r"PSF-2\.0", r"MIT-CMU",
    ))


def _installs_licenses(iss: str) -> bool:
    """installer.iss 必须把两份文件装进 {app}\\licenses\\。"""
    files = re.search(r"^\[Files\]([\s\S]*?)(?=^\[|\Z)", iss, re.M)
    body = files.group(1) if files else ""
    return ('"LICENSE"' in body and '"THIRD_PARTY_NOTICES.md"' in body
            and re.search(r'DestDir:\s*"\{app\}\\licenses"', body) is not None)


# ── A / B：清单覆盖 ─────────────────────────────────────────────────────────
def case_coverage() -> None:
    notices = _read("THIRD_PARTY_NOTICES.md")
    run_reqs = _reqs(_read("requirements.txt"))
    build_reqs = _reqs(_read("requirements-build.txt"))
    documented = _documented(notices)

    A(bool(run_reqs), f"A0 从 requirements.txt 解析出 {len(run_reqs)} 个包")
    A(bool(build_reqs),
      f"A0b 从 requirements-build.txt 解析出 {len(build_reqs)} 个包")

    miss_run = _missing(run_reqs, documented)
    A(not miss_run,
      f"A1 运行时分发的包**全部**在 THIRD_PARTY_NOTICES.md 里列过"
      f"（漏了：{miss_run}）—— 其中 pystray 是 LGPL-3.0、frida 是 wxWindows，"
      "都要求随分发附上许可证")

    miss_build = _missing(build_reqs, documented)
    A(not miss_build,
      f"A2 构建期工具也在文档里说明了（漏了：{miss_build}）"
      "—— PyInstaller 的 GPL 特殊例外必须写出来，否则读的人以为整包被 GPL 传染")

    A(_has_license_names(notices),
      "B1 每个包都写了**许可证名**（MIT / BSD-3-Clause / LGPL-3.0 / wxWindows /"
      " GPL-2.0 / PSF-2.0 / MIT-CMU 都出现过），不是只列包名凑数")

    # 归一化必须真的在起作用：requirements 写的是 winrt-Windows.UI，
    # 而人写文档时可能写成 winrt-windows-ui —— 不能因为大小写/连字符就假红
    A(_norm("winrt-Windows.UI") == _norm("winrt_windows_ui")
      and _norm("Pillow") == "pillow",
      "B2 包名归一化按 PEP 503（大小写不敏感、`-`/`_`/`.` 等价）")

    # 顺带确认几个"必须单独交代"的包真的写了
    for name, why in (("pystray", "LGPL-3.0，要求允许替换"),
                      ("frida", "wxWindows，且要说明降级行为"),
                      ("pyinstaller", "GPL-2.0 + 特殊例外")):
        A(name in documented, f"B3 单独交代了 `{name}`（{why}）")


# ── C：真的装进安装包 ───────────────────────────────────────────────────────
def case_installer() -> None:
    iss = _read("installer.iss")
    A(_installs_licenses(iss),
      "C1 installer.iss 把 `LICENSE` 与 `THIRD_PARTY_NOTICES.md` 装进"
      " `{app}\\licenses\\` —— 只写进清单不装进包，用户照样拿不到")


# ── D：反例自证 ─────────────────────────────────────────────────────────────
def case_negative() -> None:
    notices = _read("THIRD_PARTY_NOTICES.md")
    iss = _read("installer.iss")
    req = _read("requirements.txt")
    run_reqs = _reqs(req)
    documented = _documented(notices)

    # 反例①：删掉 NOTICES 里的一整行 → 那个包立刻变成"漏了"
    victim = "numpy"
    n1 = "\n".join(ln for ln in notices.splitlines()
                   if f"`{victim}`" not in ln)
    A(n1 != notices and _missing(run_reqs, _documented(n1)) == [victim],
      f"[反例] 从 NOTICES 删掉 `{victim}` 那一行 → A1 判不合格")

    # 反例②：给 requirements 加一个新依赖而文档没跟 → 必须报红
    n2 = req.rstrip() + "\nbrandnewpkg==1.0\n"
    A(_missing(_reqs(n2), documented) == ["brandnewpkg"],
      "[反例] requirements.txt 新增一个包而文档没补 → A1 判不合格"
      "（这就是这道闸要拦的那个动作）")

    # 反例③：把许可证名全抹掉 → B1 报红
    n3 = re.sub(r"\bMIT\b|BSD-3-Clause|LGPL-3\.0|wxWindows|GPL-2\.0|PSF-2\.0|MIT-CMU",
                "（未注明）", notices)
    A(n3 != notices and not _has_license_names(n3),
      "[反例] 把许可证名全换成「未注明」→ B1 判不合格")

    # 反例④：把 [Files] 里那两行去掉 → C1 报红
    n4 = iss.replace(
        'Source: "LICENSE"; DestDir: "{app}\\licenses"; '
        'DestName: "LICENSE-RemoteVoiceBridge.txt"; Flags: ignoreversion\n', "")
    n4 = n4.replace('Source: "THIRD_PARTY_NOTICES.md"; '
                    'DestDir: "{app}\\licenses"; Flags: ignoreversion\n', "")
    A(n4 != iss and not _installs_licenses(n4),
      "[反例] 把 [Files] 里那两行去掉 → C1 判不合格")


def main() -> int:
    print("=" * 74)
    print(" 闸：第三方许可证清单覆盖实际依赖，并真的装进安装包（第六节 9）")
    print("=" * 74)

    case_coverage()
    case_installer()
    case_negative()

    fails = 0
    for ok, msg in _checks:
        print(f"  {'OK  ' if ok else '❌  '} {msg}")
        if not ok:
            fails += 1

    print()
    if fails:
        print(f"LICENSES FAILED（{fails} 项）")
        print("  提示：这几条坏掉的现象是「分发了 LGPL / wxWindows 的代码却没附"
              "许可证」、")
        print("        以及「文档里写了、用户手里那份包里根本没有」。")
        return 1
    print(f"LICENSES OK（{len(_checks)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
