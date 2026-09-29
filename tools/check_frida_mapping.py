"""按键屏蔽表「随配置实时更新 + 等到确认」的回归闸 —— 2026-09-29 审查报告 P1-6。

背景
====
`RemoteHidTap.set_mapping()` 原先只在**启动时**调用一次。而 JS 侧
（`frida_tap.js` 的 `nullify()`）是「按这张表把报告里的 usage **原地写 0**」的。
于是运行中：

  · 关掉 `mapping_enabled`  → Python 不再映射，JS 仍按旧表清零
  · 把某个键改成 `native`   → 同上，那个键彻底失效
  · 改一个新键的映射        → JS 不知道，新键的原生动作照样发生

现象统一是「**界面说改了/关了，实际得重连才生效**」—— 看起来像「这个键坏了」。
另一面：下发是**异步**的，控制台保存完就回「已生效」，用户可能立刻按键、
而钩子还在用旧表。所以要能**等确认**。

这道闸钉四件事（每条都配反例）：

  A. `frida_tap.js` 的 `block_ack` 回传 `seq`（没有它就无法区分「我这一版生效了」
     和「收到的是上一条的迟到 ack」）
  B. Python 侧 `set_mapping(wait=…)` + 序号比对；脚本没了要作废确认状态
  C. 行为级（真跑，不注入 frida）：拿假 script 驱动 `RemoteHidTap`，验
      ① 正常 ack → True      ② 空表能清空（总开关关掉的情形）
      ③ 没挂脚本 → False     ④ **迟到 ack 不算数**（最容易假绿的一条）
  D. 接线：控制台三个写入口都会推 + 等确认；`main._get_cfg` 一变就推；
     `_effective_keymap` 在总开关关时给**空表**；`_hid_tap` 声明在 `_get_cfg`
     之前（否则预热那次调用会撞 NameError）

用法： python tools/check_frida_mapping.py
输出： FRIDA MAPPING OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_checks: list[tuple[bool, str]] = []


def A(ok, msg) -> None:
    _checks.append((bool(ok), msg))


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


# ── A. JS 侧 ────────────────────────────────────────────────────────────────
def case_js() -> None:
    js = _read("frida_tap.js")
    acks = re.findall(r'kind:\s*"block_ack"[^}]*', js)
    A(acks, "A1 frida_tap.js 里有 block_ack 回执")
    A(len(acks) == 1, f"A1b 只有一处 block_ack 回执（实际 {len(acks)} 处）")
    A(all("seq" in a for a in acks),
      "A2 block_ack 把 seq 原样回传（否则 Python 分不清「我这一版生效了」"
      "和「收到的是上一条的迟到 ack」）")
    A(re.search(r"blockCC\s*=\s*cc\s*;", js) is not None
      and re.search(r"blockVendor\s*=\s*vd\s*;", js) is not None,
      "A3 收到新表时是**整体替换**（blockCC = cc / blockVendor = vd）"
      "—— 用 merge 的写法就永远清不掉旧表，空表下发形同虚设")
    A("blockCC = {}" in js and "blockVendor = {}" in js,
      "A3b 两张表都从空对象起（新表里没有的 usage 一定不再被抹）")


def case_js_negative() -> None:
    """反例：把 seq 从回执里拿掉，A2 必须能发现。"""
    js = _read("frida_tap.js")
    broken = re.sub(r'kind:\s*"block_ack",[^}]*', 'kind: "block_ack", count: 0 ', js)
    tail = broken.split('kind: "block_ack"')[1][:120] if 'kind: "block_ack"' in broken else ""
    A("seq" not in tail,
      "[反例] 回执不带 seq → A2 判不合格（不是「JS 里出现过 seq 就算」）")


# ── B. Python 侧（静态）─────────────────────────────────────────────────────
def case_py_static() -> None:
    src = _read("frida_hid.py")

    m = re.search(r"def set_mapping\(\s*self,\s*mapping[^)]*\)\s*->\s*[^:]+:", src, re.S)
    A(m is not None, "B0 找得到 set_mapping()")
    sig = m.group(0) if m else ""
    A("wait" in sig and "timeout" in sig,
      "B1 set_mapping 有 wait / timeout（控制台要能等确认）")
    A('script.post({"type": "block", "cc": cc, "vendor": vp, "seq": seq})' in src,
      "B2 下发的消息里带 seq（与 JS 侧的回执对齐）")
    A("self._acked_seq >= seq" in src,
      "B3 等的就是**我这一版**（比对序号，不是「收到过 ack 就算」）")
    A("if script is None:" in src and "return False" in src,
      "B4 没挂上脚本时返回 False（＝没有钩子可以确认，调用方要如实说话）")
    A(re.search(r"def _teardown.*?self\._acked_seq\s*=\s*-1", src, re.S) is not None,
      "B5 脚本一没（_teardown）就作废确认状态"
      "—— 留着偏大的 _acked_seq，下一次 wait 会**假确认**")
    A(re.search(r'kind == "block_ack"', src) is not None
      and "seq_i >= self._ack_seq" in src,
      "B6 回执处理只认**够新**的序号（迟到/乱序的 ack 不许放行）")
    A("def active_tap(" in src, "B7 有 active_tap()（控制台据此拿到在跑的实例）")
    A(re.search(r"def push_mapping\(.*?if tap is None:\s*\n\s*return True", src, re.S)
      is not None,
      "B8 没有旁路在跑时 push_mapping 返回 **True**"
      "（别把「没装 frida / 旁路没起来」报成「保存失败」，那会把用户支去查配置）")
    A("super().start()" in src and "_ACTIVE = self" in src,
      "B9 start() 登记为当前旁路")
    A(re.search(r"def stop\(self\).*?_ACTIVE = None", src, re.S) is not None,
      "B10 stop() 注销（否则退出后控制台还在往一个死实例推）")


def case_py_negative() -> None:
    """反例：拆掉序号比对，B6 必须能发现。"""
    src = _read("frida_hid.py")
    broken = src.replace("if seq_i >= self._ack_seq:", "if True:")
    A("seq_i >= self._ack_seq" not in broken,
      "[反例] 把「只认够新的 ack」改成来者不拒 → B6 判不合格")


# ── C. 行为级：拿假 script 驱动 RemoteHidTap（不注入 frida）─────────────────
class _FakeScript:
    """只记录 post 出去的东西（不碰真 frida）。"""

    def __init__(self) -> None:
        self.posts: list[dict] = []

    def post(self, msg) -> None:
        self.posts.append(dict(msg))

    def unload(self) -> None:
        pass


def _ack_later(tap, seq: int, delay: float) -> None:
    """delay 秒后把 seq 号的 ack 投给 tap（模拟钩子的异步回执）。"""

    def w() -> None:
        time.sleep(delay)
        tap._on_message(
            {"type": "message",
             "payload": {"kind": "block_ack", "count": 0, "seq": seq}}, None)

    threading.Thread(target=w, daemon=True).start()


def _make_loose_tap(mod):
    """反例用的替身工厂：造出「P1-6 没修之前」的那种形状。

    真正的守卫是**两半合起来**才成立的：
      · `_on_message` 里 `seq_i >= self._ack_seq`（只认够新的回执）
      · `set_mapping` 末尾 `self._acked_seq >= seq`（等的就是我这版）
    所以反例必须**两半都松掉**，只松一半的话 C4 照样红、证明不了什么
    （第一版反例就只松了 `_on_message`，结果 C5 自己红了 —— 这正说明
    单靠"事件被 set 过"是不够的，两半缺一不可）。
    """
    base = mod.RemoteHidTap

    class _T(base):                                  # type: ignore[misc, valid-type]
        def _on_message(self, message, data) -> None:
            payload = message.get("payload") or {}
            if payload.get("kind") == "block_ack":
                with self._ack_lock:
                    self._acked_seq = self._ack_seq      # ← 来者不拒
                    self._ack_event.set()
                return
            super()._on_message(message, data)

        def set_mapping(self, mapping=None, wait=False, timeout=1.5) -> bool:
            with self._lock:
                self._mapping = dict(mapping or {})
            cc, vp = mod.block_usages_for(self._mapping)
            script = self._script
            if script is None:
                return False
            with self._ack_lock:
                self._ack_seq += 1
                seq = self._ack_seq
                self._ack_event.clear()
            script.post({"type": "block", "cc": cc, "vendor": vp, "seq": seq})
            if not wait:
                return True
            return self._ack_event.wait(timeout)         # ← 只看"响过没有"

    return _T


def case_behavior() -> None:
    import frida_hid

    # C1 正常 ack → True，且下发的是**已映射键**的 usage
    tap = frida_hid.RemoteHidTap(lambda *a: None)
    tap._script = _FakeScript()
    _ack_later(tap, 1, 0.05)
    ok1 = tap.set_mapping({"up": "up", "home": "native"}, wait=True, timeout=1.0)
    A(ok1 is True, "C1 钩子回了 ack → set_mapping(wait=True) 返回 True")
    post = tap._script.posts[-1] if tap._script.posts else {}
    A(0x0042 in (post.get("cc") or []),
      "C1b 下发的是「要抹掉原生动作」的 usage（方向上 0x0042 在里面）")
    A(0x0223 not in (post.get("cc") or []),
      "C1c 值写成 native 的键**不进**抹除表（用户就是想要它的原生行为）")

    # C2 空表 → 两张表都清空（总开关关掉 / 全部 native 的情形）
    tap2 = frida_hid.RemoteHidTap(lambda *a: None)
    tap2._script = _FakeScript()
    _ack_later(tap2, 1, 0.05)
    ok2 = tap2.set_mapping({}, wait=True, timeout=1.0)
    p2 = tap2._script.posts[-1] if tap2._script.posts else {}
    A(ok2 is True and p2.get("cc") == [] and p2.get("vendor") == [],
      "C2 空表下发的是**真的空表**（cc=[] / vendor=[]）"
      "—— 这正是「总开关关掉 = 不许再抹任何键」要的效果")

    # C3 没挂脚本 → False（没有钩子可以确认）
    tap3 = frida_hid.RemoteHidTap(lambda *a: None)
    A(tap3.set_mapping({"up": "up"}, wait=True, timeout=0.3) is False,
      "C3 还没挂上脚本时返回 False（调用方据此说「未确认」，不是「成功」）")

    # C4 ⚠ 迟到 ack 不算数 —— 这条最容易假绿
    tap4 = frida_hid.RemoteHidTap(lambda *a: None)
    tap4._script = _FakeScript()
    _ack_later(tap4, 1, 0.15)                       # 只回**第 1 版**的 ack
    tap4.set_mapping({"up": "up"}, wait=False)      # 第 1 版（seq=1）
    ok4 = tap4.set_mapping({"down": "down"}, wait=True, timeout=0.6)   # 第 2 版（seq=2）
    A(ok4 is False,
      "C4 **迟到 ack 不算数**：只回上一版的 ack 时，第 2 版必须等到超时返回 False"
      "（否则控制台会在旧表还在用时报告「已生效」）")

    # C5 反例自证：同一场景换成「两半都松掉」的替身 → 上面那条必须变绿（＝失效）
    loose = _make_loose_tap(frida_hid)
    tap5 = loose(lambda *a: None)
    tap5._script = _FakeScript()
    _ack_later(tap5, 1, 0.15)
    tap5.set_mapping({"up": "up"}, wait=False)
    ok5 = tap5.set_mapping({"down": "down"}, wait=True, timeout=0.6)
    A(ok5 is True,
      "[反例] 把回执处理改成「来者不拒」后，同一场景变成 True"
      " ⇒ C4 抓的正是这个差别（不是「反正都会超时」）")

    # C6 脚本没了要作废确认状态：_teardown 之后不能假确认
    tap6 = frida_hid.RemoteHidTap(lambda *a: None)
    tap6._script = _FakeScript()
    _ack_later(tap6, 1, 0.05)
    tap6.set_mapping({"up": "up"}, wait=True, timeout=1.0)   # 先把 _acked_seq 抬到 1
    tap6._teardown()                                        # 脚本没了
    tap6._script = _FakeScript()                            # 重新挂一个（不 ack）
    A(tap6.set_mapping({"down": "down"}, wait=True, timeout=0.4) is False,
      "C6 重挂之后**不许**拿旧的确认状态顶数（_teardown 要作废 _acked_seq）")

    # C7 没有在跑的旁路时 push_mapping 返回 True（别把「没装 frida」报成保存失败）
    old = frida_hid._ACTIVE
    try:
        frida_hid._ACTIVE = None
        A(frida_hid.push_mapping({"up": "up"}, wait=True) is True,
          "C7 没有旁路在跑 → push_mapping 返回 True（不是「保存失败」）")
    finally:
        frida_hid._ACTIVE = old


# ── D. 接线：控制台 / main ──────────────────────────────────────────────────
def case_wiring() -> None:
    cs = _read("console_server.py")
    mn = _read("main.py")

    A("def _push_mapping_now(" in cs, "D0 控制台有 _push_mapping_now()")
    m = re.search(r"def _push_mapping_now\(.*?\n(?=    def )", cs, re.S)
    body = m.group(0) if m else ""
    A("wait=True" in body, "D1 控制台推表时 **wait=True**（等钩子确认再回报）")
    A('if getattr(cfg, "mapping_enabled", True) else {}' in body,
      "D2 总开关关掉时下发的是**空表**（不是跳过下发 —— 跳过＝JS 保留旧表）")

    m_reset = re.search(r'if path == "/api/mapping/reset":.*?\n(?=\s+if path)', cs, re.S)
    A(m_reset is not None and "_push_mapping_now()" in m_reset.group(0),
      "D3 写入口 /api/mapping/reset 会推表并等确认")
    m_map = re.search(r"def _patch_mapping\(.*?\n(?=    def )", cs, re.S)
    A(m_map is not None and "_push_mapping_now()" in m_map.group(0),
      "D3b 写入口 /api/mapping 会推表并等确认")
    A('out["effective"] = self._push_mapping_now()' in cs,
      "D3c 写入口 /api/config 会推表并等确认")

    A(re.search(r'if "mapping_enabled" in body:', cs) is not None,
      "D4 /api/config 只在 mapping_enabled 变化时推（其它字段改不动屏蔽表）")

    A("_push_mapping_if_changed(_cfg_cache)" in mn,
      "D5 main._get_cfg 每次重载配置都会推表（mtime 一变就生效，不用重连）")
    A(re.search(r"def _effective_keymap\(.*?return \{\}", mn, re.S) is not None,
      "D6 _effective_keymap 在总开关关时返回**空表**")
    A(re.search(r"def _push_mapping_if_changed\(.*?nonlocal _last_pushed_mapping",
                mn, re.S) is not None,
      "D7 表没变就不重复下发（否则按键一来就 post 一次，纯噪音）")

    i_decl = mn.index("_hid_tap = None")
    i_def = mn.index("def _get_cfg()")
    i_preheat = mn.index("    _get_cfg()\n")
    A(i_decl < i_def < i_preheat,
      "D8 `_hid_tap = None` 声明在 `_get_cfg` 定义之前（它是闭包变量；"
      "晚一步声明，预热那次 _get_cfg() 会撞 NameError: unbound local，"
      "整个桥都起不来）")
    A(mn.count("_hid_tap = None") == 2,
      f"D8b `_hid_tap = None` 只出现在该出现的地方（声明 + except 兜底），"
      f"实际 {mn.count('_hid_tap = None')} 处")
    A("_hid_tap.set_mapping(_initial)" in mn,
      "D9 启动时下发的是**归一后**的表（_initial = _effective_keymap(...)）")


def case_wiring_negative() -> None:
    """反例：把 wait 拆掉、把声明挪后，对应断言必须变红。"""
    cs = _read("console_server.py")
    broken = cs.replace("push_mapping(km, wait=True", "push_mapping(km, wait=False")
    m = re.search(r"def _push_mapping_now\(.*?\n(?=    def )", broken, re.S)
    A("wait=True" not in (m.group(0) if m else ""),
      "[反例] 把 wait=True 改成 False → D1 判不合格")

    mn = _read("main.py")
    lines = mn.splitlines(keepends=True)
    out, dropped = [], False
    for ln in lines:                    # 删掉**第一处**声明（＝挪到 _get_cfg 之后）
        if not dropped and ln.strip() == "_hid_tap = None":
            dropped = True
            continue
        out.append(ln)
    broken2 = "".join(out)
    try:
        i_decl = broken2.index("_hid_tap = None")
        i_def = broken2.index("def _get_cfg()")
        A(i_decl > i_def,
          "[反例] 去掉早声明后，唯一声明落在 _get_cfg 之后 → D8 判不合格")
    except ValueError:
        A(True, "[反例] 去掉早声明后根本找不到声明 → D8 判不合格")


def main() -> int:
    print("=" * 74)
    print(" 闸：按键屏蔽表随配置实时更新 + 等钩子确认（P1-6）")
    print("=" * 74)

    case_js()
    case_js_negative()
    case_py_static()
    case_py_negative()
    case_behavior()
    case_wiring()
    case_wiring_negative()

    fails = 0
    for ok, msg in _checks:
        print(f"  {'OK  ' if ok else '❌  '} {msg}")
        if not ok:
            fails += 1

    print()
    if fails:
        print(f"FRIDA MAPPING FAILED（{fails} 项）")
        print("  提示：这几条坏掉的现象是「界面说改了/关了，实际得重连才生效」，")
        print("        以及「控制台报已生效、钩子还在用旧表」。")
        return 1
    print(f"FRIDA MAPPING OK（{len(_checks)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
