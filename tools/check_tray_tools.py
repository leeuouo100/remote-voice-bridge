"""托盘「修复 / 诊断」菜单的回归闸 —— 2026-09-29 武哥的建议。

背景
====
原话：「把修复蓝牙配对等各方面的修复工具，也合并到那个软件里面去吧。
右键菜单里不是有"重启""控制台""重新连接"这些选项嘛，你把修复功能也放到
这里面来，这样子好一点。」

这些修复工具以前只散落在开始菜单和仓库目录的 .bat 里，出问题时用户得自己找。
收进托盘右键菜单是对的，但**托盘菜单是最容易"静默失效"的地方**：

  · 菜单项指向的动作函数改名 / 删掉 → pystray 在**构建菜单时**才炸，
    而托盘是常驻进程，炸了用户只看到"右键没反应"；
  · 工具文件名写错（`修复蓝牙配对.bat` vs `pairing_fix.bat`）→ 菜单能点，
    但永远"找不到修复工具"，而且只有气泡提示、日志里什么都没有；
  · 传了个工具**不认**的开关 → 工具安静地当成"只读诊断"跑，
    用户以为修了、其实什么都没改；
  · 用了 `subprocess.run` 等工具跑完 → 托盘线程被**卡死**（菜单点不动、
    图标不刷新、连"退出"都点不了），而工具会弹 UAC + `pause`，
    等于永久卡住。

所以这道闸钉四件事（每条都配反例，证明它测得出来）：

  A. 菜单结构：`build_menu` 里真有「修复 / 诊断」子菜单，且 5 个菜单项齐全
  B. 动作函数真的存在（不是菜单里写了个不存在的名字）
  C. 工具名与 installer.iss / spec 一致（改名就红，不许静默失效）
  D. 传的开关是工具真的认的；且**不许**阻塞托盘线程
  E. 兜底顺序：bat → Diag.exe → python（安装版优先，源码版也能用）

用法： python tools/check_tray_tools.py
输出： TRAY TOOLS OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

REPO = Path(__file__).resolve().parent.parent
FAILS: list[str] = []
PASSES = 0

# 期望的菜单项标签（子菜单里必须一字不差地出现）。
# 顺序 = 排查顺序：先"查"再"修"，别把修排在查前面。
EXPECT_ITEMS = [
    "蓝牙配对体检（只读，不改动）",
    "蓝牙配对修复（换过 USB 口、连不上时跑这个）",
    "遥控器诊断（按键没反应时跑这个）",
    "打开配对体检报告",
    "打开修复备份目录",
]
# 每个菜单项标签 → 它必须绑到的动作函数名
EXPECT_ACTIONS = {
    "蓝牙配对体检（只读，不改动）": "_pairing_check",
    "蓝牙配对修复（换过 USB 口、连不上时跑这个）": "_fix_pairing",
    "遥控器诊断（按键没反应时跑这个）": "_diag_remote",
    "打开配对体检报告": "_open_pairing_report",
    "打开修复备份目录": "_open_backup_dir",
}


def check(cond, msg) -> bool:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
    return bool(cond)


def _fn_src(tree: ast.AST, src: str, name: str) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return None


# ── 可被复用的"检查器"（反例也用同一份，才说明它测得出来）────────────────────

def menu_has_items(menu_src: str | None, items: list[str]) -> list[str]:
    """返回**缺失**的菜单项标签。"""
    if not menu_src:
        return list(items)
    return [lab for lab in items if lab not in menu_src]


def menu_binds(menu_src: str | None, mapping: dict[str, str]) -> list[str]:
    """返回**绑定错了**的菜单项（标签在、但动作函数名不在同一段里）。"""
    if not menu_src:
        return list(mapping)
    return [lab for lab, act in mapping.items()
            if lab in menu_src and act not in menu_src]


def is_nonblocking(src: str | None) -> bool:
    """**不许有阻塞调用**。

    ⚠ `subprocess.run` / `os.system` 都会阻塞调用线程 —— 托盘线程一卡，
    菜单点不动、图标不刷新、连"退出"都点不了，而工具还会弹 UAC + pause，
    等于永久卡死。`os.startfile` 是 ShellExecute，非阻塞，所以允许。
    """
    if not src:
        return False
    if re.search(r"\bsubprocess\.run\s*\(", src):
        return False
    if re.search(r"\bos\.system\s*\(", src):
        return False
    return True


def spawns_window(src: str | None) -> bool:
    """真的会起一个新窗口（Popen 或 ShellExecute）。

    动作函数是**委托**给 helper 的（本身不 spawn），所以这条只对
    `_spawn` / `_run_pairing_tool` 这类真正落地的函数要求。
    """
    if not src:
        return False
    return bool(re.search(r"\bsubprocess\.Popen\s*\(", src) or "startfile" in src)


def labels_match(actual: list, expected: list) -> bool:
    """菜单标签**逐项且按顺序**一致（多一项、少一项、顺序变了都不算）。"""
    return list(actual) == list(expected)


# ── F. 真把菜单建出来走一遍（动态，比静态字符串强）──────────────────────────
# ⚠ 静态检查只能证明"源码里有这些字"。pystray 的菜单是**运行期**构建的：
#   动作函数名写错、`pystray.Menu(*items)` 参数不对、标签重复导致
#   pystray 内部报错 —— 静态全绿、运行期炸。托盘是常驻进程，炸了用户
#   只看到"右键没反应"，日志里一个字都没有。所以这里真的 build 一次。
def case_f() -> None:
    sys.path.insert(0, str(REPO))
    try:
        import tray_app
    except Exception as e:                       # noqa: BLE001
        check(False, f"F0 能 import tray_app（实际 {type(e).__name__}: {e}）")
        return

    try:
        menu = tray_app.build_menu(None)
    except Exception as e:                       # noqa: BLE001
        check(False, f"F1 build_menu(None) 能跑通（实际 {type(e).__name__}: {e}）")
        return
    check(menu is not None, "F1 build_menu(None) 真的返回了菜单")

    def _label(item) -> str:
        t = item.text
        return t() if callable(t) else t

    def _is_sep(lab) -> bool:
        return bool(lab) and set(lab) <= {"-", " "}

    def _find_sub(m, label):
        for it in m:
            if _label(it) == label and getattr(it, "submenu", None):
                return it.submenu
        return None

    sub = _find_sub(menu, "修复 / 诊断")
    check(sub is not None, "F2 真菜单里能按标签找到「修复 / 诊断」子菜单")
    if sub is None:
        return

    items = [( _label(it), getattr(it, "_action", None)) for it in sub]
    labels = [lab for lab, _ in items if not _is_sep(lab)]
    check(labels_match(labels, EXPECT_ITEMS),
          f"F3 子菜单项与顺序完全一致（期望 {EXPECT_ITEMS}，实际 {labels}）")
    for lab, act in items:
        if lab in EXPECT_ACTIONS:
            got = getattr(act, "__name__", act)
            check(callable(act) and got == EXPECT_ACTIONS[lab],
                  f"F4 「{lab}」真的绑到 {EXPECT_ACTIONS[lab]}（实际 {got}）")

    # F0 反例：少一项 / 多一项 / 顺序颠倒 —— 都必须红
    check(not labels_match(labels[:1], EXPECT_ITEMS),
          "F0a 反例：只剩一项 → F3 判不合格（说明这条测得出来）")
    check(not labels_match(list(reversed(labels)), EXPECT_ITEMS),
          "F0b 反例：顺序颠倒 → F3 判不合格（顺序 = 排查顺序，不能乱）")
    check(not labels_match(labels + ["多余的项"], EXPECT_ITEMS),
          "F0c 反例：多出一项 → F3 判不合格")


def main() -> int:
    src = (REPO / "tray_app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # ── A. 菜单结构 ─────────────────────────────────────────────────────────
    menu = _fn_src(tree, src, "build_menu")
    check(menu is not None, "A0 tray_app.py 里能找到 build_menu()")
    check(menu is not None and "修复 / 诊断" in menu,
          "A1 build_menu 里有「修复 / 诊断」子菜单")
    miss = menu_has_items(menu, EXPECT_ITEMS)
    check(not miss, f"A2 子菜单项齐全（缺：{miss}）")
    wrong = menu_binds(menu, EXPECT_ACTIONS)
    check(not wrong, f"A3 每个菜单项都绑到了对应的动作函数（绑定错：{wrong}）")

    # ── A0 反例：缺一项 / 绑错一项，上面两条必须红 ──────────────────────────
    fake_menu = (
        "def build_menu(icon):\n"
        "    return pystray.Menu(\n"
        "        pystray.MenuItem('修复 / 诊断', pystray.Menu(\n"
        "            pystray.MenuItem('蓝牙配对修复（换过 USB 口、连不上时跑这个）', _fix_pairing),\n"
        "        )),\n"
        "    )\n"
    )
    check(menu_has_items(fake_menu, EXPECT_ITEMS),
          "A0a 反例：只给一项 → A2 判不合格（说明 A2 测得出来）")
    bad_bind = fake_menu.replace("_fix_pairing", "_wrong_name")
    check(menu_binds(bad_bind, EXPECT_ACTIONS),
          "A0b 反例：绑到不存在的动作函数 → A3 判不合格")

    # ── B. 动作函数真的存在 ─────────────────────────────────────────────────
    for lab, act in EXPECT_ACTIONS.items():
        check(_fn_src(tree, src, act) is not None,
              f"B 动作函数 {act}（{lab}）真的定义在 tray_app.py 里")

    # ── C. 工具名与 installer.iss / spec 一致 ──────────────────────────────
    iss = (REPO / "installer.iss").read_text(encoding="utf-8")
    spec = (REPO / "remote-voice-bridge.spec").read_text(encoding="utf-8")

    m = re.search(r'#define\s+MyFixBatName\s+"([^"]+)"', iss)
    iss_bat = m.group(1) if m else None
    check(iss_bat == "修复蓝牙配对.bat",
          f"C1 installer.iss 的 MyFixBatName = 修复蓝牙配对.bat（实际 {iss_bat!r}）")
    check(iss_bat is not None and iss_bat in src,
          f"C2 tray_app.py 找的 .bat 名字与 installer.iss 一致（{iss_bat!r}）"
          f"—— 改名不同步 = 菜单能点、永远'找不到修复工具'")

    m2 = re.search(r'#define\s+MyDiagExeName\s+"([^"]+)"', iss)
    iss_exe = m2.group(1) if m2 else None
    check(iss_exe == "RemoteVoiceBridgeDiag.exe",
          f"C3 installer.iss 的 MyDiagExeName = RemoteVoiceBridgeDiag.exe（实际 {iss_exe!r}）")
    check(iss_exe is not None and iss_exe in src,
          f"C4 tray_app.py 找的 Diag exe 名字与 installer.iss 一致（{iss_exe!r}）")
    check("name='RemoteVoiceBridgeDiag'" in spec,
          "C5 spec 里真的产出了 RemoteVoiceBridgeDiag 这个 exe")

    # 安装包必须把 bat 一起装上 —— 不然菜单在安装版上永远走不到那条路
    check(re.search(r'Source:\s*"\{#MyFixBatName\}"', iss) is not None,
          "C6 installer.iss 把这份 .bat 装进安装目录（否则安装版永远找不到）")

    # ── D. 开关是工具真的认的；且不许阻塞 ──────────────────────────────────
    pair_src = (REPO / "pairing.py").read_text(encoding="utf-8")
    check('"--fix-pairing"' in pair_src,
          "D1 pairing.py 认 `--fix-pairing`（托盘修复项传的就是它）")
    check('"--dry-run"' in pair_src,
          "D2 pairing.py 认 `--dry-run`（只读体检项传的就是它）")
    # --fix-pairing --dry-run 必须等于"只读、不弹 UAC"
    check("if not is_admin() and not dry:" in pair_src,
          "D3 pairing.py 里 --dry-run 走的是**不提权**那条路（体检项才不会弹 UAC）")

    for act in EXPECT_ACTIONS.values():
        body = _fn_src(tree, src, act)
        check(is_nonblocking(body),
              f"D4 {act} 不阻塞托盘线程（不许 subprocess.run / os.system）")
    for helper in ("_run_pairing_tool", "_spawn"):
        check(is_nonblocking(_fn_src(tree, src, helper)),
              f"D5 {helper} 不阻塞托盘线程")
    # 真正落地的那一层必须**真的起新窗口** —— 否则工具的输出用户看不见
    spawn_body = _fn_src(tree, src, "_spawn")
    check(spawns_window(spawn_body),
          "D6 _spawn 真的起进程（subprocess.Popen）")
    check(spawn_body is not None and "CREATE_NEW_CONSOLE" in spawn_body,
          "D7 _spawn 用 CREATE_NEW_CONSOLE —— 这些工具要 pause，"
          "没有自己的窗口用户就看不到结果")
    # _run_pairing_tool 是**委托层**：bat 走 _open_path（ShellExecute），
    # exe / python 走 _spawn（Popen）。它自己不直接 spawn，所以查委托关系。
    rp = _fn_src(tree, src, "_run_pairing_tool")
    check(rp is not None and "_open_path" in rp and "_spawn" in rp,
          "D8 _run_pairing_tool 把 bat 交给 _open_path（ShellExecute）、"
          "把 exe/python 交给 _spawn（Popen）")

    # D0 反例：阻塞写法必须被判不合格
    check(not is_nonblocking("def _act(icon, item):\n    subprocess.run(['x'])\n"),
          "D0a 反例：用 subprocess.run 等工具跑完 → 判不合格（托盘会卡死）")
    check(not is_nonblocking("def _act(icon, item):\n    os.system('x')\n"),
          "D0b 反例：用 os.system → 判不合格（GUI 版 stdout 是空的，输出全丢）")
    check(is_nonblocking("def _act(icon, item):\n    os.startfile('x.bat')\n"),
          "D0c 对照：os.startfile 判合格（ShellExecute，非阻塞）")
    check(not spawns_window("def _spawn(c):\n    subprocess.run(c)\n"),
          "D0d 反例：只有 run 没有 Popen → D6 判不合格")

    # ── E. 兜底顺序 bat → exe → python ─────────────────────────────────────
    rp = _fn_src(tree, src, "_run_pairing_tool")
    check(rp is not None, "E0 找到 _run_pairing_tool()")
    if rp:
        i_bat = rp.find("修复蓝牙配对.bat")
        i_exe = rp.find("RemoteVoiceBridgeDiag.exe")
        i_py = rp.find("pairing.py")
        check(0 <= i_bat < i_exe < i_py,
              f"E1 兜底顺序是 bat({i_bat}) → exe({i_exe}) → python({i_py})"
              f"—— 安装版优先走 bat，源码版也能用")
        check("--fix-pairing" in rp,
              "E2 _run_pairing_tool 里带 --fix-pairing（两个环境都认这个名字）")

    # ── F. 真建菜单走一遍 ───────────────────────────────────────────────────
    case_f()

    for m_ in FAILS:
        print(f"  FAIL {m_}")
    if FAILS:
        print(f"TRAY TOOLS FAILED（{len(FAILS)} 项）")
        print("  提示：托盘菜单是「静默失效」的重灾区 —— 菜单项指向的函数改名、")
        print("        工具文件名写错、传了个不认的开关，用户看到的都只是")
        print("        「点了没反应」，日志里一个字都没有。")
        return 1
    print(f"  OK   {PASSES} 项全过（菜单结构 / 动作函数 / 工具名一致 / 不阻塞 / 兜底顺序）")
    print("TRAY TOOLS OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
