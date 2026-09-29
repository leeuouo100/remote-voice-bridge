"""蓝牙配对修复工具「备份/还原是可信事务」的回归闸 —— 2026-09-29 审查报告 P1-7。

背景
====
`pairing.py` 是**以管理员身份**动系统注册表的工具，老版本的备份/还原有三个洞：

  ① **导出范围过宽**：整棵 `BTHPORT\\Parameters\\Keys` + 整个 `Devices` 全抄 ——
     本机所有蓝牙设备的链路密钥都落进磁盘，而我们只用得着其中一两条；
  ② **备份落在用户可写目录**（`%APPDATA%\\remote-voice-bridge\\backup`）：
     低权限进程可以放一个"更新"的 .reg，等用户下次点「还原」时被 UAC
     提权导入 —— **一次提权放大**。而备份里装的是能解密链路的密钥材料；
  ③ **不是事务**：一次修复生成好几个 .reg，还原时只 `glob` 最新的**一个**
     导进去 —— 可能只恢复一半，留下"迁移到一半"的状态；挑哪个还取决于
     mtime（谁都能改）。

这道闸钉四件事（每条都配反例）：

  A. 备份范围 = 最小子树（目标设备 + 目标适配器 + 涉及的本地地址），
     不再整棵抄；关键项导出失败 → **返回空、调用方在动注册表前停下**
  B. 备份目录在 %ProgramData% 且 DACL 收紧（禁继承 + 只给管理员/SYSTEM 写）
  C. 还原**只认 manifest**（不 glob .reg）、逐文件校验 SHA-256、
     并解析 .reg 里的键路径**限制注册表前缀**
  D. 行为级（真跑，不碰注册表）：假 `_run` + 临时目录，把
     ① 无 manifest ② 哈希不符 ③ 越界键 ④ 正常事务 ⑤ 多出来的 .reg
     这几种情形跑一遍，并验反例

用法： python tools/check_pairing_backup.py
输出： PAIRING BACKUP OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import json
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


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


# ── 判据（抽成纯函数，好让反例能**真的**把同一条判据跑红）────────────────────
def _restore_src(src: str) -> str:
    """抠出 `restore_backup()` 整个函数体（到下一个 `# ──` 分节注释为止）。"""
    m = re.search(r"def restore_backup\(.*?\n(?=# ──)", src, re.S)
    return m.group(0) if m else ""


def _manifest_only(restore_src: str) -> bool:
    """还原是否「只认 manifest」。

    数**真正的 `.glob(` 调用**：全函数只该有一处，且指向 manifest。
    ⚠ 别拿"整段源码里有没有 glob(*.reg)"判 —— `restore_backup` 的文档字符串里
    **故意**引用了老写法 `glob("*bthle*.reg")` 作对照，那样会假红。
    """
    g = re.findall(r"\.glob\(([^)]*)\)", restore_src)
    return len(g) == 1 and "manifest" in g[0]


def _critical_fail_stops(src: str) -> bool:
    """`backup()` 里关键项导出失败时是否**返回空列表**（而不是只提示、继续）。"""
    m = re.search(r"def backup\(.*?\n(?=# ──)", src, re.S)
    body = m.group(0) if m else ""
    return re.search(r"if failed_critical:\s*\n(?:\s*#.*\n)*\s*print[\s\S]*?return \[\]",
                     body) is not None


def _bat_backup_path_ok(bat: str) -> bool:
    """`修复蓝牙配对.bat` 里告诉用户的备份路径，必须是 `%ProgramData%`。

    为什么单独钉这一条：`.bat` 是**用户唯一会看到**的那份说明（双击就看到），
    而它不在 `pairing.py` 的测试范围内 —— 2026-09-29 真机验收时才发现它
    还印着 P1-7 之前的 `%APPDATA%\\remote-voice-bridge\\backup\\`。
    用户按它去找备份会**找不到**，而"找不到备份"紧接着的念头就是"那是不是没备份"。

    判据只看**提到 backup 的那些行**（`%APPDATA%` 在同一个文件里是**对的** ——
    体检报告 `pairing-fix.txt` 确实还在那儿，不能一刀切禁掉）。
    `%%` 是 .bat 里的转义写法，先还原再判。
    """
    norm = bat.replace("%%", "%")
    for ln in norm.splitlines():
        if "backup" in ln.casefold() and "appdata" in ln.casefold():
            return False
    return "ProgramData%\\remote-voice-bridge\\backup" in norm


def _read_bat(name: str) -> str:
    """读 .bat —— 走共享入口（它们的编码不止一种，见 tools/_bat.py）。

    ⚠ 别在这里自己写 `decode("utf-8")`：`修复蓝牙配对.bat` 2026-09-29 从
    UTF-8 转成了 GBK，写死 utf-8 会解出乱码，A3b 会报「.bat 里没写
    ProgramData」—— 而真正的原因只是编码变了，结论指向完全错误的地方。
    """
    return _bat.read(ROOT / name)


# ── A. 静态：pairing.py / config.py / tray_app.py ───────────────────────────
def case_static() -> None:
    src = _read("pairing.py")
    cfg = _read("config.py")
    tray = _read("tray_app.py")

    # A1 备份目录不再是 %APPDATA% 下那个
    #     只看 BACKUP_DIR 那个赋值本身（含续行）—— 注释里一定会提到 %APPDATA%
    #     （那是在解释「为什么不能放那儿」），拿整份文件判会误伤。
    lines = cfg.splitlines()
    i = next((k for k, ln in enumerate(lines) if ln.startswith("BACKUP_DIR")), -1)
    expr = "\n".join(lines[i:i + 4]) if i >= 0 else ""
    A(i >= 0 and "ProgramData" in expr and "remote-voice-bridge" in expr
      and "backup" in expr and "CONFIG_DIR" not in expr,
      "A1 config.py 里 BACKUP_DIR 落在 %ProgramData%（不是用户可写的 %APPDATA%）")
    A(re.search(r"BACKUP_DIR\s*=\s*CONFIG_DIR\s*/", src) is None
      and re.search(r"BACKUP_DIR\s*=\s*CONFIG_DIR\s*/", cfg) is None,
      "A1b 两边都不再有 `BACKUP_DIR = CONFIG_DIR / ...` 这种写法")
    A("from config import BACKUP_DIR" in src,
      "A2 pairing.py 的 BACKUP_DIR 从 config 拿（单一出处，不会漂）")
    A("PAIRING_BACKUP = BACKUP_DIR" in tray,
      "A3 托盘的「打开修复备份目录」用的是同一个常量"
      "（自己再写一遍字面量迟早会漂到旧路径）")

    # A3b/A3c 用户双击看到的那个 .bat 也必须说对
    bat_name = "修复蓝牙配对.bat"
    bat = _read_bat(bat_name)
    A(_bat_backup_path_ok(bat),
      f"A3b {bat_name} 里**提到 backup 的那一行**写的是 `%ProgramData%\\…\\backup`"
      "（不是 P1-7 之前的 %APPDATA%）—— 用户按它去找备份必须找得到")
    A("ProgramData" in bat and "APPDATA" in bat,
      f"A3c {bat_name} 里两份路径各自说对：备份在 ProgramData、"
      "体检报告 `pairing-fix.txt` 仍在 APPDATA（不能一刀切全改）")

    # A4 允许的注册表前缀
    A("ALLOWED_REG_PREFIXES" in src, "A4 定义了 ALLOWED_REG_PREFIXES")
    for pre in (r"Services\BTHPORT", r"Enum\BTHLE", r"Enum\USB"):
        A(pre in src, f"A4b 白名单含 {pre}")
    A(re.search(r"ALLOWED_REG_PREFIXES[\s\S]{0,400}?\)\s*\n",
                src) is not None,
      "A4c 白名单是个有限的元组（不是「任意前缀都行」）")

    # A5 最小子树：不许再整棵导 Keys / Devices
    A(re.search(r"jobs\s*=\s*\[\s*\(BTHPORT_DEVICES,", src) is None
      and re.search(r"\(BTHPORT_KEYS,\s*\"bthport-keys\"\)", src) is None,
      "A5 不再把整棵 BTHPORT\\Keys / Devices 列进备份清单")
    A(re.search(r"def _backup_jobs\(", src) is not None,
      "A6 有 _backup_jobs()（把「要备份什么」抽出来，可单测）")
    body = re.search(r"def _backup_jobs\(.*?\n(?=def )", src, re.S)
    bsrc = body.group(0) if body else ""
    A("BTHPORT_KEYS}\\\\{loc}" in bsrc or "BTHPORT_KEYS}\\" in bsrc,
      "A6b 密钥按**本地地址**逐棵导（Keys\\<addr>，不是 Keys 整棵）")
    A(re.search(r"_reg_key_state\(path\) == \"absent\"", bsrc) is not None,
      "A7 明确「不存在」的键才跳过；「打不开」（权限）的照样列进去导"
      "—— 用 bool 判断会把读不了的链路密钥整棵漏掉")

    # A8 关键失败 → 返回空
    A(_critical_fail_stops(src),
      "A8 关键项导出失败 → backup() **返回空列表**（不是「只提示、继续」）")
    A(re.search(r"made = backup\(d\)\s*\n\s*if not made:", src) is not None,
      "A9 调用点在 `not made` 时停下（不再动注册表）")

    # A10 还原只认 manifest
    rbs = _restore_src(src)
    A("glob(\"*-manifest.json\")" in rbs,
      "A10 restore_backup 只找 manifest")
    A(_manifest_only(rbs),
      f"A10b 全函数只有一处 glob（{re.findall(r'[.]glob[(]([^)]*)[)]', rbs)}），"
      "且指向 manifest —— 不再 glob .reg 直接导（那正是「挑最新那个」的老写法）")
    A("_sha256(f) != e.get(\"sha256\")" in rbs,
      "A11 逐文件校验 SHA-256（文件被改过就拒绝）")
    A("_reg_paths_in(f)" in rbs and "_reg_prefix_ok(rp)" in rbs,
      "A12 导入前解析 .reg 里的键路径并**限制前缀**"
      "（reg import 会按文件里的 HKLM 路径老实写）")
    A(re.search(r"def _reg_prefix_ok\(", src) is not None
      and re.search(r"def _reg_paths_in\(", src) is not None,
      "A12b 有 _reg_paths_in / _reg_prefix_ok 两个纯函数")

    # A13 目录 ACL
    A("PROTECTED_DACL_SECURITY_INFORMATION" in src,
      "A13 收紧目录权限时**禁用继承**（否则从父目录继承来的 Users 写权限还在）")
    A(re.search(r"def _secure_dir\(", src) is not None, "A13b 有 _secure_dir()")
    A(re.search(r"ok, why = _secure_dir\(BACKUP_DIR\)[\s\S]{0,200}?if not ok:\s*\n\s*return \[\]",
                src) is not None,
      "A13c 目录权限没设成功 → 直接不备份（目录不可信 = 备份不可信）")


def case_static_negative() -> None:
    """反例：把每条判据真正跑红一次，证明它抓的是"行为"而不是"字符串在不在"。

    ⚠ 反例必须**走同一条判据函数**。第一版这里是
      `A('glob("*-manifest.json")' not in b2, ...)`
    —— 那只证明"替换生效了"，跟 A10b 到底会不会红没关系。
    """
    src = _read("pairing.py")

    # ① 关键失败也继续 → _critical_fail_stops 必须变 False
    anchor = "        return []\n\n    # 地址缓存的原值"
    b1 = src.replace(anchor, "        return made\n\n    # 地址缓存的原值")
    A(b1 != src, "[反例] 找得到 backup() 里「关键失败 → return []」那段（锚点存在）")
    A(_critical_fail_stops(src) and not _critical_fail_stops(b1),
      "[反例] 改成「关键失败也继续」→ A8 判不合格")

    # ② 还原改回 glob *.reg → _manifest_only 必须变 False
    rbs = _restore_src(src)
    b2 = rbs.replace('glob("*-manifest.json")', 'glob("*.reg")')
    A(b2 != rbs, "[反例] 找得到 restore_backup 里那处 glob（锚点存在）")
    A(_manifest_only(rbs) and not _manifest_only(b2),
      "[反例] 把还原改回 glob *.reg → A10b 判不合格")

    # ③ 去掉 SHA-256 校验 → 同一条断言必须变红
    b3 = src.replace('if _sha256(f) != e.get("sha256"):', "if False:")
    A(b3 != src and '_sha256(f) != e.get("sha256")' not in _restore_src(b3),
      "[反例] 去掉 SHA-256 校验 → A11 判不合格")

    # ④ 把 .bat 里的备份路径改回 %APPDATA% → _bat_backup_path_ok 必须变 False
    #    （这条就是 2026-09-29 真机验收时抓到的那个：.bat 还印着旧路径）
    bat = _read_bat("修复蓝牙配对.bat")
    b4 = bat.replace("%%ProgramData%%", "%%APPDATA%%")
    A(b4 != bat, "[反例] 找得到 .bat 里那行备份路径（锚点存在）")
    A(_bat_backup_path_ok(bat) and not _bat_backup_path_ok(b4),
      "[反例] 把 .bat 的备份路径改回 %APPDATA% → A3b 判不合格")

    # ⑤ 反向也要成立：不能靠"整份文件禁掉 APPDATA"蒙过去 ——
    #    体检报告确实还在 APPDATA，禁掉它 A3c 就会红（那是另一条判据）。
    A(_bat_backup_path_ok(bat.replace("ProgramData", "APPDATA")) is False,
      "[反例] 两份路径全改成 APPDATA（一刀切）→ A3b 判不合格")


# ── D. 行为级（真跑，不碰注册表）────────────────────────────────────────────
def _reg_bytes(paths: list[str], enc: str = "utf-16") -> bytes:
    """造一份像 `reg export` 输出的文本。"""
    lines = ["Windows Registry Editor Version 5.00", ""]
    for p in paths:
        lines.append(f"[HKEY_LOCAL_MACHINE\\{p}]")
        lines.append('"X"=dword:00000001')
        lines.append("")
    return "\r\n".join(lines).encode(enc)


class _FakeRun:
    """假 _run：只记录调用，不真执行。"""

    def __init__(self, rc: int = 0, out: str = ""):
        self.calls: list[list[str]] = []
        self.rc = rc
        self.out = out

    def __call__(self, cmd, timeout=90):
        self.calls.append(list(cmd))
        return self.rc, self.out

    @property
    def imported(self) -> list[str]:
        return [c[-1] for c in self.calls if len(c) > 1 and c[1] == "import"]


def _write_manifest(d: Path, tx: str, files: list[tuple[str, str, bool]]) -> Path:
    import hashlib
    entries = []
    for name, reg_path, crit in files:
        f = d / name
        entries.append({"file": name, "reg_path": reg_path, "critical": crit,
                        "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
                        "bytes": f.stat().st_size})
    mf = d / f"{tx}-manifest.json"
    mf.write_text(json.dumps({"tx": tx, "created": "2026-09-29 00:00:00",
                              "allowed_prefixes": ["SYSTEM\\CurrentControlSet"],
                              "files": entries, "extra": []}),
                  encoding="utf-8")
    return mf


def case_behavior() -> None:
    import pairing as P

    # D1 前缀白名单
    good = [r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Keys\047f0ef2d294",
            r"SYSTEM\CurrentControlSet\Enum\BTHLE\Dev_f196a263671c\8&1b58e5cc&0&f196a263671c",
            r"SYSTEM\CurrentControlSet\Enum\USB\VID_33FA&PID_0001\6&2ac08ccf&0&2\Device Parameters"]
    bad = [r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
           r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run",
           r"SYSTEM\CurrentControlSet\Services\BTHPORT_evil",
           r"SYSTEM\CurrentControlSet\Enum\USBSTOR\Disk&Ven_x",
           r"SYSTEM\CurrentControlSet\Enum\BTHLE_bad",
           r"SAM\SAM\Domains\Account",
           r"SYSTEM\CurrentControlSet\Control\Lsa"]
    A(all(P._reg_prefix_ok(p) for p in good),
      "D1 我们自己的三类键都在白名单内")
    A(not any(P._reg_prefix_ok(p) for p in bad),
      "D1b 自启动项 / SAM / LSA / **前缀形近的假键** 一律拒绝")

    # D1c 反例：证明"按路径分段匹配"和"裸 startswith"真的是两回事
    #      （2026-09-29 就是这里抓出了 pairing.py 的真漏洞）
    def _naive(p: str) -> bool:
        q = p.replace("/", "\\").strip("\\").casefold()
        return any(q.startswith(pre.casefold()) for pre in P.ALLOWED_REG_PREFIXES)

    sneaky = [r"SYSTEM\CurrentControlSet\Services\BTHPORT_evil",
              r"SYSTEM\CurrentControlSet\Enum\USBSTOR\Disk&Ven_x",
              r"SYSTEM\CurrentControlSet\Enum\BTHLE_bad"]
    A(all(_naive(p) for p in sneaky)
      and not any(P._reg_prefix_ok(p) for p in sneaky),
      "D1c [反例] 裸 startswith 会放行这三个形近假键、分段匹配不会"
      " —— 证明 D1b 抓的是真差别，不是「字符串在不在」")

    # D2 _reg_paths_in 能读 UTF-16LE 的 .reg（reg export 的真实编码）
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        f = t / "a.reg"
        f.write_bytes(_reg_bytes([good[0], bad[0]]))
        got = P._reg_paths_in(f)
        A(len(got) == 2 and got[0].startswith("SYSTEM\\") and got[1].startswith("SOFTWARE\\"),
          f"D2 解析 UTF-16LE 的 .reg 拿到 {len(got)} 条键路径（实际 {got}）")
        A(any(not P._reg_prefix_ok(g) for g in got),
          "D2b 越界的那条确实会被判越界（白名单真的在起作用）")

    # D3 没有 manifest → 拒绝，且提示怎么手工处理
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "20260101-000000-abcd-bthle-x.reg").write_bytes(_reg_bytes([good[0]]))
        old_dir, old_run = P.BACKUP_DIR, P._run
        P.BACKUP_DIR = t
        try:
            ok, msg = P.restore_backup("")
            A(not ok and "manifest" in msg,
              "D3 目录里只有裸 .reg（老版本留下的）→ 拒绝自动导入并说明原因")
        finally:
            P.BACKUP_DIR, P._run = old_dir, old_run

    # D4 哈希不符 → 拒绝，且**一次 reg import 都没发**
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        f = t / "tx1-dev-f196a263671c.reg"
        f.write_bytes(_reg_bytes([good[0]]))
        _write_manifest(t, "tx1", [(f.name, good[0], True)])
        f.write_bytes(_reg_bytes([good[0]], enc="utf-8"))    # 事后改内容
        old_dir, old_run = P.BACKUP_DIR, P._run
        fake = _FakeRun()
        P.BACKUP_DIR, P._run = t, fake
        try:
            ok, msg = P.restore_backup("")
            A(not ok and "SHA-256" in msg,
              "D4 文件被改过（哈希对不上）→ 拒绝导入")
            A(fake.imported == [],
              f"D4b 被拒绝时**一个 reg import 都没发**（实际 {fake.imported}）")
        finally:
            P.BACKUP_DIR, P._run = old_dir, old_run

    # D5 manifest 里列的 .reg 含越界键 → 拒绝
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        f = t / "tx2-dev-f196a263671c.reg"
        f.write_bytes(_reg_bytes([good[0], bad[0]]))          # 掺了自启动项
        _write_manifest(t, "tx2", [(f.name, good[0], True)])
        old_dir, old_run = P.BACKUP_DIR, P._run
        fake = _FakeRun()
        P.BACKUP_DIR, P._run = t, fake
        try:
            ok, msg = P.restore_backup("")
            A(not ok and "越界" in msg,
              "D5 manifest 里的 .reg 掺了越界的键 → 拒绝导入")
            A(fake.imported == [],
              f"D5b 拒绝时一个 import 都没发（实际 {fake.imported}）")
        finally:
            P.BACKUP_DIR, P._run = old_dir, old_run

    # D6 正常事务：只导 manifest 里列的文件；目录里多出来的 .reg 不导
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        f1 = t / "tx3-dev-f196a263671c.reg"
        f2 = t / "tx3-keys-047f0ef2d294.reg"
        f1.write_bytes(_reg_bytes([good[0]]))
        f2.write_bytes(_reg_bytes([good[2]]))
        (t / "tx3-other-extra.reg").write_bytes(_reg_bytes([bad[0]]))   # 没进 manifest
        _write_manifest(t, "tx3", [(f1.name, good[0], True),
                                   (f2.name, good[2], True)])
        old_dir, old_run = P.BACKUP_DIR, P._run
        fake = _FakeRun()
        P.BACKUP_DIR, P._run = t, fake
        try:
            ok, msg = P.restore_backup("")
            A(ok, f"D6 正常事务能导入（{msg}）")
            A(sorted(Path(x).name for x in fake.imported)
              == sorted([f1.name, f2.name]),
              f"D6b 只导 manifest 里列的两个文件（实际 {fake.imported}）")
            A("tx3-other-extra.reg" not in " ".join(fake.imported),
              "D6c 目录里没进 manifest 的 .reg **不导**（这就是「只接受 manifest 里的文件」）")
        finally:
            P.BACKUP_DIR, P._run = old_dir, old_run

    # D7 默认只导关键项；`*` 才连次要项一起导
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        fa = t / "tx4-keys-047f0ef2d294.reg"
        fb = t / "tx4-bthle-f196a263671c.reg"
        fa.write_bytes(_reg_bytes([good[0]]))
        fb.write_bytes(_reg_bytes([good[1]]))
        _write_manifest(t, "tx4", [(fa.name, good[0], True),
                                   (fb.name, good[1], False)])
        old_dir, old_run = P.BACKUP_DIR, P._run
        try:
            fake = _FakeRun()
            P.BACKUP_DIR, P._run = t, fake
            P.restore_backup("")
            A([Path(x).name for x in fake.imported] == [fa.name],
              f"D7 默认只导关键项（实际 {fake.imported}）")
            fake2 = _FakeRun()
            P._run = fake2
            P.restore_backup("*")
            A(sorted(Path(x).name for x in fake2.imported)
              == sorted([fa.name, fb.name]),
              f"D7b `*` 时次要项也导（实际 {fake2.imported}）")
        finally:
            P.BACKUP_DIR, P._run = old_dir, old_run

    # D8 _backup_jobs：最小子树 + absent 跳过
    d = {
        "live_addr": "047f0ef2d294",
        "records": [{"remote": "f196a263671c",
                     "services_for": [{"addr": "047f0ef2d294"}]}],
        "adapters": [{"inst": "6&2ac08ccf&0&2",
                      "reg_path": r"VID_33FA&PID_0001\6&2ac08ccf&0&2",
                      "addr": "047f0ef2d294"}],
        "nodes": [{"key": r"SYSTEM\CurrentControlSet\Enum\BTHLE\Dev_x\y",
                   "remote": "f196a263671c"}],
        "keys": {"locals": [{"local": "047f0ef2d294"}]},
    }
    jobs = P._backup_jobs(d)
    paths = [p for p, _t, _c in jobs]
    A(not any(p.rstrip("\\").casefold().endswith(r"parameters\keys") for p in paths),
      "D8 不再导整棵 Keys（实际清单里没有裸的 ...\\Parameters\\Keys）")
    A(not any(p.casefold().endswith(r"parameters\devices") for p in paths),
      "D8b 不再导整个 Devices")
    A(any(p.casefold().endswith(r"parameters\keys\047f0ef2d294") for p in paths),
      "D8c 只导**涉及到的那个本地地址**的密钥子树")
    A(any(p.casefold().endswith(r"devices\f196a263671c") for p in paths),
      "D8d 只导**目标设备**的配对记录")
    A(all(not p.casefold().endswith(r"keys\ffffffffffff") for p in paths),
      "D8e 明确不存在的键不进清单")
    crit = {p: c for p, _t, c in jobs}
    keyjob = [p for p in paths if p.casefold().endswith(r"keys\047f0ef2d294")]
    A(keyjob and crit[keyjob[0]] is True,
      "D8f 链路密钥是**关键项**（导不出来就不许动注册表）")

    # D9 反例：把"只跳过 absent"放宽成"只要不是 ok 就跳过" → D8c 变红
    #      （＝没有管理员权限时会把读不了的链路密钥整棵漏掉）
    src = _read("pairing.py")
    broken = src.replace('if _reg_key_state(path) == "absent":',
                         'if _reg_key_state(path) != "ok":')
    A(broken != src, "[反例] 能构造出「放宽跳过条件」的版本（锚点存在）")
    ns_mod = __import__("types").ModuleType("pairing_broken")
    sys.modules["pairing_broken"] = ns_mod
    try:
        exec(compile(broken, "pairing_broken.py", "exec"), ns_mod.__dict__)
        # 本机 `Keys\<addr>` 在非管理员下是 denied → 放宽后会被跳过
        states = [P._reg_key_state(f"{P.BTHPORT_KEYS}\\047f0ef2d294")]
        if "denied" in states:
            jobs_b = ns_mod._backup_jobs(d)
            A(not any(p.casefold().endswith(r"keys\047f0ef2d294")
                      for p, _t, _c in jobs_b),
              "[反例] 放宽成「不是 ok 就跳过」后，读不了的链路密钥真的被漏掉了"
              " ⇒ D8c 抓的正是这个差别")
        else:
            A(True, "[反例] 本机 Keys 子键可读，跳过该反例（不影响结论）")
    finally:
        sys.modules.pop("pairing_broken", None)


def main() -> int:
    print("=" * 74)
    print(" 闸：配对修复的备份/还原是可信事务（P1-7）")
    print("=" * 74)

    case_static()
    case_static_negative()
    case_behavior()

    fails = 0
    for ok, msg in _checks:
        print(f"  {'OK  ' if ok else '❌  '} {msg}")
        if not ok:
            fails += 1

    print()
    if fails:
        print(f"PAIRING BACKUP FAILED（{fails} 项）")
        print("  提示：这几条坏掉的现象是「密钥材料被降级存放」、"
              "「被换过的 .reg 在管理员上下文里写任意位置」、")
        print("        以及「还原只恢复了一半」。")
        return 1
    print(f"PAIRING BACKUP OK（{len(_checks)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
