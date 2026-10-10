"""鼠标模式闸 —— 「方向键推指针」这件事，四条硬约束 + 一张真值表。

为什么单独一条闸
----------------
v1.0.31 加的这个功能，**每一条错法都是用户直接受害**，而且全是静默的：

  · 松开丢了 ⇒ 指针**一直往一个方向跑**，用户只能拔遥控器电池
    （真机实测 2026-10-10 15:48：`up` 与 `vol_down` 各丢了一次松开 ——
      厂商页报告一次只带**一个**按键，前一个键的松开被顶掉了）
  · 注入持锁 ⇒ `release()` 被卡在 BLE 回调线程上 ⇒ 手都松了指针还在走
  · 低速时不做亚像素累加 ⇒ 慢慢推指针**纹丝不动**（int() 把 0.3 抹成 0）
  · 语音会话中按确认键那条规则被鼠标模式抢走 ⇒ 说话时误点出一发左键

所以这道闸分两半：

  ① **引擎真值表**（`mouse_mode.MouseMover`）—— 它是**纯逻辑**，输入是
     (方向, 按下/松开, 时刻)、输出是位移序列。闸门拿假时钟 + 记录调用的桩
     驱动它，**完全不碰硬件**（`autostart=False` ⇒ 一个后台线程都不起）。
  ② **接线断言** —— 引擎再对，接错地方也白搭。重点钉三条：
     · `_mouse_handle` 的调用点必须在那条「语音会话中按确认键 = 收尾」**之后**
     · `_get_cfg()` 的白名单必须有 5 个 `mouse_*` 字段（不加就**静默**不生效）
     · `res.mouse_mover` 要登记、`teardown` 要 `stop()`（否则退出后指针还在跑）

最后一段是**反例自证**：把每条判据的锚点人为改坏，断言它真的会红 ——
不然"全绿"可能只是判据写错了。

用法
----
    python tools/check_mouse_mode.py

退出码 0 = 通过；1 = 失败。
"""
from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

# 引擎里那条"丢松开、强制停下"的 warning 是本闸**故意**要触发的（T5 / 反例 3）。
# 不关掉的话它会以 lastResort handler 直接喷到 stderr，和闸门的输出搅在一起。
logging.getLogger("rvb.mouse").setLevel(logging.CRITICAL)
logging.getLogger("rvb.mouse").propagate = False

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MAIN = ROOT / "main.py"
CONFIG = ROOT / "config.py"
STATE = ROOT / "state.py"
ENGINE = ROOT / "mouse_mode.py"

# main.py 里「语音会话中按确认键 = 收尾」那条规则的特征串。
# 鼠标模式**必须**排在它后面 —— 正在说话时误按确认键，应该关语音、不是点左键。
_OK_ENDS_VOICE_ANCHOR = 'end_voice_session("厂商页确认键", send=True)'


# ── 小工具 ──────────────────────────────────────────────────────────────────
def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8", errors="replace"))


def _func(tree: ast.Module, name: str) -> ast.AST | None:
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _line_of(tree: ast.Module, needle: str) -> int | None:
    """`needle` 在源码里的行号（按 ast.unparse 出来的语句找，避免注释干扰）。

    ⚠ 为什么不用字符串搜索：`_OK_ENDS_VOICE_ANCHOR` 那句话在**注释里也出现过**
      （`_on_hid_button` 上方的说明文字），字符串搜索会命中注释那一行 ⇒
      行号比较得出"顺序正确"的假绿。
    ⚠ 为什么要归一引号：`ast.unparse` 把双引号渲染成单引号，拿带双引号的字面量
      去比对会永远判红 —— 本闸第一版就是这么错的（和 `check_audio_agc.py`
      踩的是同一个坑）。
    ⚠ 为什么要跳过函数/类定义：`ast.FunctionDef` **本身就是** `ast.stmt`，
      而它的 unparse 包含整个函数体 ⇒ 两个锚点都会命中函数定义那一行，
      行号比较得出"顺序相同"的假绿（本闸第一版就是这么错的）。
    """
    want = needle.replace('"', "'")
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(n, ast.stmt):
            try:
                if want in ast.unparse(n).replace('"', "'"):
                    return n.lineno
            except Exception:                            # noqa: BLE001
                continue
    return None


def _call_inside_lock(func: ast.AST, attr: str) -> bool:
    """`self.<attr>(...)` 有没有出现在某个 `with self._lock:` 块**里面**？

    ⚠ 这是本闸最重要的一条**结构性**断言：`SendInput` 是系统调用，持锁调它
      会把 `release()`（BLE 回调线程在等）卡住 —— 现象是"手都松了指针还在走"。
      这种 bug 在真机上极难复现（要靠时序），只能靠结构钉死。
    """
    locked_ids: set[int] = set()
    for n in ast.walk(func):
        if not isinstance(n, ast.With):
            continue
        if not any(_is_self_lock(i.context_expr) for i in n.items):
            continue
        for sub in ast.walk(n):
            locked_ids.add(id(sub))
    for n in ast.walk(func):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == attr
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "self"):
            if id(n) in locked_ids:
                return True
    return False


def _is_self_lock(expr: ast.AST) -> bool:
    return (isinstance(expr, ast.Attribute) and expr.attr == "_lock"
            and isinstance(expr.value, ast.Name) and expr.value.id == "self")


def _move_injection_into_lock(src: str) -> str | None:
    """把 `try: self._move(...)` 改写成 `with self._lock: self._move(...)`。

    ⚠ 光换 `try:` 那一行会留下一个**悬空的 except** ⇒ 坏样本本身就语法错，
      闸门会以 SyntaxError 崩掉而不是"判据变红"。所以紧跟的 `except ...:` 也得
      换成 `if True:`（它原来的函数体正好当 if 的体，缩进天然合法）。
      失败返回 None（锚点变了），由调用方报出来。
    """
    lines = src.splitlines(keepends=True)
    idx = next((i for i, l in enumerate(lines) if l.strip() == "self._move(ix, iy)"),
               None)
    if idx is None or idx == 0 or "try:" not in lines[idx - 1]:
        return None
    lines[idx - 1] = "        with self._lock:\n"
    for j in range(idx + 1, min(idx + 4, len(lines))):
        if lines[j].lstrip().startswith("except Exception as e:"):
            lines[j] = "        if True:\n"
            break
    else:
        return None
    return "".join(lines)


def _drop_dict_entry(src: str, key: str) -> str | None:
    """把 `"<key>": ...` 这一项从字面量字典里整段删掉（含续行）。

    ⚠ 不能只删第一行 —— 那会留下半个括号，坏样本直接语法错（本闸第一版
      就是这么崩的）。所以按括号配平把续行一起带走。
    """
    lines = src.splitlines(keepends=True)
    for i, l in enumerate(lines):
        if f'"{key}":' not in l:
            continue
        depth = l.count("(") + l.count("{") - l.count(")") - l.count("}")
        j = i
        while depth > 0 and j + 1 < len(lines):
            j += 1
            depth += (lines[j].count("(") + lines[j].count("{")
                      - lines[j].count(")") - lines[j].count("}"))
        del lines[i:j + 1]
        return "".join(lines)
    return None


# ── 1. 引擎真值表（纯逻辑，不碰硬件）───────────────────────────────────────
class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def adv(self, dt: float) -> None:
        self.t += dt


class _Recorder:
    """记下每一次注入调用（dx, dy）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, dx: int, dy: int):
        self.calls.append((int(dx), int(dy)))
        return True

    def total(self) -> tuple[int, int]:
        return (sum(d[0] for d in self.calls), sum(d[1] for d in self.calls))

    def clear(self) -> None:
        self.calls.clear()


def _mk(**kw):
    import mouse_mode
    clk = kw.pop("clock", None) or _Clock()
    rec = kw.pop("rec", None) or _Recorder()
    mv = mouse_mode.MouseMover(move=rec, clock=clk, autostart=False, **kw)
    return mv, clk, rec


def check_engine(verbose: bool = True) -> bool:
    ok = True
    import mouse_mode
    TICK = 1.0 / 60.0

    # T1：按下 → 指针往对的方向走；松开 → 立刻停
    mv, clk, rec = _mk(base_speed=6.0, max_speed=6.0, accel_ms=0)
    mv.press("right")
    for _ in range(10):
        clk.adv(TICK)
        mv.tick()
    dx, dy = rec.total()
    if dx <= 0 or dy != 0:
        print(f"   ❌ T1 按「方向右」10 帧后位移是 ({dx}, {dy}) —— 方向不对")
        ok = False
    else:
        print(f"   OK T1 按「方向右」10 帧 → dx={dx} dy={dy}（方向正确）")

    rec.clear()
    mv.release("right")
    for _ in range(10):
        clk.adv(TICK)
        mv.tick()
    if rec.calls:
        print(f"   ❌ T1b 松开之后还发了 {len(rec.calls)} 次位移 —— 指针不会停")
        ok = False
    else:
        print("   OK T1b 松开之后一次都不再发（指针真的停）")

    # T2：四个方向都得对上（屏幕坐标系 y 向下为正）
    expect = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
    bad = []
    for name, (ex, ey) in expect.items():
        mv, clk, rec = _mk(base_speed=10.0, max_speed=10.0, accel_ms=0)
        mv.press(name)
        for _ in range(6):
            clk.adv(TICK)
            mv.tick()
        tx, ty = rec.total()
        if (tx == 0 and ex != 0) or (ty == 0 and ey != 0) \
                or (ex == 0 and tx != 0) or (ey == 0 and ty != 0):
            bad.append(f"{name}→({tx},{ty})")
        elif (tx > 0) != (ex > 0) or (ty > 0) != (ey > 0):
            bad.append(f"{name}→({tx},{ty})")
    if bad:
        print(f"   ❌ T2 方向映射错了：{bad}")
        ok = False
    else:
        print("   OK T2 四个方向都对（up=屏幕上方）")

    # T3：亚像素余量 —— 速度小于 1 px/帧 时，多帧之后**必须真的动**
    #     老写法 `int(v*step)` 每帧都是 0 ⇒ 慢慢推指针纹丝不动。
    mv, clk, rec = _mk(base_speed=0.2, max_speed=0.2, accel_ms=0)
    mv.press("right")
    for _ in range(30):
        clk.adv(TICK)
        mv.tick()
    tx, _ = rec.total()
    if tx <= 0:
        print(f"   ❌ T3 速度 0.2 px/帧、30 帧（=6 px）之后位移还是 {tx} —— "
              f"没有亚像素累加，慢慢推指针会纹丝不动")
        ok = False
    else:
        print(f"   OK T3 0.2 px/帧 × 30 帧 → 走了 {tx} px（余量真的累加了）")

    # T4：加速度曲线 —— 起步=base、到点=max；accel_ms=0 ⇒ 固定 base
    mv, clk, rec = _mk(base_speed=4.0, max_speed=20.0, accel_ms=1000)
    if abs(mv._frame_step(0.0) - 4.0) > 1e-6:
        print(f"   ❌ T4 按住 0 秒时每帧 {mv._frame_step(0.0):.2f} ≠ 起步速度 4.0")
        ok = False
    elif abs(mv._frame_step(1.0) - 20.0) > 1e-6:
        print(f"   ❌ T4 按住 1 秒（= accel_ms）时每帧 {mv._frame_step(1.0):.2f} ≠ 上限 20.0")
        ok = False
    elif abs(mv._frame_step(5.0) - 20.0) > 1e-6:
        print("   ❌ T4 超过加速时间后还在涨 —— 曲线没有封顶")
        ok = False
    else:
        print(f"   OK T4 加速曲线：0s→{mv._frame_step(0.0):.1f}、"
              f"0.5s→{mv._frame_step(0.5):.1f}、1s→{mv._frame_step(1.0):.1f}"
              f"（起步慢、到点封顶）")

    mv0, _, _ = _mk(base_speed=7.0, max_speed=20.0, accel_ms=0)
    if abs(mv0._frame_step(9.9) - 7.0) > 1e-6:
        print("   ❌ T4b accel_ms=0 时速度不是固定的 —— 「不做加速」没生效")
        ok = False
    else:
        print("   OK T4b accel_ms=0 ⇒ 固定速度（不做加速这条路真的存在）")

    # T5：丢松开的兜底 —— 按住超时没事件 ⇒ 必须自己停，且记一笔诊断
    mv, clk, rec = _mk(base_speed=8.0, max_speed=8.0, accel_ms=0)
    mv.press("left")
    clk.adv(TICK)
    mv.tick()
    rec.clear()
    clk.adv(mouse_mode.MAX_HOLD_SECONDS + 0.1)       # 松开永远没来
    mv.tick()
    if rec.calls:
        print("   ❌ T5 超时之后还发了一次位移 —— 兜底没生效")
        ok = False
    elif mv.stuck_stops() < 1:
        print("   ❌ T5 超时停下了，但没记 stuck_stops —— 排查时看不到「发生过」")
        ok = False
    elif mv.active():
        print("   ❌ T5 超时之后 directions 还非空 —— 状态没清干净")
        ok = False
    else:
        print(f"   OK T5 丢松开 {mouse_mode.MAX_HOLD_SECONDS:.0f} 秒后自动停下"
              f"（stuck_stops={mv.stuck_stops()}，日志会解释原因）")

    # T6：`release_all()` 是丢松开的**主路径**兜底（main.py 在每个新键按下时调它）
    mv, clk, rec = _mk(base_speed=8.0, max_speed=8.0, accel_ms=0)
    mv.press("up")
    clk.adv(TICK)
    mv.tick()
    mv.release_all()                                  # "新键按下 ⇒ 旧的已经不在按了"
    rec.clear()
    clk.adv(TICK)
    mv.tick()
    if rec.calls or mv.active():
        print("   ❌ T6 release_all() 之后还在移动 —— 「按住 A 再按 B」会失控")
        ok = False
    else:
        print("   OK T6 release_all() 立刻清干净（新键按下时的主路径兜底）")

    # T7：两个方向同时"被按住"（只可能来自丢松开）⇒ 不许超速
    mv, clk, rec = _mk(base_speed=10.0, max_speed=10.0, accel_ms=0)
    mv.press("right")
    mv.press("down")
    for _ in range(10):
        clk.adv(TICK)
        mv.tick()
    tx, ty = rec.total()
    # 不衰减的话斜着是 √2 倍：10 帧 × 10px = 100，斜着会是 ~141
    if max(tx, ty) > 100 + 2:
        print(f"   ❌ T7 两个方向同时按住时单轴走了 {tx}/{ty} —— 斜着会快 √2 倍")
        ok = False
    elif tx <= 0 or ty <= 0:
        print(f"   ❌ T7 两个方向没同时生效（{tx}/{ty}）—— 衰减写过头了")
        ok = False
    else:
        print(f"   OK T7 两方向同时按住 → ({tx}, {ty})，单轴没超过 100（有衰减）")

    # T8：**切换方向**时余量清零 —— 否则反向时会"欠着"几个像素
    mv, clk, rec = _mk(base_speed=0.4, max_speed=0.4, accel_ms=0)
    mv.press("right")
    for _ in range(5):
        clk.adv(TICK)
        mv.tick()
    mv.release("right")
    mv.press("left")
    rec.clear()
    for _ in range(30):
        clk.adv(TICK)
        mv.tick()
    tx, _ = rec.total()
    if tx >= 0:
        print(f"   ❌ T8 反向后位移是 {tx}（应为负）—— 余量没清零，反向会欠账")
        ok = False
    else:
        print(f"   OK T8 换方向后余量清零（反向 30 帧走了 {tx} px）")

    # T9：空方向 / 未知方向不许炸、也不许发事件
    mv, clk, rec = _mk()
    mv.press("nonsense")
    mv.release("nonsense")
    clk.adv(TICK)
    mv.tick()
    if rec.calls:
        print("   ❌ T9 未知方向居然发出了位移")
        ok = False
    else:
        print("   OK T9 未知方向被安全忽略")

    # T10：`active()` / `directions()` 跟着按下松开走（UI 靠它显示）
    mv, clk, rec = _mk()
    if mv.active():
        print("   ❌ T10 刚建出来就是 active")
        ok = False
    else:
        mv.press("up")
        mv.press("left")
        if sorted(mv.directions()) != ["left", "up"]:
            print(f"   ❌ T10 directions() 返回 {mv.directions()}，应为 ['left','up']")
            ok = False
        else:
            mv.release("up")
            if mv.directions() != ["left"] or not mv.active():
                print(f"   ❌ T10 松开一个之后 directions()={mv.directions()}")
                ok = False
            else:
                print("   OK T10 active()/directions() 与按下松开一致")

    # T11：`stop()` 必须把"还按着的方向"松开（退出时指针不能还在跑）
    mv, clk, rec = _mk()
    mv.press("down")
    mv.stop()
    if mv.active():
        print("   ❌ T11 stop() 之后 directions 还非空 —— 退出后指针会继续跑")
        ok = False
    else:
        print("   OK T11 stop() 会先松开所有方向（退出时指针不会自己动）")

    return ok


# ── 2. 结构性：注入必须在锁外 ───────────────────────────────────────────────
def check_no_inject_under_lock(verbose: bool = True) -> bool:
    tree = _tree(ENGINE)
    tick = _func(tree, "tick")
    if tick is None:
        print("   ❌ mouse_mode.py 里找不到 tick()")
        return False
    if _call_inside_lock(tick, "_move"):
        print("   ❌ tick() 里 `self._move(...)` 出现在 `with self._lock:` 块内 —— "
              "`SendInput` 是系统调用，持锁调它会把 `release()`（BLE 回调线程在等）"
              "卡住 ⇒ 现象是「手都松了指针还在走」")
        return False
    if verbose:
        print("   OK tick() 的注入在锁外（松手不会被 SendInput 卡住）")
    return True


# ── 3. 接线断言（引擎再对，接错地方也白搭）──────────────────────────────────
def check_wiring(verbose: bool = True) -> bool:
    ok = True
    tree = _tree(MAIN)

    # W1：优先级 —— 鼠标模式必须排在「语音会话中按确认键 = 收尾」**之后**
    hid = _func(tree, "_on_hid_button")
    if hid is None:
        print("   ❌ main.py 里找不到 _on_hid_button")
        return False
    l_ok = _line_of(hid, _OK_ENDS_VOICE_ANCHOR)
    l_mouse = _line_of(hid, "_mouse_handle(")
    if l_ok is None:
        print("   ❌ _on_hid_button 里找不到「语音会话中按确认键 = 收尾」那条 —— "
              "判据失去了参照物")
        ok = False
    elif l_mouse is None:
        print("   ❌ _on_hid_button 里没有调用 _mouse_handle —— 鼠标模式没接上")
        ok = False
    elif l_mouse < l_ok:
        print(f"   ❌ 鼠标模式（第 {l_mouse} 行）排在「语音会话中按确认键收尾」"
              f"（第 {l_ok} 行）**前面** —— 说话时误按确认键会点出一发左键")
        ok = False
    elif verbose:
        print(f"   OK 优先级正确：先判语音收尾（L{l_ok}），再走鼠标模式（L{l_mouse}）")

    # W2：`_get_cfg()` 白名单必须含全部 mouse_* 字段（不加就**静默**不生效）
    getcfg = _func(tree, "_get_cfg")
    if getcfg is None:
        print("   ❌ main.py 里找不到 _get_cfg")
        ok = False
    else:
        src = ast.unparse(getcfg)
        missing = [k for k in ("mouse_mode_enabled", "mouse_speed",
                               "mouse_speed_max", "mouse_accel_ms",
                               "mouse_idle_exit_s") if k not in src]
        if missing:
            print(f"   ❌ _get_cfg() 的缓存白名单缺 {missing} —— "
                  f"控制台改了滑块也**不会生效**，而且会静默退回默认值")
            ok = False
        elif verbose:
            print("   OK _get_cfg() 白名单含全部 5 个 mouse_* 字段（热生效）")

    # W3：main.py 里必须真的调用引擎的三个动作
    allmain = ast.unparse(tree)
    for need, why in (
        ("_mover.press(", "方向键按下没有驱动引擎"),
        ("_mover.release(", "方向键松开没有停引擎"),
        ("_mover.release_all(", "新键按下时没有清掉旧方向（丢松开会失控）"),
        ("mouse_click(", "确认/返回键没有接成鼠标左右键"),
        ("mouse_scroll(", "音量键没有接成滚轮"),
    ):
        if need not in allmain:
            print(f"   ❌ main.py 里没有 `{need}` —— {why}")
            ok = False
    if ok and verbose:
        print("   OK press/release/release_all/click/scroll 都接上了")

    # W4：收尾登记 —— 引擎必须挂进 _BridgeResources 并在 teardown 里 stop()
    bres = _func(tree, "teardown")
    if bres is None or "mouse_mover" not in ast.unparse(bres):
        print("   ❌ _BridgeResources.teardown 里没有停 mouse_mover —— "
              "退出后那条 60Hz 线程还在，指针会自己动")
        ok = False
    elif "mouse_mover" not in ast.unparse(_func(tree, "__init__") or ast.Module()):
        print("   ❌ _BridgeResources.__init__ 里没有声明 self.mouse_mover")
        ok = False
    elif verbose:
        print("   OK 引擎登记进 _BridgeResources，teardown 会停它")

    # W5：切换键必须**认映射表**（写死 btn_id == "input" 的话，
    #     用户把「信源」改回 Alt+Tab 之后就再也切不回来了）
    mouse_fn = _func(tree, "_mouse_handle")
    if mouse_fn is None:
        print("   ❌ main.py 里找不到 _mouse_handle")
        ok = False
    else:
        msrc = ast.unparse(mouse_fn)
        if '"mouse_mode"' not in msrc and "'mouse_mode'" not in msrc:
            print("   ❌ _mouse_handle 里没有按 `mouse_mode` 这个映射值判断切换键 —— "
                  "写死按键的话，用户改过映射就切不进/切不出了")
            ok = False
        elif verbose:
            print("   OK 切换键认映射值 mouse_mode（换到哪个键上都能用）")

    # W6：config.py 三处必须一致
    ctext = CONFIG.read_text(encoding="utf-8", errors="replace")
    if '"mouse_mode"' not in ctext:
        print("   ❌ config.py 里没有 mouse_mode 这个映射目标")
        ok = False
    if "CONFIG_VERSION = 4" not in ctext:
        print("   ❌ config.py 的 CONFIG_VERSION 不是 4 —— "
              "老配置不会走 v4 迁移，「信源」键还是 Alt+Tab")
        ok = False
    if '"input":   "mouse_mode"' not in ctext:
        print("   ❌ DEFAULT_KEYMAP 里「信源」的默认值不是 mouse_mode")
        ok = False
    if "if from_version < 4:" not in ctext:
        print("   ❌ config.py 里没有 `if from_version < 4:` 的迁移分支")
        ok = False
    if ok and verbose:
        print("   OK config.py：目标表 / 默认表 / CONFIG_VERSION=4 / v4 迁移 四处齐了")

    # W7：state.py 必须暴露 mouse_mode（UI 靠它显示，否则就是"无感切换"）
    stext = STATE.read_text(encoding="utf-8", errors="replace")
    if "mouse_mode:      bool" not in stext:
        print("   ❌ state.py 的 BridgeState 里没有 mouse_mode 字段 —— "
              "托盘/控制台没法显示当前模式，用户不知道自己在哪个模式里")
        ok = False
    elif verbose:
        print("   OK state.py 暴露 mouse_mode（托盘/控制台能显示）")

    # W8：进入鼠标模式要先收掉语音（否则一边说话一边被推指针）
    mset = _func(tree, "_mouse_set")
    if mset is None:
        print("   ❌ main.py 里找不到 _mouse_set")
        ok = False
    else:
        ssrc = ast.unparse(mset)
        if "end_voice_session" not in ssrc or "voice_active" not in ssrc:
            print("   ❌ _mouse_set 进入时没有先收掉语音会话 —— "
                  "会一边说话一边被方向键推指针")
            ok = False
        elif verbose:
            print("   OK 进入鼠标模式会先收掉语音会话（两者互斥）")

    return ok


# ── 4. 反例自证（判据真的会红吗）────────────────────────────────────────────
def check_counter_examples(verbose: bool = True) -> bool:
    ok = True
    print("\n── 反例自证（把锚点改坏，判据必须变红）──")

    # 反例 1：把注入搬进锁里 → 结构性判据必须红
    esrc = ENGINE.read_text(encoding="utf-8", errors="replace")
    if "self._move(ix, iy)" not in esrc:
        print("   ❌ [反例] 找不到 tick() 里 `self._move(ix, iy)` 的锚点")
        ok = False
    else:
        # 把 `try: self._move(...)` 改写成 `with self._lock: self._move(...)`，
        # 顺手把紧跟的 `except ...:` 换成 `if True:`（不然是悬空的 except，语法错）。
        broken = _move_injection_into_lock(esrc)
        if broken is None:
            print("   ❌ [反例] 构造「持锁注入」的坏样本失败（锚点变了）")
            ok = False
        elif not _call_inside_lock(_func(ast.parse(broken), "tick"), "_move"):
            print("   ❌ [反例] 把注入搬进锁里却判不出来 → 判据无效")
            ok = False
        else:
            print("   OK [反例] 注入搬进 `with self._lock` → 结构性判据变红")

    # 反例 2：去掉亚像素累加 → T3 必须红
    b2 = esrc.replace(
        "            self._carry_x += vx * step",
        "            self._carry_x = 0.0 * step", 1)
    if b2 == esrc:
        print("   ❌ [反例] 找不到亚像素累加的锚点")
        ok = False
    else:
        mod = {}
        exec(compile(b2, "<counter-example>", "exec"), mod)   # noqa: S102
        clk = _Clock()
        rec = _Recorder()
        mv = mod["MouseMover"](move=rec, clock=clk, autostart=False,
                               base_speed=0.2, max_speed=0.2, accel_ms=0)
        mv.press("right")
        for _ in range(30):
            clk.adv(1.0 / 60.0)
            mv.tick()
        if sum(d[0] for d in rec.calls) > 0:
            print("   ❌ [反例] 去掉亚像素累加后 T3 仍判绿 → 判据无效")
            ok = False
        else:
            print("   OK [反例] 去掉亚像素累加 → T3 变红（慢慢推真的会不动）")

    # 反例 3：去掉丢松开的兜底 → T5 必须红
    b3 = esrc.replace("            if (now - self._last_event) > MAX_HOLD_SECONDS:",
                      "            if False:", 1)
    if b3 == esrc:
        print("   ❌ [反例] 找不到 MAX_HOLD_SECONDS 的锚点")
        ok = False
    else:
        mod = {}
        exec(compile(b3, "<counter-example>", "exec"), mod)   # noqa: S102
        clk = _Clock()
        rec = _Recorder()
        mv = mod["MouseMover"](move=rec, clock=clk, autostart=False,
                               base_speed=8.0, max_speed=8.0, accel_ms=0)
        mv.press("left")
        clk.adv(1.0 / 60.0)
        mv.tick()
        clk.adv(mod["MAX_HOLD_SECONDS"] + 0.1)
        mv.tick()
        if not mv.active():
            print("   ❌ [反例] 去掉兜底后 T5 仍判绿 → 判据无效")
            ok = False
        else:
            print("   OK [反例] 去掉丢松开兜底 → T5 变红（指针会一直跑）")

    # 反例 4：把鼠标模式挪到语音收尾**前面** → W1 必须红
    msrc = MAIN.read_text(encoding="utf-8", errors="replace")
    block = """        # ── 鼠标模式（v1.0.31）──────────────────────────────────────────────
        # ⚠ 位置**必须**在上面那条「语音会话中按确认键 = 收尾」之后：
        #   正在说话时误按确认键，应该是"关掉语音"，而不是点出一发左键
        #   （否则用户会对着聊天窗口乱点）。
        try:
            if _mouse_handle(btn_id, is_down, cached.get("keymap") or {}):
                return
        except Exception as e:                      # noqa: BLE001
            # 鼠标模式出问题**不能**把按键派发整条掐掉 —— 记一行、放它走普通映射。
            logger.error("鼠标模式派发异常（这一下按键已忽略）：%s", e)

"""
    if block not in msrc:
        print("   ❌ [反例] 找不到 main.py 里鼠标模式那一段的锚点")
        ok = False
    else:
        b4 = msrc.replace(block, "", 1)              # 直接删掉 → 找不到调用点
        hid = _func(ast.parse(b4), "_on_hid_button")
        if _line_of(hid, "_mouse_handle(") is not None:
            print("   ❌ [反例] 删掉 _mouse_handle 调用后仍判成有 → 判据无效")
            ok = False
        else:
            print("   OK [反例] 删掉 _mouse_handle 调用 → W1 变红")

        # 更真实的一种：把它挪到语音收尾前面
        b5 = msrc.replace(block, "", 1).replace(
            "        # 语音会话中按「确认键」= 收尾。",
            block + "        # 语音会话中按「确认键」= 收尾。", 1)
        hid5 = _func(ast.parse(b5), "_on_hid_button")
        l_ok5 = _line_of(hid5, _OK_ENDS_VOICE_ANCHOR)
        l_m5 = _line_of(hid5, "_mouse_handle(")
        if l_ok5 is None or l_m5 is None or not (l_m5 < l_ok5):
            print("   ❌ [反例] 把鼠标模式挪到语音收尾前面后，顺序判据没变红 → 判据无效")
            ok = False
        else:
            print(f"   OK [反例] 鼠标模式挪到语音收尾前 → W1 变红"
                  f"（L{l_m5} < L{l_ok5}）")

    # 反例 5：白名单里删掉一个 mouse_* 字段 → W2 必须红
    b6 = _drop_dict_entry(msrc, "mouse_accel_ms")
    if b6 is None:
        print("   ❌ [反例] 找不到白名单 mouse_accel_ms 的锚点")
        ok = False
    else:
        src6 = ast.unparse(_func(ast.parse(b6), "_get_cfg"))
        if "mouse_accel_ms" in src6:
            print("   ❌ [反例] 删掉白名单字段后仍判成有 → 判据无效")
            ok = False
        else:
            print("   OK [反例] 删掉白名单字段 → W2 变红（会静默不生效）")

    return ok


def main() -> int:
    print("=" * 60)
    print("鼠标模式闸 —— 方向键推指针：四条硬约束 + 一张真值表")
    print("=" * 60)

    print("\n── 1. 引擎真值表（纯逻辑，不碰硬件）──")
    ok = check_engine()

    print("\n── 2. 结构性：注入必须在锁外 ──")
    ok = check_no_inject_under_lock() and ok

    print("\n── 3. 接线断言 ──")
    ok = check_wiring() and ok

    ok = check_counter_examples() and ok

    print("\n" + ("✅ 通过" if ok else "❌ 失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
