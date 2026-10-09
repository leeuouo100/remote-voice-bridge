"""日志降噪闸 —— 「不许刷屏」+「不许把一件事说成两件」。

为什么单独一条闸
----------------
桥是要连着跑几天的。日志一旦被噪声撑起来，**真正的线索就被埋了** ——
而"埋在噪声里"和"根本没打"在用户眼里一模一样（都得不出结论）。

2026-10-09 真机日志里的三处实证：

  ① `⏹ Audio STOP （本次共收到 1746 个音频帧，峰值 32768）` **出现两次**
     （12:07:56,880 与 ,890），中间还夹着别的线程的日志。
     功能上无害（那个分支里两句都幂等），但排查时会**误读成"停了两次"** ——
     而"停了两次"会把下一个查日志的人引向"是不是重复收尾"，方向整个走反。

  ② `📊 HID 通道审计` 单条 250+ 字符，其中 60+ 个字符是 `c.key` 的对齐填充
     （形如 `—      0xFF80/0000 厂商自定义`），而这条每 20 秒（静默期 5 分钟）
     就会再来一条。

  ③ `🔘 程序看到一个键 …` 单条 300+ 字符，而它并不罕见（物理键盘敲字母、
     本程序自己注入的键都会走这里）。

这条闸钉四件事
--------------
1. `Audio STOP` 必须**去重**（同一段收尾只报一次，第 2 次降 debug）。
2. HID 审计的"已挂哪几路"必须走 `_short_keys()`（不带对齐填充）。
3. 那两条键名日志的**字面量长度**要有上限 —— 要加内容就先压缩别处。
4. 降噪不许把**判决能力**一起削掉（见下面 `check_honesty`）：
   闸门 `check_failure_visibility.py` 要的三句必须还在。

用法
----
    python tools/check_log_noise.py

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
RHID = ROOT / "remote_hid.py"

# 键名日志的字面量长度上限。改前的实测值：
#   `🔘 程序看到一个键 …` ≈ 300，`✅ HID 按键 …` ≈ 350。
# 上限卡在 240 —— 留了余量，但**逼着**"想加内容就先压缩别处"。
KEY_LOG_MAX = 240


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8", errors="replace"))


def _joined_len(call: ast.Call) -> int:
    """一个 logger 调用的**字面量**总长度。

    ⚠ 只算 `ast.Constant` / `ast.JoinedStr` 里的字符串，**不算**
      `FormattedValue` 里的东西 —— 否则 `getattr(e, 'scan_code', None)`
      那个 `'scan_code'` 也会被算进来（那是代码，不是文案）。
    """
    total = 0

    def add(node: ast.AST) -> None:
        nonlocal total
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            total += len(node.value)
        elif isinstance(node, ast.JoinedStr):
            for v in node.values:
                add(v)

    for a in call.args:
        add(a)
    return total


def _nearest_if_test(tree: ast.Module, target: ast.AST) -> str | None:
    """`target` 的**最近**那个祖先 `If` 的条件（unparse 后的字符串）。

    ⚠ 为什么必须取"最近"的，而不是"存在某个 If"：
      这段代码天然长在 `if voice_active: … else: <Audio STOP>` 里面 ——
      拿"在任意 If 子树里"当判据，**永远为真**，等于什么都没测。
      （本闸第一版就是这么错的：反例把去重分支删干净了，它照样判绿。）
    """
    best: ast.If | None = None
    for n in ast.walk(tree):
        if isinstance(n, ast.If) and any(x is target for x in ast.walk(n)):
            if best is None or n.lineno > best.lineno:
                best = n
    return ast.unparse(best.test) if best is not None else None


def _log_calls(tree: ast.Module) -> list[tuple[ast.Call, str, int]]:
    out = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("info", "warning", "error", "debug")):
            out.append((n, ast.unparse(n), _joined_len(n)))
    return out


# ── 1. Audio STOP 去重 ──────────────────────────────────────────────────────
def check_audio_stop_dedup(verbose: bool = True) -> bool:
    ok = True
    src = MAIN.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)

    for name in ("_last_audio_stop_at", "_last_audio_stop_frames"):
        if f"^{name}" not in src and f"\n{name} = " not in src:
            print(f"   ❌ main.py 里没有 `{name}` —— 去重的状态没地方放")
            ok = False

    target = None
    for call, text, _ln in _log_calls(tree):
        if "Audio STOP" in text and "logger.info" in text:
            target = call
            break
    if target is None:
        print("   ❌ 找不到 `⏹ Audio STOP` 的 logger.info")
        return False

    # 这条 info 必须在**去重判断**的分支里（不是"随便哪个 If"）
    cond = _nearest_if_test(tree, target)
    if cond is None or "_dup" not in cond:
        print(f"   ❌ `⏹ Audio STOP` 的 logger.info 不在去重分支里"
              f"（最近的条件是 `{cond}`）—— 遥控器关麦时会重复上报 AUDIO_STOP，"
              f"这条就会打两遍，排查时被误读成「停了两次」"
              f"（2026-10-09 真机 12:07:56,880 与 ,890）")
        ok = False
    elif verbose:
        print(f"   OK `⏹ Audio STOP` 在去重分支里（条件 `{cond}`，同一段收尾只报一次）")

    return ok


# ── 2. HID 审计不再带对齐填充 ───────────────────────────────────────────────
def check_hid_audit(verbose: bool = True) -> bool:
    ok = True
    src = RHID.read_text(encoding="utf-8", errors="replace")

    if "def _short_keys(" not in src:
        print("   ❌ remote_hid.py 里没有 `_short_keys()` —— 审计行会继续"
              "带着 `c.key` 的对齐填充（60+ 字符/条）")
        return False

    raw = '"、".join(c.key for c in self._cols)'
    if raw in src:
        print(f"   ❌ 还在用裸拼接 `{raw}` —— 判决书不需要那串对齐空格，"
              f"需要的是「挂了几路、是哪几路」")
        ok = False
    elif src.count("_short_keys(self._cols)") < 2:
        print(f"   ❌ `_short_keys(self._cols)` 只出现 "
              f"{src.count('_short_keys(self._cols)')} 次 —— 两处 heads 都要换掉，"
              f"漏一处就等于没降噪")
        ok = False
    elif verbose:
        print("   OK 两处 heads 都走 _short_keys()（去掉对齐填充）")

    return ok


# ── 3. 键名日志长度上限 ─────────────────────────────────────────────────────
def check_key_log_len(verbose: bool = True) -> bool:
    ok = True
    tree = _tree(MAIN)
    for needle in ("程序看到一个键", "HID 按键"):
        found = False
        for _call, text, ln in _log_calls(tree):
            if needle in text:
                found = True
                if ln > KEY_LOG_MAX:
                    print(f"   ❌ `{needle}` 那条日志的字面量有 {ln} 字符"
                          f"（上限 {KEY_LOG_MAX}）—— 它并不罕见（物理键盘敲字母、"
                          f"本程序注入的键都会走这里），会把日志撑长、把线索埋掉")
                    ok = False
                elif verbose:
                    print(f"   OK `{needle}` 那条 {ln} 字符（上限 {KEY_LOG_MAX}）")
                break
        if not found:
            print(f"   ❌ 找不到含 `{needle}` 的日志（改名了？闸门要跟着改）")
            ok = False
    return ok


# ── 4. 降噪不许削掉判决能力 ─────────────────────────────────────────────────
def check_honesty(verbose: bool = True) -> bool:
    """降噪最容易顺手削掉的东西 —— 那条日志**为什么存在**。

    `tools/check_failure_visibility.py::key_log_is_honest` 要的三件事：
    说清来源 / 给出"什么时候才该怀疑遥控器" / 不许退回旧误导句。
    这里复检一遍：那条日志确实还带着这三件事。
    """
    ok = True
    src = MAIN.read_text(encoding="utf-8", errors="replace")
    need = ("🔘 程序看到一个键",
            "这**不是**故障：物理键盘敲的字母",
            "只有当你**正在按遥控器上的某个键**时它才出现")
    miss = [s for s in need if s not in src]
    if miss:
        print(f"   ❌ 键名日志丢了这些关键句：{miss} —— 降噪可以，但这条日志"
              f"存在的理由就是这三句（说清来源 + 给出条件），删了等于把坑重新挖开")
        ok = False
    elif verbose:
        print("   OK 键名日志仍然说清来源 + 给出「什么时候才该怀疑遥控器」")
    return ok


# ── 5. 反例自证 ─────────────────────────────────────────────────────────────
def run_counter_examples(verbose: bool = True) -> bool:
    ok = True
    print("\n── 反例自证（判据真的会红吗）──")
    src = MAIN.read_text(encoding="utf-8", errors="replace")

    # 反例 1：退回"无条件打这条日志"（老代码的样子）→ 判据必须红
    # ⚠ 必须把 if/else **整块**换成无条件的版本 —— 只删 `if`/`else` 两行会
    #   留下 24 空格缩进的 logger.info，变成 IndentationError，反例跑不起来。
    old_block = (
        "                    if _dup_stop:\n"
        "                        logger.debug(\"⏹ Audio STOP 重复上报"
        "（同一段收尾的第 2 次，已忽略）\")\n"
        "                    else:\n"
        "                        logger.info(\n"
        "                            f\"⏹ Audio STOP （本次共收到 {_audio_frames} 个音频帧，\"\n"
        "                            f\"峰值 {_audio_peak}）\")\n")
    new_block = (
        "                    logger.info(\n"
        "                        f\"⏹ Audio STOP （本次共收到 {_audio_frames} 个音频帧，\"\n"
        "                        f\"峰值 {_audio_peak}）\")\n")
    broken = src.replace(old_block, new_block, 1)
    if broken == src:
        print("   ❌ [反例] 找不到 Audio STOP 去重块的锚点（闸门锚点过期了）")
        ok = False
    else:
        bt = ast.parse(broken)
        tgt = None
        for call, text, _ln in _log_calls(bt):
            if "Audio STOP" in text and "logger.info" in text:
                tgt = call
                break
        cond = _nearest_if_test(bt, tgt) if tgt is not None else None
        if cond is not None and "_dup" in cond:
            print("   ❌ [反例] 退回无条件打之后仍判成在去重分支里 → 判据无效")
            ok = False
        else:
            print(f"   OK [反例] 退回无条件打 → Audio STOP 判据变红"
                  f"（最近的条件变成 `{cond}`）")

    # 反例 2：HID 审计换回裸拼接 → 判据必须红
    rsrc = RHID.read_text(encoding="utf-8", errors="replace")
    if rsrc.count("_short_keys(self._cols)") < 2:
        print("   ❌ [反例] 真实文件里 _short_keys 的调用点就不足 2 处")
        ok = False
    else:
        broken2 = rsrc.replace("_short_keys(self._cols)",
                               '"、".join(c.key for c in self._cols)')
        if '"、".join(c.key for c in self._cols)' not in broken2:
            print("   ❌ [反例] 换回裸拼接后判不出来 → 判据无效")
            ok = False
        else:
            print("   OK [反例] HID 审计换回裸拼接 → 判据变红")

    # 反例 3：给键名日志加长 → 长度判据必须红
    long_tail = "才说明它还没被映射 —— 去控制台「按键映射」指定动作。"
    if long_tail not in src:
        print("   ❌ [反例] 找不到键名日志的尾巴（闸门锚点过期了）")
        ok = False
    else:
        broken3 = src.replace(long_tail, long_tail + "（补充说明）" * 20, 1)
        bt3 = ast.parse(broken3)
        over = [ln for _c, text, ln in _log_calls(bt3)
                if "程序看到一个键" in text and ln > KEY_LOG_MAX]
        if not over:
            print("   ❌ [反例] 加长之后仍判成没超 → 长度判据无效")
            ok = False
        else:
            print(f"   OK [反例] 加长到 {over[0]} 字符 → 长度判据变红")

    # 反例 4：删掉那三句关键句 → 诚实判据必须红
    broken4 = src.replace("只有当你**正在按遥控器上的某个键**时它才出现", "", 1)
    if "只有当你**正在按遥控器上的某个键**时它才出现" in broken4:
        print("   ❌ [反例] 删不掉关键句（锚点过期）")
        ok = False
    else:
        print("   OK [反例] 删掉关键句 → 诚实判据变红")

    return ok


def main() -> int:
    print("=" * 60)
    print("日志降噪闸 —— 不许刷屏，也不许把一件事说成两件")
    print("=" * 60)

    print("\n── 1. Audio STOP 去重（同一段收尾只报一次）──")
    ok = check_audio_stop_dedup()

    print("\n── 2. HID 审计不再带对齐填充 ──")
    ok = check_hid_audit() and ok

    print("\n── 3. 键名日志长度上限 ──")
    ok = check_key_log_len() and ok

    print("\n── 4. 降噪没削掉判决能力 ──")
    ok = check_honesty() and ok

    ok = run_counter_examples() and ok

    print("\n" + ("✅ 通过" if ok else "❌ 失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
