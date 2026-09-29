"""CI 工作流与「项目声称的闸门」必须一致（2026-09-29 审查报告 P1-10）。

老工作流 `build-installer.yml` 的五个洞
=======================================
  ① 只在 tag / `workflow_dispatch` 触发 ⇒ **PR 根本不跑**，回归要到发版那一刻才发现；
  ② 闸门是「手动挑十来道列一遍」⇒ 和本地 `check_all` 两份清单各自漂，
     「本地绿、CI 不跑」的东西发出去没人验；
  ③ 构建与发布挤在同一个 job ⇒ 发布用的包是**又构建了一次**的，
     和「验过的那个包」不是同一个文件（PyInstaller 产物每次都不完全一样）；
  ④ 没写 `draft` ⇒ 靠 Action 的默认值。v1.0.21 事实上就是**直接公开**发布的，
     而 README 声称默认 Draft；
  ⑤ `permissions: contents: write` 全程给着，连只读的检查步骤也有写权限。

外加一条机制上的洞：`smoke_console.py` 之类对任意 `OSError` 都以 SKIP/成功退出
⇒ 环境一坏，整道闸静默变成**永远绿的摆设**。

本闸钉这些（每条都配反例）：

  A. 工作流文件本身：唯一一份、PR/main/tag 都触发、只跑 `check_all.py --ci`、
     build 依赖 checks、release 依赖 build 且**只下载 artifact 不重新构建**、
     `draft: true` 显式、权限最小化
  B. `check_all.py --ci` 的**行为**（拿假 STEPS + 假 `_run_step` 真跑 `main()`）：
     硬件闸被摘掉、必需项 SKIP 算失败、非必需项 SKIP 算 SKIP、缺工具在 CI 算失败、
     GITHUB_STEP_SUMMARY 里真的有结构化计数
  C. 反例自证：把上面每一条判据**真的**跑红一次
  D. 供应链（P2-7）：`uses:` 全部钉 40 位 commit SHA + 行尾版本注释、
     依赖装**带哈希的锁文件**、有 SBOM、有构建证明、
     Authenticode 签名是"有条件 + 没证书时明确说未签名"（不许静默跳过）

用法： python tools/check_ci_workflow.py
输出： CI WORKFLOW OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
from pathlib import Path

from _utf8 import setup as _setup_utf8

import _bat

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_checks: list[tuple[bool, str]] = []


def A(ok, msg) -> None:
    _checks.append((bool(ok), msg))


def _read(p: Path) -> str:
    # `.bat` 的编码不固定（见 tools/_bat.py）—— `run.bat` 2026-09-29 从
    # UTF-8 转成了 GBK。这里只对 .bat 走 _bat.read，别的文件仍是 UTF-8。
    if p.suffix.lower() == ".bat":
        return _bat.read(p)
    return p.read_text(encoding="utf-8")


WF = ROOT / ".github" / "workflows" / "ci.yml"


# ── 判据（抽成纯函数，好让反例能**真的**把同一条判据跑红）──────────────────
def _job(text: str, name: str) -> str:
    """切出一个 job 的文本（从 `  <name>:` 到下一个同缩进的 job 头为止）。

    ⚠ 不能拿 `.*?` 一路非贪婪到文件尾 —— 那样 release job 里会带上
    后面所有内容，`pyinstaller not in rel` 这类断言就永远假红。
    """
    m = re.search(rf"^  {re.escape(name)}:\s*$([\s\S]*?)(?=^  \S|\Z)", text, re.M)
    return m.group(0) if m else ""


def _only_one_workflow(names: list[str]) -> bool:
    """workflows/ 下只能有一份 —— 两份都「构建 + 发布」会互相打架。"""
    return names == ["ci.yml"]


def _triggers_ok(text: str) -> bool:
    return (re.search(r"^  pull_request:", text, re.M) is not None
            and re.search(r"^  push:", text, re.M) is not None
            and re.search(r"^\s*branches:\s*\[main\]", text, re.M) is not None
            and re.search(r"^\s*tags:\s*\['v\*'\]", text, re.M) is not None)


def _checks_uses_aggregator(checks_txt: str, whole: str) -> bool:
    """checks job 必须跑 `check_all.py --ci`，且**不许**手工再列别的 check_*。"""
    return ("tools/check_all.py --ci" in checks_txt
            and "check_injection" not in whole
            and "playwright install chromium" in checks_txt)


def _build_needs_checks(build_txt: str) -> bool:
    return re.search(r"^    needs:\s*checks\s*$", build_txt, re.M) is not None


def _release_consumes_artifact(rel_txt: str) -> bool:
    """release 只消费 artifact：不许出现任何构建动作。"""
    low = rel_txt.casefold()
    return (re.search(r"^    needs:\s*build\s*$", rel_txt, re.M) is not None
            and "download-artifact" in low
            and "pyinstaller" not in low
            and "iscc" not in low
            and "setup-python" not in low)


def _release_is_draft(rel_txt: str) -> bool:
    """**显式**草稿 —— 不许依赖 Action 的默认值。"""
    return (re.search(r"^\s*draft:\s*true\s*$", rel_txt, re.M) is not None
            and "action-gh-release" in rel_txt)


# ── D. 供应链：Action 钉 SHA / 依赖锁哈希 / SBOM / 构建证明 / 签名（P2-7）────

def _unpinned_uses(text: str) -> list[str]:
    """返回**没有**钉 40 位 commit SHA 的 `uses:` 值。

    `@v4` 是**可变引用** —— 上游把 v4 指到一个新 commit，我们下次构建
    跑的就是没审过的新代码，而这件事在 diff 里**看不见**（本仓库一个
    字节都没变）。这是供应链攻击最常见的那条路。
    """
    return [m.group(1) for m in re.finditer(r"^\s*-?\s*uses:\s*(\S+)", text, re.M)
            if not re.match(r"^[^@\s]+@[0-9a-f]{40}$", m.group(1))]


def _uses_have_version_comment(text: str) -> bool:
    """每个 `uses:` 行尾都要有版本注释 —— 不然没人知道那个 SHA 是哪一版。"""
    lines = [l for l in text.splitlines()
             if re.match(r"^\s*-?\s*uses:", l)]
    return bool(lines) and all(re.search(r"#\s*v?\d+\.\d+", l) for l in lines)


def _uses_hash_lock(text: str) -> bool:
    """装依赖必须走**带哈希的锁文件**，不能直接 `-r requirements.txt`。

    requirements.txt 里传递依赖没锁版本 ⇒ 同一份代码在不同时间构建出的
    产物不一样，而按键旁路依赖的 frida 原生扩展一变就是静默故障。
    """
    return ("pip install --require-hashes -r requirements.lock.txt" in text
            and "pip install -r requirements.txt" not in text)


def _has_sbom(text: str) -> bool:
    """要有 SBOM 生成 + 校验 + 上传（三件缺一不可）。"""
    low = text.casefold()
    return ("cyclonedx" in low and "sbom.cdx.json" in text
            and "json.load" in text              # 生成后真解析一遍，别传个语法都不对的
            and "upload-artifact" in low)


def _has_provenance(build_txt: str) -> bool:
    """构建证明：Action + 权限 + subject 三件都要。"""
    return ("attest-build-provenance" in build_txt
            and "id-token: write" in build_txt
            and "attestations: write" in build_txt
            and "subject-path" in build_txt)


def _signing_is_honest(text: str) -> bool:
    """签名必须「有条件执行 + 没有证书时明确说未签名」。

    ⚠ 最坏的做法是**静默跳过**：下载页上写着"已签名"、实际没签，
      而用户那边的 SmartScreen 提示与"签名坏了"看起来一模一样。

    ⚠ "明确说未签名"必须落在**写进 job 摘要的那一行**上 —— 第一版只在整份
      文件里搜「未签名」，结果**文件头的说明注释**里也有这个词，
      于是"把摘要那行改成含糊的『已跳过』"照样判合格（反例当场报绿）。
      判据得盯着**真的会被看到的那一行**，不能盯全文。
    """
    notice_on_summary_line = any(
        "GITHUB_STEP_SUMMARY" in ln and "未签名" in ln
        for ln in text.splitlines())
    return ("if: steps.sign.outputs.available == 'true'" in text
            and "SIGN_PFX_B64" in text
            and "::warning::" in text
            and notice_on_summary_line)


def _perms_minimal(whole: str, readonly_txt: str, rel_txt: str) -> bool:
    """默认只读；只有 release job 提权。

    ⚠ 行尾**可以带注释**（`contents: write          # 只有这一步需要写权限`），
    所以 `\\s*$` 后面要允许 `# …`。第一版没写这一层，A7 当场假红。
    """
    return (re.search(r"^permissions:\s*\n\s*contents:\s*read\s*(?:#.*)?$",
                      whole, re.M) is not None
            and re.search(r"^\s*contents:\s*write\s*(?:#.*)?$",
                          rel_txt, re.M) is not None
            and re.search(r"^\s*contents:\s*write\s*(?:#.*)?$",
                          readonly_txt, re.M) is None)


# ── E. 支持范围声明不许漂（第六节 8）────────────────────────────────────────
def _py_tuple(text: str, name: str) -> tuple[int, int] | None:
    m = re.search(rf"^{name}\s*=\s*\((\d+)\s*,\s*(\d+)\)", text, re.M)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _ci_python_version(yml: str) -> tuple[int, int] | None:
    m = re.search(r"python-version:\s*['\"](\d+)\.(\d+)['\"]", yml)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _support_range_consistent(cfg_txt: str, yml: str, req_txt: str,
                              bat_txt: str, readme_txt: str) -> bool:
    """「声明的支持范围」= CI 实跑的那个版本 = run.bat 的判定区间 = 文档里写的。

    这条治的是：README 说"支持 3.10+"、CI 只验 3.11、requirements 里钉着
    一个 3.13 装不上的 numpy —— 三处各说各话，用户拿 3.13 去撞才发现。
    """
    lo = _py_tuple(cfg_txt, "PY_MIN")
    hi = _py_tuple(cfg_txt, "PY_MAX")
    ci = _ci_python_version(yml)
    if not (lo and hi and ci):
        return False
    if not (lo <= ci <= hi):                 # CI 验的版本必须落在声明区间内
        return False
    squashed = bat_txt.replace(" ", "")      # run.bat 里的区间必须**逐字**一致
    if f"({lo[0]},{lo[1]})" not in squashed or f"({hi[0]},{hi[1]})" not in squashed:
        return False
    text = f"{lo[0]}.{lo[1]} / {hi[0]}.{hi[1]}"
    return text in req_txt and text in readme_txt


# ── A. 工作流文件 ────────────────────────────────────────────────────────────
def case_workflow() -> None:
    names = sorted(p.name for p in WF.parent.glob("*.y*ml"))
    A(_only_one_workflow(names),
      f"A1 workflows/ 下只有 ci.yml（实际 {names}）"
      "—— 两份都「构建 + 发布」会互相打架")
    A(WF.is_file(), "A1b ci.yml 存在")

    yml = _read(WF)
    checks, build, rel = _job(yml, "checks"), _job(yml, "build"), _job(yml, "release")
    A(bool(checks) and bool(build) and bool(rel),
      "A1c 三个 job 都切得出来（checks / build / release）")

    A(_triggers_ok(yml),
      "A2 PR、main push、tag 都触发（老工作流只在 tag/手动触发 ⇒ PR 不跑）")
    A(_checks_uses_aggregator(checks, yml),
      "A3 checks job 跑 check_all.py --ci（唯一一份闸门清单）"
      "，且装了 playwright chromium（必需项不许靠 SKIP 混过去）")
    A(_build_needs_checks(build),
      "A4 build 依赖 checks（闸门不全绿就不构建）")
    A(_release_consumes_artifact(rel),
      "A5 release 依赖 build，且**只下载 artifact、不重新构建**"
      "（否则发出去的包不是验过的那个文件）")
    A(_release_is_draft(rel),
      "A6 release **显式** draft: true（不依赖 Action 默认值 —— "
      "v1.0.21 就是因为没写而直接公开了）")
    A(_perms_minimal(yml, checks + build, rel),
      "A7 默认 contents: read，只有 release job 提权到 write")

    # ── E. 支持范围声明不许漂（第六节 8）───────────────────────────────────
    cfg = _read(ROOT / "config.py")
    req = _read(ROOT / "requirements.txt")
    bat = _read(ROOT / "run.bat")
    rdm = _read(ROOT / "README.md")
    A(_support_range_consistent(cfg, yml, req, bat, rdm),
      "A8 「声明的支持范围」(config.PY_MIN/PY_MAX) = CI 实跑版本 = run.bat 的"
      "判定区间 = requirements.txt 与 README 里写的（三处各说各话的话，"
      "用户拿 3.13 去撞，而失败原因和「跑不起来」看起来毫无关系）")

    # ── D. 供应链（P2-7）───────────────────────────────────────────────────
    unpinned = _unpinned_uses(yml)
    A(not unpinned,
      f"D1 每个 `uses:` 都钉了 40 位 commit SHA（没钉的：{unpinned}）"
      "—— `@v4` 是可变引用：上游一指，我们下次构建跑的就是没审过的新代码，"
      "而 diff 里看不见（本仓库一个字节都没变）")
    A(_uses_have_version_comment(yml),
      "D1b 每个 `uses:` 行尾都有版本注释（否则没人知道那个 SHA 是哪一版，"
      "升级时只能靠猜）")
    A(_uses_hash_lock(yml),
      "D2 装依赖走带 SHA-256 的锁文件（`--require-hashes -r requirements.lock.txt`）"
      "—— 传递依赖不锁 = 同一份代码可能构建出不同产物")
    A(_has_sbom(yml),
      "D3 有 SBOM：CycloneDX 生成 + 真解析一遍（语法都不对的 SBOM 比没有更糟）"
      "+ 随 artifact 上传")
    A(_has_provenance(build),
      "D4 有构建证明（attest-build-provenance + id-token/attestations 权限 + subject-path）"
      "—— 这是「下载页上那个 exe 到底是不是你构建的」唯一能自证的东西")
    A(_signing_is_honest(yml),
      "D5 Authenticode 签名：有条件执行（有证书才签），且**没有证书时明确打印未签名**"
      "—— 静默跳过会让「以为签了」变成默认认知")


def case_workflow_negative() -> None:
    """反例：把每条判据真的跑红一次（用的是同一批判据函数）。"""
    yml = _read(WF)
    checks, build, rel = _job(yml, "checks"), _job(yml, "build"), _job(yml, "release")

    A(not _only_one_workflow(["build-installer.yml", "ci.yml"]),
      "[反例] 多出一份工作流 → A1 判不合格")

    b1 = yml.replace("  pull_request:", "  # pull_request:")
    A(_triggers_ok(yml) and not _triggers_ok(b1),
      "[反例] 去掉 PR 触发 → A2 判不合格")

    b2 = yml.replace("python tools/check_all.py --ci",
                     "python tools/check_keymap.py")
    A(_checks_uses_aggregator(checks, yml)
      and not _checks_uses_aggregator(_job(b2, "checks"), b2),
      "[反例] checks 改回手工列单道闸 → A3 判不合格")

    b3 = yml.replace("tools/check_all.py --ci",
                     "tools/check_injection.py")
    A(_checks_uses_aggregator(checks, yml)
      and not _checks_uses_aggregator(_job(b3, "checks"), b3),
      "[反例] 把硬件闸（check_injection）手工塞进工作流 → A3 判不合格")

    b4 = yml.replace("    needs: checks", "    # needs: checks")
    A(_build_needs_checks(build) and not _build_needs_checks(_job(b4, "build")),
      "[反例] build 不再依赖 checks → A4 判不合格")

    b5 = rel.replace("actions/download-artifact@v4", "actions/checkout@v4") + \
        "\n      - run: pyinstaller remote-voice-bridge.spec --noconfirm\n"
    A(_release_consumes_artifact(rel) and not _release_consumes_artifact(b5),
      "[反例] release 里又构建一次（不下载 artifact）→ A5 判不合格")

    b6 = rel.replace("draft: true", "draft: false")
    A(_release_is_draft(rel) and not _release_is_draft(b6),
      "[反例] draft: false（直接公开发布）→ A6 判不合格")

    b7 = yml.replace("permissions:\n  contents: read",
                     "permissions:\n  contents: write")
    A(_perms_minimal(yml, checks + build, rel)
      and not _perms_minimal(b7, _job(b7, "checks") + _job(b7, "build"),
                             _job(b7, "release")),
      "[反例] 默认给写权限 → A7 判不合格")

    # A8 的四条反例：每一处"各说各话"都必须能被抓出来
    cfg = _read(ROOT / "config.py")
    req = _read(ROOT / "requirements.txt")
    bat = _read(ROOT / "run.bat")
    rdm = _read(ROOT / "README.md")
    A(_support_range_consistent(cfg, yml, req, bat, rdm),
      "[反例·基线] 现状是自洽的（下面四条反例才有意义）")

    b8 = yml.replace("python-version: '3.11'", "python-version: '3.13'")
    A(b8 != yml and not _support_range_consistent(cfg, b8, req, bat, rdm),
      "[反例] CI 改跑 3.13（声明范围外）→ A8 判不合格")

    b9 = cfg.replace("PY_MAX = (3, 11)", "PY_MAX = (3, 13)")
    A(b9 != cfg and not _support_range_consistent(b9, yml, req, bat, rdm),
      "[反例] 只把 config 的声明放宽到 3.13（CI / bat / 文档没跟）→ A8 判不合格")

    b10 = bat.replace("(3,11)", "(3,12)")
    A(b10 != bat and not _support_range_consistent(cfg, yml, req, b10, rdm),
      "[反例] run.bat 的判定区间与 config 不一致 → A8 判不合格")

    b11 = rdm.replace("3.10 / 3.11", "3.10 及以上")
    A(b11 != rdm and not _support_range_consistent(cfg, yml, req, bat, b11),
      "[反例] README 改口说「3.10 及以上」→ A8 判不合格")

    # ── D 组的反例（P2-7）──────────────────────────────────────────────────
    A(not _unpinned_uses(yml) and _unpinned_uses(
        yml.replace("@11d5960a326750d5838078e36cf38b85af677262", "@v4")),
      "[反例] 把 checkout 退回 `@v4`（可变引用）→ D1 判不合格")

    b12 = yml.replace("   # v4.4.0\n", "\n")
    A(_uses_have_version_comment(yml) and not _uses_have_version_comment(b12),
      "[反例] 删掉 uses 行尾的版本注释 → D1b 判不合格")

    b13 = yml.replace("pip install --require-hashes -r requirements.lock.txt",
                      "pip install -r requirements.txt")
    A(_uses_hash_lock(yml) and not _uses_hash_lock(b13),
      "[反例] 装依赖退回 requirements.txt（传递依赖不锁）→ D2 判不合格")

    b14 = yml.replace("sbom.cdx.json", "x.json").replace("cyclonedx", "nothing")
    A(_has_sbom(yml) and not _has_sbom(b14),
      "[反例] 去掉 SBOM 生成 → D3 判不合格")

    b15 = build.replace("      id-token: write\n", "")
    A(_has_provenance(build) and not _has_provenance(b15),
      "[反例] 去掉 id-token 权限（构建证明签不出来）→ D4 判不合格")

    b16 = yml.replace("if: steps.sign.outputs.available == 'true'", "if: true")
    A(_signing_is_honest(yml) and not _signing_is_honest(b16),
      "[反例] 让签名步骤无条件执行（没证书时直接挂）→ D5 判不合格")

    b17 = yml.replace("**未签名** —— 没有配置 Secret SIGN_PFX_B64。", "已跳过。")
    A(_signing_is_honest(yml) and not _signing_is_honest(b17),
      "[反例] 把「未签名」改成含糊的「已跳过」（＝静默跳过）→ D5 判不合格")


# ── B. check_all.py --ci 的**行为** ─────────────────────────────────────────
def _load_check_all():
    """把 tools/check_all.py 当模块加载（它有 __main__ 保护，import 不跑主流程）。"""
    p = ROOT / "tools" / "check_all.py"
    spec = importlib.util.spec_from_file_location("check_all_probe", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_main(mod, argv, run_stub) -> tuple[int, str, list[list[str]]]:
    """跑一遍 `mod.main(argv)`，把 stdout 和「真的执行了哪些命令」一起收回来。"""
    calls: list[list[str]] = []

    def fake(cmd):
        calls.append(list(cmd))
        return run_stub(cmd)

    orig_steps, orig_run = mod.STEPS, mod._run_step
    mod.STEPS, mod._run_step = mod.STEPS, fake
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = mod.main(argv)
    finally:
        mod.STEPS, mod._run_step = orig_steps, orig_run
    return code, buf.getvalue(), calls


def _stub(cmd):
    """假 `_run_step`。

    ⚠ 第一个元素必须是**布尔**。第一版这里返回 `0`（想表达"退出码 0"），
    而 `0` 是 falsy ⇒ `_verdict` 把它当 FAIL，B2c/B4/B5/B6b 一起假红。
    """
    tag = cmd[0]
    if tag == "HW":
        return True, "本机没有硬件"
    if tag == "SKIPME":
        return True, "SKIPPED（假装环境不支持）"
    return True, "全部通过"


_FAKE_STEPS = [
    ("硬件闸（--ci 应摘掉）", ["HW"], {"hardware": True, "why": "需要真机"}),
    ("会 SKIP 的必需闸", ["SKIPME"], {}),
    ("正常闸", ["OKME"], {}),
]


def case_ci_behavior() -> None:
    mod = _load_check_all()
    real_steps = list(mod.STEPS)          # 下面会反复改 mod.STEPS，最后要还回去
    try:
        # B1 真机闸确实被标了 hardware，且只有它（清单里就这一道需要交互桌面）
        hw = [s[0] for s in real_steps
              if (s[2] if len(s) > 2 else {}).get("hardware")]
        A(hw == ["按键注入自检"],
          f"B1 需要真机/人工的闸只有「按键注入自检」（实际 {hw}）")

        # B2 --ci：硬件闸被摘掉、必需项 SKIP 算失败
        mod.STEPS = list(_FAKE_STEPS)
        code, out, calls = _run_main(mod, ["--ci"], _stub)
        A(code == 1, "B2 --ci 里「必需项 SKIP」把整轮判失败（退出码 1）")
        A("[FAIL] 会 SKIP 的必需闸" in out,
          "B2b SKIP 的必需项打的是 FAIL（不是 OK、也不是静默跳过）")
        A("[PASS] 正常闸" in out, "B2c 正常闸照常 PASS")
        A(["HW"] not in calls and "[--  ]" in out,
          "B2d --ci 真的**没执行**硬件闸，并在输出里点名（没跑 ≠ 过了）")

        # B3 本地全量：硬件闸照跑
        code2, out2, calls2 = _run_main(mod, [], _stub)
        A(["HW"] in calls2, "B3 本地全量（不带 --ci）照跑硬件闸")
        A(code2 == 1, "B3b 本地也一样：必需项 SKIP 算失败")

        # B4 非必需项 SKIP → 算 SKIP、不算失败
        mod.STEPS = [("可选闸", ["SKIPME"], {"required": False})]
        code3, out3, _ = _run_main(mod, ["--ci"], _stub)
        A(code3 == 0 and "[SKIP]" in out3,
          "B4 required=False 的闸 SKIP 时算 SKIP，不拖垮整轮")

        # B5 反例：把「SKIP 视为失败」这条规则摘掉 → 同一批 STEPS 必须变绿
        #     （证明 B2 的退出码 1 真是这条规则起的作用，不是别的什么）
        mod.STEPS = list(_FAKE_STEPS)
        orig_verdict = mod._verdict
        mod._verdict = lambda ok, out: "FAIL" if not ok else "PASS"  # noqa: E731
        try:
            code4, _, _ = _run_main(mod, ["--ci"], _stub)
        finally:
            mod._verdict = orig_verdict
        A(code4 == 0,
          "B5 [反例] 摘掉「SKIP 视为失败」后整轮变绿 —— 证明 B2 抓的正是这条规则")

        # B6 缺工具（没有 node）在 --ci 里算失败，在本地只算「没跑」
        mod._UNRUN = [("缺工具的闸", "本机没有 node")]
        mod.STEPS = [("正常闸", ["OKME"], {})]
        try:
            code5, out5, _ = _run_main(mod, ["--ci"], _stub)
            code6, out6, _ = _run_main(mod, [], _stub)
            A(code5 == 1 and "[FAIL] 缺工具的闸" in out5,
              "B6 --ci 里缺工具算失败（不许悄悄降级成跳过）")
            A(code6 == 0 and "[--  ] 缺工具的闸" in out6,
              "B6b 本地只报「没跑」，不当失败（用户机器上本来就可能没 node）")
        finally:
            mod._UNRUN = []

        # B7 结构化结果真的写进 GITHUB_STEP_SUMMARY
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "summary.md"
            os.environ["GITHUB_STEP_SUMMARY"] = str(sf)
            mod.STEPS = list(_FAKE_STEPS)
            try:
                _run_main(mod, ["--ci"], _stub)
            finally:
                os.environ.pop("GITHUB_STEP_SUMMARY", None)
            txt = sf.read_text(encoding="utf-8") if sf.is_file() else ""
        A("| PASS |" in txt and "| FAIL |" in txt and "| SKIP |" in txt,
          "B7 GITHUB_STEP_SUMMARY 里有结构化的 PASS/FAIL/SKIP 计数")
        A("本模式未运行的闸" in txt,
          "B7b 摘要里也写清「本模式未运行的闸」")
    finally:
        mod.STEPS = real_steps


def case_verdict_unit() -> None:
    """`_verdict` 的小单测：几种真实输出形态都得认出来。"""
    mod = _load_check_all()
    v = mod._verdict
    A(v(True, "SMOKE OK") == "PASS", "B8 正常输出 → PASS")
    A(v(False, "FAIL（3 项）") == "FAIL", "B8b 非零退出 → FAIL")
    A(v(True, "SKIPPED（非 Windows）") == "SKIP",
      "B8c 行首 SKIPPED → SKIP")
    A(v(True, "  ⚠ SKIPPED（import main 失败：X）") == "SKIP",
      "B8d 带符号前缀的 SKIPPED → SKIP（check_send_after_voice 就是这么打的）")
    A(v(True, "  （本机没枚举到蓝牙适配器，跳过实例 ID 真机抽查）") == "PASS",
      "B8e 只是**子项**跳过（用的是「跳过」不是 SKIPPED）→ 仍算 PASS"
      "，不能把局部降级读成整道闸没跑")


def main() -> int:
    print("=" * 74)
    print(" 闸：CI 与「声称的闸门」一致（P1-10）")
    print("=" * 74)

    case_workflow()
    case_workflow_negative()
    case_ci_behavior()
    case_verdict_unit()

    fails = 0
    for ok, msg in _checks:
        print(f"  {'OK  ' if ok else '❌  '} {msg}")
        if not ok:
            fails += 1

    print()
    if fails:
        print(f"CI WORKFLOW FAILED（{fails} 项）")
        print("  提示：这几条坏掉的现象是「PR 不跑闸门」、「发出去的包没验过」、")
        print("        以及「环境一坏，闸门静默变成永远绿的摆设」。")
        return 1
    print(f"CI WORKFLOW OK（{len(_checks)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
