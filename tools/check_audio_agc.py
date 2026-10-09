"""自动增益（AGC）闸 —— 「会自己收，但绝不乱动你的设置」。

为什么单独一条闸
----------------
`CHANGELOG.md` v1.0.22 的结论是「固定增益怎么选都是错的」：遥控器解码后的
峰值本来就能顶到满量程（真机实测 `29459` ≈ 90%），用户设 `gain = 10`
就整段被软限幅压平，语音识别明显退化；设成 1 又太小声。

AGC 是那个结论的自然延伸 —— **持续限幅就小步下调，长期不顶格再慢慢回调**。

但它一出生就带着"程序偷偷动我设置"的风险，所以有三条**硬约束**，
少一条都会让用户觉得软件在跟他作对：

  ① 只调**遥控器那一路**，不碰系统麦克风（那是用户自己的设备）；
  ② 系数**只降不升过 1.0** ⇒ 用户设的增益永远是**上限**，AGC 绝不偷偷放大；
  ③ 有效增益**不低于 1.0** ⇒ 再糟也不会收到听不见。

还有一条**性能**约束，最容易被后来的改动踩掉：

  ④ `_agc_settle()` 跑在**音频回调线程**里（约每 15ms 一次）——
     它**不许打日志、不许写 state**。回调里写文件/拿锁会让声音抖，
     用户听到的是爆音。要播报的东西先记进 `_agc["pending_log"]`，
     由主循环去打印。

用法
----
    python tools/check_audio_agc.py

退出码 0 = 通过；1 = 失败。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MAIN = ROOT / "main.py"
CONSOLE = ROOT / "console_server.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8", errors="replace"))


def _func(tree: ast.Module, name: str) -> ast.AST | None:
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _attr_calls(func: ast.AST, root_name: str) -> list[str]:
    """`root_name.xxx(...)` 形式的调用，返回 `xxx` 列表。"""
    out: list[str] = []
    for n in ast.walk(func):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == root_name):
            out.append(n.func.attr)
    return out


def _text_of(func: ast.AST) -> str:
    return ast.unparse(func)


def _assigns(func: ast.AST, target_repr: str, value_contains: str) -> bool:
    """函数体里有没有 `target = <含 value_contains 的表达式>`。

    ⚠ 为什么要用 ast 而不是字符串匹配：`ast.unparse` 把 `_agc["enabled"]`
      输出成 `_agc['enabled']`（单引号）。拿双引号的字面量去匹配会永远判红
      —— 本闸第一版就是这么错的。而且字符串匹配还会被"两处各出现一半"骗过。
    """
    for n in ast.walk(func):
        if isinstance(n, ast.Assign) and len(n.targets) == 1:
            try:
                if (ast.unparse(n.targets[0]) == target_repr
                        and value_contains in ast.unparse(n.value)):
                    return True
            except Exception:                            # noqa: BLE001
                continue
    return False


# ── 1. 三条硬约束 ───────────────────────────────────────────────────────────
def check_constraints(verbose: bool = True) -> bool:
    ok = True
    tree = _tree(MAIN)
    settle = _func(tree, "_agc_settle")
    if settle is None:
        print("   ❌ main.py 里找不到 _agc_settle")
        return False
    src = _text_of(settle)

    # ② 上限必须是 1.0（只降不升过用户设定值）
    if "min(1.0, f * _AGC_STEP_UP)" not in src:
        print("   ❌ _agc_settle 里没有 `min(1.0, f * _AGC_STEP_UP)` —— "
              "少了这个上限，AGC 会**偷偷把用户设的增益放大**")
        ok = False
    elif verbose:
        print("   OK 系数上限锁在 1.0（用户设的增益永远是上限，不会偷偷放大）")

    # ③ 有效增益下限
    if "_AGC_MIN_EFF_GAIN" not in src:
        print("   ❌ _agc_settle 里没有引用 _AGC_MIN_EFF_GAIN —— "
              "没有下限时 AGC 会一路收到听不见")
        ok = False
    elif verbose:
        print("   OK 有有效增益下限（再糟也不会收到听不见）")

    # ① 只调遥控器那一路：r_gain 乘 factor，s_gain 不乘
    mix = _func(tree, "_create_stream")
    if mix is None:
        print("   ❌ 找不到 _create_stream")
        return False
    mtext = _text_of(mix)
    if 'r_gain = float(mx["remote_gain"]) * _agc["factor"]' not in mtext.replace("'", '"'):
        print("   ❌ 遥控器那一路的增益没有乘 `_agc[\"factor\"]` —— AGC 就白算了")
        ok = False
    elif "_agc" in mtext.split('s_gain =')[1].split("\n")[0]:
        print("   ❌ 系统麦克风那一路也乘了 AGC 系数 —— 那是用户自己的设备，"
              "它太响该由用户自己调；AGC 碰它就是错的因果")
        ok = False
    elif verbose:
        print("   OK AGC 只乘在遥控器那一路，不碰系统麦克风")

    return ok


# ── 2. 回调线程不许打日志 / 写 state（声音会抖）──────────────────────────────
def check_no_side_effects(verbose: bool = True) -> bool:
    ok = True
    tree = _tree(MAIN)
    settle = _func(tree, "_agc_settle")
    if settle is None:
        print("   ❌ main.py 里找不到 _agc_settle")
        return False

    logs = _attr_calls(settle, "logger")
    states = _attr_calls(settle, "state")
    if logs:
        print(f"   ❌ _agc_settle 里调了 logger.{logs} —— 它跑在**音频回调线程**上"
              f"（约每 15ms 一次），在那里写文件会让声音抖（用户听到的是爆音）。"
              f"要播报就先记进 _agc[\"pending_log\"]，由主循环去打印")
        ok = False
    if states:
        print(f"   ❌ _agc_settle 里调了 state.{states} —— 同样在音频回调线程上，"
              f"拿锁会让声音抖。状态同步交给主循环")
        ok = False
    if not logs and not states and verbose:
        print("   OK _agc_settle 是纯算术（不打日志、不写 state —— 回调线程安全）")

    # 播报通道必须存在，且由主循环消费
    main_fn = _func(tree, "_run_bridge_inner")
    if main_fn is None or "pending_log" not in _text_of(main_fn):
        print("   ❌ 主循环里没有消费 `_agc[\"pending_log\"]` —— "
              "AGC 收了增益却一句话都不说，用户只会觉得「声音怎么变小了」")
        ok = False
    elif verbose:
        print("   OK 播报走 pending_log，由主循环消费（回调零副作用）")

    return ok


# ── 3. 开关真的接上了（config → 回调 / 控制台白名单）────────────────────────
def check_switch(verbose: bool = True) -> bool:
    ok = True
    cfg = (ROOT / "config.py").read_text(encoding="utf-8", errors="replace")
    if "auto_gain:" not in cfg or "= True" not in cfg.split("auto_gain:")[1][:40]:
        print("   ❌ config.py 里没有 `auto_gain: bool = True`")
        ok = False
    elif verbose:
        print("   OK config.py 有 auto_gain（默认开）")

    tree = _tree(MAIN)
    main_fn = _func(tree, "_run_bridge_inner")
    if main_fn is None or not _assigns(main_fn, "_agc['enabled']", "cfg.auto_gain"):
        print("   ❌ _run_bridge_inner 没有把 cfg.auto_gain 灌进 _agc[\"enabled\"] —— "
              "配置里的开关就成了摆设")
        ok = False
    elif verbose:
        print("   OK cfg.auto_gain 真的灌进了 _agc[\"enabled\"]")

    ctext = CONSOLE.read_text(encoding="utf-8", errors="replace")
    if '"auto_gain": bool' not in ctext:
        print("   ❌ console_server.py 的 /api/config 白名单里没有 auto_gain —— "
              "界面上关了、后端还在自动收你的增益（方向相反，最难查）")
        ok = False
    elif verbose:
        print("   OK /api/config 白名单里有 auto_gain（面板一关立刻生效）")

    return ok


# ── 4. 行为级：拿真函数跑四种情形 ───────────────────────────────────────────
def check_behavior(verbose: bool = True) -> bool:
    ok = True
    try:
        import main
    except Exception as e:                                # noqa: BLE001
        print(f"   ❌ import main 失败：{type(e).__name__}: {e}")
        return False

    def run(user_gain: float, hit_ratio: float, times: int, enabled: bool = True):
        main._agc_reset()
        main._agc["enabled"] = enabled
        for i in range(times):
            main._agc["n"] = 1000
            main._agc["hit"] = int(1000 * hit_ratio)
            main._agc_settle(user_gain, float(i))
        return main._agc["factor"]

    # B1：一直被压平 → 往下收，但不跌破"有效增益 ≥ 1.0"
    f = run(10.0, 1.0, 12)
    if not (f < 0.5):
        print(f"   ❌ B1 采样 100% 被压平时系数只到 {f:.3f} —— 没有真的往下收")
        ok = False
    elif f < 0.1 - 1e-9:
        print(f"   ❌ B1 系数跌到 {f:.3f}，有效增益 {(f * 10):.2f} < 1.0 —— "
              f"越过了下限，用户会觉得「怎么没声了」")
        ok = False
    elif verbose:
        print(f"   OK B1 压平时往下收（系数 {f:.3f}，有效增益 {f * 10:.2f}）")

    # B2：长期安静 → 慢慢回调，但**绝不越过 1.0**
    f = run(10.0, 0.0, 400)
    if f > 1.0 + 1e-9:
        print(f"   ❌ B2 系数涨到 {f:.4f} > 1.0 —— **AGC 偷偷放大了**用户设的增益")
        ok = False
    elif verbose:
        print(f"   OK B2 长期安静也只回到 {f:.3f}（上限 1.0，绝不偷偷放大）")

    # B3：用户自己就把增益设得比下限还小 → AGC 完全不介入
    f = run(0.5, 1.0, 12)
    if abs(f - 1.0) > 1e-9:
        print(f"   ❌ B3 用户增益 0.5 时系数变成了 {f:.3f} —— "
              f"用户已经设得很小了，AGC 不该再压")
        ok = False
    elif verbose:
        print("   OK B3 用户增益本来就小于下限 → AGC 完全不介入")

    # B4：关掉开关 → 一动不动
    f = run(10.0, 1.0, 12, enabled=False)
    if abs(f - 1.0) > 1e-9:
        print(f"   ❌ B4 关掉 auto_gain 后系数还是变成了 {f:.3f} —— "
              f"「关掉」必须是真的关掉")
        ok = False
    elif verbose:
        print("   OK B4 关掉开关后一动不动（factor 恒为 1.0）")

    # B5：播报是"排队"的，不是回调里直接打
    main._agc_reset()
    main._agc["enabled"] = True
    main._agc["pending_log"] = None
    for i in range(6):
        main._agc["n"] = 1000
        main._agc["hit"] = 1000
        main._agc_settle(10.0, float(i))
    if main._agc["pending_log"] is None:
        print("   ❌ B5 明显下调时没有往 pending_log 里排队 —— "
              "用户会听见音量变小，却没有任何解释")
        ok = False
    elif verbose:
        _p = main._agc["pending_log"]
        print(f"   OK B5 明显下调时排队播报（pct={_p[0]:.0f}%, factor={_p[1]:.2f}）")
    main._agc["pending_log"] = None
    main._agc_reset()

    return ok


# ── 5. 反例自证 ─────────────────────────────────────────────────────────────
def run_counter_examples(verbose: bool = True) -> bool:
    ok = True
    print("\n── 反例自证（判据真的会红吗）──")
    src = MAIN.read_text(encoding="utf-8", errors="replace")
    tree0 = _tree(MAIN)
    settle0 = _func(tree0, "_agc_settle")

    # 反例 1：去掉系数上限 → 约束②判据必须红
    broken = src.replace("min(1.0, f * _AGC_STEP_UP)", "f * _AGC_STEP_UP", 1)
    if broken == src:
        print("   ❌ [反例] 找不到 `min(1.0, f * _AGC_STEP_UP)` 的锚点")
        ok = False
    elif "min(1.0, f * _AGC_STEP_UP)" in _text_of(_func(_tree_src(broken), "_agc_settle")):
        print("   ❌ [反例] 去掉上限后仍判成有 → 判据无效")
        ok = False
    else:
        print("   OK [反例] 去掉系数上限 → 约束②判据变红")

    # 反例 2：在回调里打日志 → 零副作用判据必须红
    broken2 = src.replace(
        "    pct = 100.0 * hit / n\n",
        "    pct = 100.0 * hit / n\n    logger.info('pct=%s', pct)\n", 1)
    if broken2 == src:
        print("   ❌ [反例] 找不到 _agc_settle 的锚点")
        ok = False
    else:
        got = _attr_calls(_func(_tree_src(broken2), "_agc_settle"), "logger")
        if not got:
            print("   ❌ [反例] 回调里插了 logger 调用却判不出来 → 判据无效")
            ok = False
        else:
            print("   OK [反例] 回调里插 logger → 零副作用判据变红")

    # 反例 3：把 AGC 也乘到系统麦克风那一路 → 约束①判据必须红
    broken3 = src.replace(
        's_gain = float(mx["sys_gain"]) if state.source_audible("sys") else 0.0',
        's_gain = (float(mx["sys_gain"]) * _agc["factor"]\n'
        '                  if state.source_audible("sys") else 0.0)', 1)
    if broken3 == src:
        print("   ❌ [反例] 找不到 s_gain 的锚点")
        ok = False
    else:
        seg = _text_of(_func(_tree_src(broken3), "_create_stream"))
        after = seg.split("s_gain =")[1].split("\n")[0] if "s_gain =" in seg else ""
        if "_agc" not in after:
            print("   ❌ [反例] 系统麦克风也乘了 AGC 却判不出来 → 判据无效")
            ok = False
        else:
            print("   OK [反例] 系统麦克风也乘 AGC → 约束①判据变红")

    # 反例 4：白名单里删掉 auto_gain → 开关判据必须红
    ctext = CONSOLE.read_text(encoding="utf-8", errors="replace")
    broken4 = ctext.replace('"auto_gain": bool,', "", 1)
    if broken4 == ctext:
        print("   ❌ [反例] 找不到白名单里 `\"auto_gain\": bool,` 的锚点")
        ok = False
    elif '"auto_gain": bool' in broken4:
        print("   ❌ [反例] 删掉白名单字段后仍判成有 → 判据无效")
        ok = False
    else:
        print("   OK [反例] 删掉白名单字段 → 开关判据变红")

    # 自证：真实文件里 settle 确实存在（否则上面几条都是"对空文件判绿"）
    if settle0 is None:
        print("   ❌ [自证] 真实 main.py 里找不到 _agc_settle")
        ok = False
    return ok


def _tree_src(src: str) -> ast.Module:
    return ast.parse(src)


def main() -> int:
    print("=" * 60)
    print("自动增益闸 —— 会自己收，但绝不乱动你的设置")
    print("=" * 60)

    print("\n── 1. 三条硬约束 ──")
    ok = check_constraints()

    print("\n── 2. 回调线程零副作用（不许打日志 / 写 state）──")
    ok = check_no_side_effects() and ok

    print("\n── 3. 开关真的接上了 ──")
    ok = check_switch() and ok

    print("\n── 4. 行为级：四种情形 ──")
    ok = check_behavior() and ok

    ok = run_counter_examples() and ok

    print("\n" + ("✅ 通过" if ok else "❌ 失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
