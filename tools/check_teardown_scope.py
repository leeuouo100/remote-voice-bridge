"""run_bridge 的「收尾范围」回归闸 —— 2026-09-29 审查报告 P1-3。

背景
====
原先的 `try/finally` 只包住**主循环**，而**建立阶段**（连 BLE → 找 ATVV 服务
→ 找三个特征 → 订阅通知 → 起音频流 → 装键盘钩子 → 注入 Frida）中间有十来条
`return False`。那些路**直接绕过 finally**：

  · `GattSession.maintain_connection` 还举着 —— Windows 会一直拽着这条链路；
  · `BluetoothLEDevice` 没 close；
  · Frida 注入的会话还挂在 WUDFHost 里；
  · 音频流没停。

全靠 GC 回收，而 GC 什么时候跑、跑不跑得到都不确定。现象是"重连之后第一次
总是失败"，日志里还看不出原因。

现在改成：`run_bridge` 只剩一层薄壳，真正的活儿在 `_run_bridge_inner`；
不管它正常返回、中途 `return False`、还是抛异常，收尾都走同一个 `teardown()`。

这道闸钉五件事（每条都配反例）：

  A. 结构：`run_bridge` 是薄壳（`try/finally` 里调 `teardown()`），
     `_run_bridge_inner` 里**没有自己的 finally 清理**（否则又是两套）
  B. **句柄登记完整性**：`_BridgeResources.__init__` 里列的每一个句柄，
     在 main.py 里都真的有 `res.<名字> = …` 的登记点
     —— 漏一个 = 那个资源在失败路径上永远不会被释放，而**没有任何报错**
  C. `teardown()` 逐个 None 判断（建立阶段失败时句柄是 None，不能抛）
  D. `teardown()` 覆盖到每一个句柄（不许登记了却忘了释放）
  E. 动态：句柄全为 None 时 `await teardown()` **不抛异常**
     （这正是"连接第一步就失败"的场景）
  F. `teardown` 引用的**全局名**在模块顶层必须真的有定义
     —— 类方法看不见别的函数里的局部变量，写裸名 = 运行期 NameError
     = 被 `except` 吞成一行 warning = **那条收尾路径永远不跑**
     （2026-09-29 真漏：`end_voice_session` 就是这么失效的）

用法： python tools/check_teardown_scope.py
输出： TEARDOWN SCOPE OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import ast
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

_SANDBOX = tempfile.mkdtemp(prefix="rvb-teardown-")
os.environ["APPDATA"] = _SANDBOX
os.environ["RVB_NO_AUTO_CONSOLE"] = "1"

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FAILS: list[str] = []
PASSES = 0


def check(cond, msg) -> bool:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
    return bool(cond)


def _fn_src(tree: ast.AST, src: str, name: str) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return None


def _cls_src(tree: ast.AST, src: str, name: str) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return None


# ── 可被反例复用的检查器 ─────────────────────────────────────────────────────

def handles_declared(cls_src: str) -> list[str]:
    """`__init__` 里 `self.X = None/False/0` 声明的句柄名（保持声明顺序）。

    ⚠ 只扫 `__init__` 那一段，不要扫整个类 —— `teardown` 里也有
      `self.xxx = ...`（比如 `maintain_connection = False`），扫全类会串味。
    """
    import re
    init = re.search(r"def __init__\(self\)[^\n]*:\n(.*?)(?=\n    def )",
                     cls_src, flags=re.S)
    body = init.group(1) if init else cls_src
    names: list[str] = []
    for m in re.finditer(r"self\.([A-Za-z_]\w*)\s*=\s*(None|False|0)\b", body):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


def registered_in(main_src: str, names: list[str]) -> list[str]:
    """返回**没有** `res.<name> = …` 登记点的句柄名。"""
    import re
    return [n for n in names
            if not re.search(rf"\bres\.{re.escape(n)}\s*=", main_src)]


def released_in(teardown_src: str, names: list[str]) -> list[str]:
    """返回 teardown 里**没有**碰过的句柄名。"""
    return [n for n in names
            if f"self.{n}" not in teardown_src]


def guarded_in(teardown_src: str, names: list[str]) -> list[str]:
    """返回 teardown 里**没有 None 判断**的句柄名。

    建立阶段失败时句柄还是 None —— 直接 `self.ble.close()` 会抛
    AttributeError，把真正的错误盖掉，而且**后面的清理全不跑了**。
    """
    import re
    bad = []
    for n in names:
        if f"self.{n}" not in teardown_src:
            continue
        if not re.search(rf"self\.{re.escape(n)}\s+is\s+not\s+None", teardown_src):
            bad.append(n)
    return bad


def module_level_names(tree: ast.AST) -> set[str]:
    """模块顶层**真正绑定过**的名字：函数 / 类 / 赋值目标 / import / for 目标 …

    ⚠ 只看模块顶层。**嵌套在别的函数里的定义不算** —— 那正是本组要抓的东西：
      局部函数在别的函数里叫这个名字，不代表模块顶层有。
    """
    names: set[str] = set()

    def bind(target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for e in target.elts:
                bind(e)
        elif isinstance(target, ast.Starred):
            bind(target.value)

    def stmts(body) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    bind(t)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                bind(node.target)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    names.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                bind(node.target)
                stmts(node.body); stmts(node.orelse)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for it in node.items:
                    if it.optional_vars is not None:
                        bind(it.optional_vars)
                stmts(node.body)
            elif isinstance(node, ast.If):
                stmts(node.body); stmts(node.orelse)
            elif isinstance(node, ast.Try):
                stmts(node.body); stmts(node.orelse); stmts(node.finalbody)
                for h in node.handlers:
                    stmts(h.body)
            elif isinstance(node, ast.Global):
                names.update(node.names)

    stmts(tree.body)
    return names


def fn_globals(src: str, fn_name: str) -> set[str]:
    """用 symtable 取某个函数里**按全局名解析**的标识符集合。

    为什么不用正则：得区分"全局名"和"闭包变量"（后者在别的函数里定义，
    在这里属于 free 变量）—— 只有 symtable 分得清。
    """
    import symtable
    st = symtable.symtable(src, "<gate>", "exec")
    found: list = []

    def walk(tbl) -> None:
        for c in tbl.get_children():
            if c.get_name() == fn_name:
                found.append(c)
            walk(c)

    walk(st)
    return set(found[0].get_globals()) if found else set()


def unresolved_globals(src: str, fn_name: str, module_names: set[str]) -> list[str]:
    """返回「在 `fn_name` 里按全局名用、但模块顶层没有定义、也不是内置」的名字。

    这种名字不会在导入时报错，只在**真的执行到那一行**时抛 NameError ——
    如果那一行又恰好在一个 `except Exception` 里，就变成一行 warning：
    **代码看着在，实际上永远没跑**。
    """
    import builtins as _bi
    ok = set(module_names) | set(dir(_bi))
    return sorted(n for n in fn_globals(src, fn_name) if n not in ok)


def case_static() -> None:
    src = (REPO / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # ── A. 结构 ─────────────────────────────────────────────────────────────
    wrapper = _fn_src(tree, src, "run_bridge")
    inner = _fn_src(tree, src, "_run_bridge_inner")
    check(wrapper is not None, "A0 找得到 run_bridge()")
    check(inner is not None, "A1 找得到 _run_bridge_inner()")
    check(wrapper is not None and "try:" in wrapper and "finally:" in wrapper,
          "A2 run_bridge 是 try/finally 结构")
    check(wrapper is not None and "await res.teardown()" in wrapper,
          "A3 run_bridge 的 finally 里调了 res.teardown()")
    check(wrapper is not None and "_run_bridge_inner" in wrapper,
          "A4 run_bridge 把活儿交给 _run_bridge_inner")
    check(wrapper is not None and "return res.ran_ok" in wrapper,
          "A5 返回值来自 res.ran_ok（teardown 之后才 return）")
    # ⚠ 内层不许再有自己的 finally 清理 —— 两套收尾 = 迟早不一致
    check(inner is not None and "finally:" not in inner,
          "A6 _run_bridge_inner 里**没有**自己的 finally（收尾只有一处）")
    # A0 反例：老结构（finally 只包主循环）必须被判不合格
    fake_old = ("async def run_bridge(device_type=None):\n"
                "    ran_ok = False\n"
                "    try:\n"
                "        while True:\n"
                "            pass\n"
                "    finally:\n"
                "        logger.info('🧹 Cleanup done')\n"
                "    return ran_ok\n")
    fo = _fn_src(ast.parse(fake_old), fake_old, "run_bridge")
    check(fo is not None and "teardown" not in fo,
          "A0a 反例：老结构里没有 teardown → A3 判不合格")
    check(fo is not None and "_run_bridge_inner" not in fo,
          "A0b 反例：老结构没拆壳 → A4 判不合格")

    # ── B. 句柄登记完整性 ───────────────────────────────────────────────────
    cls = _cls_src(tree, src, "_BridgeResources")
    check(cls is not None, "B0 找得到 _BridgeResources")
    names = handles_declared(cls or "")
    check(len(names) >= 12,
          f"B1 句柄清单非空且够全（实际 {len(names)} 个：{names}）")
    missing = registered_in(src, names)
    check(not missing,
          f"B2 每个句柄都有 `res.<名字> = …` 登记点（漏了：{missing}）"
          f"—— 漏一个 = 那个资源在失败路径上永远不会被释放，且没有任何报错")
    # B0 反例：少一个登记点必须被查出来
    fake_main = "res.ble = ble\nres.gatt_session = s\n"     # 少了其它
    check(registered_in(fake_main, ["ble", "gatt_session", "sysmic"]) == ["sysmic"],
          "B0a 反例：漏登记的句柄被查出来（说明 B2 测得出来）")

    # ── C/D. teardown 的覆盖与 None 判断 ───────────────────────────────────
    # ⚠ 取 teardown 要用**模块源码**做 segment 基准（node 的行号是模块级的），
    #   传类源码会 IndexError —— 那不是代码有问题，是闸门自己取错了基准。
    td = _fn_src(tree, src, "teardown")
    check(td is not None, "C0 找得到 teardown()")
    # ran_ok / voice_active 是标志位、不是句柄，释放时不需要 None 判断
    flag_names = {"ran_ok", "voice_active"}
    handle_names = [n for n in names if n not in flag_names]
    not_released = released_in(td or "", handle_names)
    check(not not_released,
          f"D1 teardown 覆盖到每个句柄（漏了：{not_released}）")
    unguarded = guarded_in(td or "", handle_names)
    check(not unguarded,
          f"C1 teardown 里每个句柄都有 None 判断（缺：{unguarded}）"
          f"—— 建立阶段失败时句柄是 None，直接点属性会抛，后面的清理全不跑")
    # C0 反例
    fake_td = "async def teardown(self):\n    try: self.ble.close()\n    except Exception: pass\n"
    check(guarded_in(fake_td, ["ble"]) == ["ble"],
          "C0a 反例：没有 None 判断的 teardown 被查出来")
    check(guarded_in("if self.ble is not None:\n    pass\n", ["ble"]) == [],
          "C0b 对照：有 None 判断就判合格（同一个判定函数）")
    # C2 teardown 自己不许整体裸奔（每一步都得自己 try 住）
    check(td is not None and "Cleanup done" in td,
          "C2 teardown 结尾仍然打「Cleanup done」（验收要求的那行日志）")

    # ── E. 动态：句柄全 None 时 teardown 不许抛 ─────────────────────────────
    try:
        import main
        res = main._BridgeResources()
        asyncio.run(res.teardown())
        check(True, "E1 句柄全为 None 时 teardown 正常跑完")
    except Exception as e:                       # noqa: BLE001
        import traceback
        check(False, f"E1 句柄全为 None 时 teardown 抛了异常："
                     f"{type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}")

    # ── F. teardown 引用的全局名必须真的存在 ────────────────────────────────
    #
    # ⚠ 这一组是 2026-09-29 推进 P2 时补的，起因是一个**真的漏网**：
    #
    #   teardown（模块级类 `_BridgeResources` 的方法）里调用了
    #   `end_voice_session(...)`，而那是 `_run_bridge_inner` 里的**局部函数**。
    #   在类方法里它被解析成**全局名**，模块顶层根本没有这个名字
    #   ⇒ 运行期 `NameError: name 'end_voice_session' is not defined`
    #   ⇒ 被 teardown 自己那句 `except Exception as e: logger.warning(...)` 吞掉
    #   ⇒ **退出 / 断连那条收尾路径整条没跑**，日志上只多一行"退出前收尾异常"。
    #
    #   为什么 A~E 全绿却漏了它：E1 用的是"句柄全为 None"的场景，而那句调用
    #   正好包在 `if self.atvv is not None and self.coord is not None:` 里 ——
    #   两个都是 None ⇒ **那一行根本不执行** ⇒ 动态探针永远走不到它。
    #   教训：**判据得能走到那一行**；光"跑一遍不抛"是探不到未执行代码的。
    #
    #   现在改成：把"统一收尾入口"登记到 `res.end_voice_session`，teardown 走
    #   `self.end_voice_session(...)`。这道闸钉的就是"别再退化成裸名"。
    mod_names = module_level_names(tree)
    bad_globals = unresolved_globals(src, "teardown", mod_names)
    check(not bad_globals,
          f"F1 teardown 引用的全局名必须在模块顶层有定义（缺：{bad_globals}）"
          f"—— 否则运行期 NameError，被 except 吞成一行 warning，收尾静默失效")
    # F0a 对照：同片段里定义过就不报（同一个判定函数）
    _ok_src = ("def end_voice_session(x):\n"
               "    return x\n"
               "async def teardown(self):\n"
               "    end_voice_session('bye')\n")
    check(unresolved_globals(_ok_src, "teardown",
                             module_level_names(ast.parse(_ok_src))) == [],
          "F0a 对照：模块顶层定义过就不报")
    # F0b 反例：局部函数当全局名用 —— 正是本次那个 bug 的形状
    _bad_src = ("async def inner():\n"
                "    def end_voice_session(x):\n"
                "        return x\n"
                "    return end_voice_session\n"
                "async def teardown(self):\n"
                "    end_voice_session('bye')\n")
    check(unresolved_globals(_bad_src, "teardown",
                             module_level_names(ast.parse(_bad_src))) == ["end_voice_session"],
          "F0b 反例：局部函数当全局名用 → 被查出来（本次那个 bug 的形状）")
    # F0c 真反例：把**本文件真实的 teardown** 改回裸名调用 —— 必须当场报出来。
    #      用真文件自证，比拿玩具片段自证强：它证明判据作用在**实际代码**上有效。
    legacy_src = src.replace("self.end_voice_session", "end_voice_session")
    check(unresolved_globals(legacy_src, "teardown",
                             module_level_names(ast.parse(legacy_src))) == ["end_voice_session"],
          "F0c 真反例：把 teardown 里的 self.end_voice_session 改回裸名 → 判据当场报出")
    # F2 登记点必须真的在（B2 也会核，这里给一条**指名道姓**的失败信息）
    check("res.end_voice_session = end_voice_session" in src,
          "F2 main.py 里有 `res.end_voice_session = end_voice_session` 登记点"
          " —— 少了它，teardown 里的收尾入口永远是 None")


def main() -> int:
    try:
        case_static()
    except Exception as e:                       # noqa: BLE001
        import traceback
        FAILS.append(f"case_static 抛异常：{type(e).__name__}: {e}\n"
                     f"{traceback.format_exc()[-600:]}")

    shutil.rmtree(_SANDBOX, ignore_errors=True)

    for m in FAILS:
        print(f"  FAIL {m}")
    if FAILS:
        print(f"TEARDOWN SCOPE FAILED（{len(FAILS)} 项）")
        print("  提示：收尾必须是**一处**、且从拿到第一个句柄起就生效。")
        print("        建立阶段那十来条 `return False` 绕过 finally 的话，")
        print("        GattSession / 注入 / 音频流全靠 GC —— 表现是「重连后第一次总失败」。")
        return 1
    print(f"  OK   {PASSES} 项全过（薄壳结构 / 登记完整 / None 判断 / 覆盖 / "
          f"全 None 不抛 / 全局名可解析）")
    print("TEARDOWN SCOPE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
