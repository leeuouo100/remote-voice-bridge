"""托盘菜单闸 —— 「点得动」+「点退出真的退得掉」。

为什么单独一条闸
----------------
2026-10-09 真机，用户原话：

    我点退出没有反应，只能用任务管理器强制关闭。

py-spy 现场取证（进程 PID 1356，装的是 v1.0.28）：

    MainThread   停在 pystray `_mainloop`   （正常等消息）
    rvb-bridge   停在 asyncio `_poll`       （正常空转）

⇒ **没有任何线程在执行 `_quit`**，进程也没卡死。
所以不是"退不掉"，是**那个回调根本没被调用**。

根因在 pystray 的 Win32 后端（`_win32.py::_on_notify`）：

    hmenu, descriptors = self._menu_handle
    index = TrackPopupMenuEx(hmenu, ..., TPM_RETURNCMD, ...)   # 阻塞显示菜单
    if index > 0:
        descriptors[index - 1](self)     # ← index==0 时**静默什么都不做**

而 `update_menu()` 的第一件事是 `DestroyMenu(旧句柄)`。
老代码在 `_refresh` 里**每 0.8 秒无条件**调一次 `update_menu()` ⇒
菜单只要开着超过 0.8 秒，句柄就被销毁 ⇒ `TrackPopupMenuEx` 返回 0
⇒ **点任何一项都毫无反应，而且一声不响**（没有异常、日志里一个字都没有）。

用户的操作必然是"右键 → 看一眼 → 找到退出 → 点"，早就超过 0.8 秒了。

这条闸钉四件事
--------------
1. `_refresh` 里 `update_menu()` **必须在条件分支内**（按菜单内容指纹节流），
   不许无条件重建 —— 那正是事故本身。
2. 那个"条件"必须真的接上了 `_menu_fingerprint()`（否则条件恒真/恒假都是坑）。
3. `_quit` 必须有一条**不依赖消息循环**的硬保底（`os._exit`），
   而且它只能在超时后、由独立线程触发（正常路径不许走到）。
4. `_refresh` 必须是**模块级**函数 —— 埋在 `main()` 闭包里就没法测，
   这正是它带着 bug 活到今天的原因之一。

用法
----
    python tools/check_tray_menu.py

退出码 0 = 通过；1 = 失败。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TRAY = ROOT / "tray_app.py"

# 只有这些才算"被条件保护住了"。⚠ **`try` 不算** ——
# `try: X() except: pass` 里的 X() 是**无条件执行**的，把它当保护会让
# 反例（去掉 if、只留 try）假绿。这是本闸最容易写错的一处。
_GUARDS = (ast.If, ast.For, ast.While, ast.AsyncFor, ast.AsyncWith)


def _parse(src: str) -> ast.Module:
    return ast.parse(src)


def _find_func(tree: ast.Module, name: str) -> ast.AST | None:
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _top_level_func(tree: ast.Module, name: str) -> ast.AST | None:
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _unguarded_calls(func: ast.AST, attr: str) -> list[int]:
    """`func` 体内**无条件**执行的 `X.attr(...)` 调用的行号。"""
    hits: list[int] = []

    def walk(node: ast.AST, guarded: bool) -> None:
        for ch in ast.iter_child_nodes(node):
            if (isinstance(ch, ast.Call)
                    and isinstance(ch.func, ast.Attribute)
                    and ch.func.attr == attr
                    and not guarded):
                hits.append(ch.lineno)
            walk(ch, guarded or isinstance(ch, _GUARDS))

    for stmt in getattr(func, "body", []):
        walk(stmt, isinstance(stmt, _GUARDS))
    return hits


def _called_names(func: ast.AST) -> set[str]:
    """函数体里出现过的调用名 + 被引用到的名字。

    ⚠ 为什么要连 `ast.Name` 一起收：`_refresh` 在 `main()` 里是
      `threading.Timer(0.8, _refresh)` 的**参数**，不是被调用的函数 ——
      只收 `Call` 会把它漏掉，判据会对着正确的代码报红（本闸第一版就是这么错的）。
    """
    out: set[str] = set()
    for n in ast.walk(func):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
        elif isinstance(n, ast.Name):
            out.add(n.id)
    return out


# ── 1. 菜单不许被无条件重建 ─────────────────────────────────────────────────
def check_refresh(src: str, verbose: bool = True) -> bool:
    ok = True
    tree = _parse(src)
    fn = _find_func(tree, "_refresh")
    if fn is None:
        print("   ❌ 找不到 _refresh")
        return False

    bad = _unguarded_calls(fn, "update_menu")
    if bad:
        print(f"   ❌ _refresh 第 {bad} 行**无条件**调 update_menu() —— "
              f"update_menu 的第一件事是 DestroyMenu(旧句柄)，而菜单正被"
              f"TrackPopupMenuEx 用着 ⇒ 返回 0 ⇒ 回调静默不执行 ⇒ "
              f"「点退出没反应」（2026-10-09 真机）")
        ok = False
    elif verbose:
        print("   OK update_menu() 在条件分支内（菜单开着时不会被销毁句柄）")

    if "_menu_fingerprint" not in _called_names(fn):
        print("   ❌ _refresh 里没有调用 _menu_fingerprint() —— 那个条件没接上"
              "真实状态，等于换个方式的无条件重建")
        ok = False
    elif verbose:
        print("   OK 重建条件接的是 _menu_fingerprint()（菜单内容指纹）")

    return ok


# ── 2. _refresh 必须是模块级（可测）─────────────────────────────────────────
def check_toplevel(src: str, verbose: bool = True) -> bool:
    ok = True
    tree = _parse(src)

    if _top_level_func(tree, "_refresh") is None:
        print("   ❌ _refresh 不是模块级函数 —— 埋在 main() 的闭包里就没法测，"
              "这正是它带着这个 bug 活到今天的原因之一")
        ok = False
    elif verbose:
        print("   OK _refresh 是模块级函数（闸门能直接调它）")

    main_fn = _find_func(tree, "main")
    if main_fn is not None:
        nested = {n.name for n in ast.walk(main_fn)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if "_refresh" in nested:
            print("   ❌ main() 里又定义了一个 _refresh —— 两个同名实现，"
                  "改一份不生效（本项目在 main() 死代码上栽过一次）")
            ok = False
        elif "_refresh" not in _called_names(main_fn):
            print("   ❌ main() 里没有把 _refresh 挂上定时器 —— 界面就不会刷新")
            ok = False
        elif verbose:
            print("   OK main() 把模块级的 _refresh 挂上了定时器")

    return ok


# ── 3. 退出必须有硬保底，且只在超时后触发 ───────────────────────────────────
def check_quit_fallback(src: str, verbose: bool = True) -> bool:
    ok = True
    tree = _parse(src)
    quit_fn = _find_func(tree, "_quit")
    if quit_fn is None:
        print("   ❌ 找不到 _quit")
        return False

    # (a) 不许在 _quit 的直接路径上硬退 —— 那会跳过桥线程收尾
    direct = []
    nested: dict[str, ast.AST] = {}
    for n in ast.walk(quit_fn):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not quit_fn:
            nested[n.name] = n
    for n in ast.walk(quit_fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_exit"):
            in_nested = any(_contains(f, n) for f in nested.values())
            if not in_nested:
                direct.append(n.lineno)
    if direct:
        print(f"   ❌ _quit 第 {direct} 行在**直接路径**上 os._exit() —— "
              f"那会跳过桥线程的 teardown（GattSession / Frida / 音频流）")
        ok = False

    # (b) 必须有硬保底，且由独立线程触发
    holders = [name for name, f in nested.items()
               if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr == "_exit" for n in ast.walk(f))]
    if not holders:
        print("   ❌ _quit 里没有 os._exit 硬保底 —— icon.stop() 只是往托盘窗口"
              "PostMessage(WM_STOP)，要靠消息循环翻成 PostQuitMessage。"
              "任何一步没走通，main() 就永远不返回，用户只能去任务管理器强杀")
        ok = False
    else:
        threaded = set()
        for n in ast.walk(quit_fn):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "Thread"):
                for kw in n.keywords:
                    if kw.arg == "target" and isinstance(kw.value, ast.Name):
                        threaded.add(kw.value.id)
        missing = [h for h in holders if h not in threaded]
        if missing:
            print(f"   ❌ 硬保底函数 {missing} 没有被 threading.Thread 挂上 —— "
                  f"写在 _quit 里同步执行就成了「直接硬退」")
            ok = False
        elif verbose:
            print(f"   OK 硬保底 {holders} 由独立线程触发（正常路径走不到）")

    return ok


def _contains(root: ast.AST, target: ast.AST) -> bool:
    return any(n is target for n in ast.walk(root))


# ── 4. 行为级：假 icon 连刷三次，看菜单被重建几次 ───────────────────────────
class _FakeIcon:
    """够 `_refresh` 用的最小替身：只数 update_menu 被调了几次。"""

    def __init__(self) -> None:
        self.menu_updates = 0
        self.icon = None
        self.title = None

    def update_menu(self) -> None:
        self.menu_updates += 1


def check_behavior(verbose: bool = True) -> bool:
    ok = True
    try:
        import tray_app
        import state
    except Exception as e:                                # noqa: BLE001
        print(f"   ❌ import 失败：{type(e).__name__}: {e}")
        return False

    # ⚠ `_refresh` 结尾会 `threading.Timer(0.8, _refresh).start()` 递归自续。
    #   测试里先把 `_stop` 置位，让它自己停 —— 否则测试进程会被一串定时器拖着。
    saved_stop = tray_app._stop
    tray_app._stop = __import__("threading").Event()
    tray_app._stop.set()
    fake = _FakeIcon()
    try:
        tray_app._menu_fp = None
        state.update(connected=True, streaming=False, device="T", last_event="a")

        tray_app._refresh(fake)                 # ① 首次：指纹从 None 变化 → 建
        n1 = fake.menu_updates
        tray_app._refresh(fake)                 # ② 状态没变 → 不该重建
        n2 = fake.menu_updates
        tray_app._refresh(fake)                 # ③ 同上
        n3 = fake.menu_updates
        state.update(connected=False)           # ④ 状态变了 → 该重建
        tray_app._refresh(fake)
        n4 = fake.menu_updates
    finally:
        tray_app._stop = saved_stop
        tray_app._icon = None

    if n1 != 1:
        print(f"   ❌ 首次刷新应建一次菜单，实际 {n1} 次")
        ok = False
    elif n2 != 1 or n3 != 1:
        print(f"   ❌ **状态没变却又重建了菜单**（{n1} → {n2} → {n3}）——"
              f"菜单正开着时这一下就会 DestroyMenu 掉用户正在点的那个句柄")
        ok = False
    elif n4 != 2:
        print(f"   ❌ 状态变了却没重建菜单（{n3} → {n4}）——"
              f"菜单标题/勾选就会一直停在旧状态")
        ok = False
    elif verbose:
        print(f"   OK 状态不变不重建（{n1}/{n2}/{n3}），状态变了才重建（{n4}）")

    # 自证：计数器真的在工作（手动无条件刷三次，计数必须跟着涨）
    before = fake.menu_updates
    for _ in range(3):
        fake.update_menu()
    if fake.menu_updates != before + 3:
        print("   ❌ [自证] 假 icon 的计数器没跟着涨 → 上面那条判据测不出东西")
        ok = False
    elif verbose:
        print("   OK [自证] 计数器有效（手动刷 3 次 → 计数 +3）")

    return ok


# ── 5. 反例自证 ─────────────────────────────────────────────────────────────
def run_counter_examples(verbose: bool = True) -> bool:
    ok = True
    print("\n── 反例自证（判据真的会红吗）──")
    src = TRAY.read_text(encoding="utf-8", errors="replace")

    # 反例 1：去掉那个条件，退回"每次刷新都重建"（老代码的形状）
    # ⚠ 必须把整个 if 块（含 try/except）一起换掉 —— 只换前两行会留下
    #   8 空格缩进的 try，变成 IndentationError，反例根本跑不起来。
    pat = (r"    if fp != _menu_fp:\n        _menu_fp = fp\n        try:\n"
           r"            ic\.update_menu\(\)\n        except Exception:[^\n]*\n"
           r"            pass\n")
    broken1 = re.sub(pat, "    _menu_fp = fp\n    try:\n        ic.update_menu()\n"
                          "    except Exception:\n        pass\n", src, count=1)
    if broken1 == src:
        print("   ❌ [反例] 找不到重建条件的锚点（闸门锚点过期了）")
        return False
    if _unguarded_calls(_find_func(_parse(broken1), "_refresh"), "update_menu"):
        print("   OK [反例] 去掉条件 → 无条件重建判据变红")
    else:
        print("   ❌ [反例] 去掉条件后仍判成有条件 → 判据无效")
        ok = False

    # 反例 2：把指纹换成常量 → 条件接不上真实状态
    broken2 = src.replace("        fp = _menu_fingerprint()\n", "        fp = None\n", 1)
    if broken2 == src:
        print("   ❌ [反例] 找不到 _menu_fingerprint() 的锚点")
        ok = False
    elif "_menu_fingerprint" in _called_names(_find_func(_parse(broken2), "_refresh")):
        print("   ❌ [反例] 删掉指纹调用后仍判成接了指纹 → 判据无效")
        ok = False
    else:
        print("   OK [反例] 删掉指纹调用 → 指纹判据变红")

    # 反例 3：删掉 _quit 的硬保底
    broken3 = src.replace("        os._exit(0)\n", "        pass\n", 1)
    if broken3 == src:
        print("   ❌ [反例] 找不到 os._exit 的锚点")
        ok = False
    else:
        holders = [n.name for n in ast.walk(_find_func(_parse(broken3), "_quit"))
                   if isinstance(n, ast.FunctionDef)
                   and any(isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
                           and x.func.attr == "_exit" for x in ast.walk(n))]
        if holders:
            print("   ❌ [反例] 删掉 os._exit 后仍判成有硬保底 → 判据无效")
            ok = False
        else:
            print("   OK [反例] 删掉 os._exit → 硬保底判据变红")

    # 反例 4：`try` 不算保护 —— 否则"去掉 if、只留 try"会假绿
    probe = _parse(
        "def f():\n"
        "    try:\n"
        "        ic.update_menu()\n"
        "    except Exception:\n"
        "        pass\n")
    if not _unguarded_calls(_find_func(probe, "f"), "update_menu"):
        print("   ❌ [反例] try 被当成了保护 → 去掉 if 只留 try 的写法会假绿")
        ok = False
    else:
        print("   OK [反例] try 不算保护（去掉 if 只留 try 会被抓出来）")

    return ok


def main() -> int:
    print("=" * 60)
    print("托盘菜单闸 —— 点得动 + 点退出真的退得掉")
    print("=" * 60)
    src = TRAY.read_text(encoding="utf-8", errors="replace")

    print("\n── 1. 菜单不许被无条件重建 ──")
    ok = check_refresh(src)

    print("\n── 2. _refresh 必须是模块级（可测）──")
    ok = check_toplevel(src) and ok

    print("\n── 3. 退出硬保底（且只在超时后触发）──")
    ok = check_quit_fallback(src) and ok

    print("\n── 4. 行为级：假 icon 连刷三次 ──")
    ok = check_behavior() and ok

    ok = run_counter_examples() and ok

    print("\n" + ("✅ 通过" if ok else "❌ 失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
