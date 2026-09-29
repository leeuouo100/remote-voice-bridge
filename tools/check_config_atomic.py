"""配置读改写的「并发保护 + 原子写」回归闸 —— 2026-09-29 审查报告 P1-5。

为什么要有这道闸
================
控制台是 `ThreadingHTTPServer`，**多个请求会并发**走
`Config.load() → 改字段 → cfg.save()`。原先：

  · `save()` 是 `CONFIG_PATH.write_text(...)` —— "截断 + 写入"，
    中间有一个窗口期，并发的读者会拿到**半截 JSON**；
  · 解析失败走 `except` → `return cls()` ⇒ **整份回退成默认值**，
    而调用方紧接着一个 `save()` 就把默认值**写回磁盘**，用户设置一次全没；
  · 两个请求各读各的旧快照再写回 → 后写的把先写的**整段覆盖**（丢配置）；
  · 用户手改过 config.json / 降级装回旧版会留下**未知字段**，直接喂给
    dataclass 构造函数 → `TypeError` → 同样整份回退。

这三条都属于"用户做了什么、系统没当回事"，而且**一声不响**，
用户只会说「我的设置自己没了」。所以在这里钉死：

  A. 20 个线程各改**不同**的键 → 20 个键**一个都不能丢**（`Config.update`）
     A0 反例：同一批线程改用老的 `load(); 改; save()` → **确实会丢**（证明 A 测得出来）
  B. 写线程狂写的同时，读线程**任何时刻**都能解析出完整 JSON（原子替换）
     B0 反例：换成非原子写（先截断再写）→ 读线程**确实**读到了半截
  C. 磁盘上的 config.json 被写坏时，**保住用户设置**（回退到最近一次有效快照）
     C0 反例：没有快照时**确实**会退回默认值（证明"退回默认"这条路真实存在）
  D. 含未知字段的 config.json **仍能读出已知字段**（未知的只警告）
     D0 反例：把未知字段直接喂 `cls(**data)` → **确实**抛 TypeError
  E. `save()` 是原子替换（`os.replace`，且不再截断写 `CONFIG_PATH`）
     E0 反例：合成一段老写法源码 → 检查函数**必须**判它不合格

⚠ 沙箱：把 APPDATA 指向临时目录，**绝不碰用户真实的 config.json**。

用法： python tools/check_config_atomic.py
输出： CONFIG ATOMIC OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

# ── 沙箱：必须在 import config 之前改 APPDATA ─────────────────────────────────
_SANDBOX = tempfile.mkdtemp(prefix="rvb-cfgatomic-")
os.environ["APPDATA"] = _SANDBOX

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FAILS: list[str] = []
PASSES = 0


def check(cond, msg) -> bool:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
    return bool(cond)


def _reset_disk() -> None:
    """把沙箱里的 config.json 删掉，让每个用例从干净状态开始。"""
    try:
        config.CONFIG_PATH.unlink()
    except FileNotFoundError:
        pass
    config._LAST_GOOD_RAW = None


def _naive_write(cfg) -> None:
    """老写法：直接截断写。**故意留着**，给 A0 / B0 当反例。"""
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.CONFIG_PATH.write_text(
        json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _naive_update(key: str) -> None:
    """老写法：load → 改 → save，中间没有锁。sleep 是为了把竞态放大到必现。"""
    cfg = config.Config.load()
    time.sleep(0.02)
    cfg.keymap[key] = "up"
    _naive_write(cfg)


# ── A. 并发 update 不丢更新 ──────────────────────────────────────────────────
def case_a() -> None:
    N = 20

    # A0 反例：老写法确实会丢 —— 先跑它，证明"丢更新"这件事真的会发生
    _reset_disk()
    config.Config.load().save()
    barrier = threading.Barrier(N)

    def _naive(i: int) -> None:
        barrier.wait()
        _naive_update(f"k{i}")

    ts = [threading.Thread(target=_naive, args=(i,)) for i in range(N)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    naive = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
    naive_keys = [k for k in naive.get("keymap", {}) if k.startswith("k") and k[1:].isdigit()]
    check(len(naive_keys) < N,
          f"A0 反例：老写法（load→改→save，无锁）确实丢了更新"
          f"（20 个键只剩 {len(naive_keys)} 个）—— 说明这条断言测得出来")

    # A：走 Config.update，20 个键一个都不能丢
    _reset_disk()
    config.Config.load().save()
    barrier2 = threading.Barrier(N)

    def _safe(i: int) -> None:
        barrier2.wait()
        config.Config.update(lambda c: c.keymap.__setitem__(f"k{i}", "up"))

    ts = [threading.Thread(target=_safe, args=(i,)) for i in range(N)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    safe = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
    safe_keys = sorted(k for k in safe.get("keymap", {}) if k.startswith("k") and k[1:].isdigit())
    check(len(safe_keys) == N,
          f"A1 Config.update 并发改 20 个不同键，一个都不丢（实际 {len(safe_keys)}/20）")
    # 反证的前提：A0 与 A1 用的是同一套线程编排，唯一的差别就是读改写有没有串行
    check(sorted(f"k{i}" for i in range(N)) == safe_keys,
          "A2 落盘的正好是那 20 个键（不多不少）")


# ── B. 任何时刻 JSON 都可解析 ────────────────────────────────────────────────
def case_b() -> None:
    _reset_disk()
    cfg = config.Config.load()
    cfg.save()

    # B0 反例：非原子写（先截断，停一下，再写）→ 读线程**确实**能读到半截
    #
    # ⚠ reader 必须**自己把异常记下来**，绝不能让线程崩掉 ——
    #   线程一崩，后面的 `bad == 0` 就变成"因为没人读所以没失败"的**假绿**。
    #   所以这里同时记 bad（半截 JSON）、err（打不开，Windows 上非原子写会独占）。
    def _new_counter() -> dict:
        return {"ok": 0, "bad": 0, "err": 0, "errs": []}

    def _reader(stop: threading.Event, counter: dict) -> None:
        while not stop.is_set():
            try:
                json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
                counter["ok"] += 1
            except FileNotFoundError:
                # 文件不在 = 一个**一致**的"还没有配置"状态，不是半截
                counter["ok"] += 1
            except json.JSONDecodeError:
                counter["bad"] += 1
            except OSError as e:
                counter["err"] += 1
                if type(e).__name__ not in counter["errs"]:
                    counter["errs"].append(type(e).__name__)

    torn = _new_counter()
    stop0 = threading.Event()
    th0 = threading.Thread(target=_reader, args=(stop0, torn), daemon=True)
    th0.start()
    text = json.dumps(config.Config().to_dict(), ensure_ascii=False, indent=2)
    for _ in range(3):
        with open(config.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.flush()          # 此刻文件被截断成空 —— 一个真实的"半截"窗口
            time.sleep(0.06)
            f.write(text)
    time.sleep(0.05)
    stop0.set()
    th0.join(timeout=2)
    check(torn["bad"] + torn["err"] > 0,
          f"B0 反例：非原子写（截断→写）时读线程确实读到了坏状态"
          f"（半截 {torn['bad']} 次 / 打不开 {torn['err']} 次 {torn['errs']}）"
          f"—— 说明这个读线程抓得住")
    check(not th0.is_alive(), "B0b 读线程是被正常叫停的，不是自己崩掉的")

    # B：Config.save() 是原子替换 → 读线程一次都不该读到半截
    live = _new_counter()
    errs: list[str] = []
    stop = threading.Event()
    th = threading.Thread(target=_reader, args=(stop, live), daemon=True)
    th.start()
    try:
        for i in range(200):
            config.Config.update(lambda c, i=i: c.keymap.__setitem__("__probe__", f"v{i}"))
    except Exception as e:                      # noqa: BLE001
        errs.append(f"{type(e).__name__}: {e}")
    time.sleep(0.05)
    stop.set()
    th.join(timeout=3)
    # ⚠ 这条要求 save() 内部对 os.replace 做**退避重试**：Windows 上目标文件被
    #   并发读短暂占用时 os.replace 会抛 PermissionError。不重试的话，
    #   表现就是「用户点了保存，其实没保存上」，而且界面上什么都不说。
    check(not errs,
          f"B1 200 次原子替换**全部成功**（实际 {len(errs)} 次失败：{errs[:2]}）"
          f"—— 需要 save() 对 os.replace 退避重试")
    # ⚠ 先证明读线程真的在跑，否则下面"0 次半截"是假绿
    check(live["ok"] >= 50,
          f"B2 裸读线程真的在并发读（成功 {live['ok']} 次）—— 后面 0 半截才有意义")
    check(live["bad"] == 0,
          f"B3 原子替换期间，裸读线程一次都没读到**半截 JSON**（实际 {live['bad']} 次）")
    # 说明性：裸读偶尔会被 Windows 的文件占用挡在门外（PermissionError）。
    # 这是文件系统语义、不是本程序的 bug —— 所以**不**作为失败项，
    # 但它正是"读路径必须自带重试"的理由，由 B4 来钉。
    print(f"  INFO 裸读期间被短暂占用 {live['err']} 次 {live['errs']}"
          f"（Windows 语义，不计失败；读路径的重试由 B4 保证）")

    # B4：**产品真实的读路径**（Config.load）在狂写的同时必须 100% 成功。
    #     这条才是用户能感知的：读失败 → 回退快照/默认 → 设置看着像丢了。
    rd_fail: list[str] = []
    stop2 = threading.Event()
    # 先把 gain 钉成 5.0，否则读线程可能在任何写之前读到默认的 10.0 → 误报
    config.Config.update(lambda c: setattr(c, "gain", 5.0))

    def _prod_reader() -> None:
        while not stop2.is_set():
            try:
                cfg = config.Config.load()
                if cfg.gain != 5.0:              # 写线程钉的是 gain=5.0
                    rd_fail.append(f"读到不完整配置：gain={cfg.gain}")
            except Exception as e:               # noqa: BLE001
                rd_fail.append(f"{type(e).__name__}: {e}")

    th2 = threading.Thread(target=_prod_reader, daemon=True)
    th2.start()
    for i in range(200):
        config.Config.update(lambda c: setattr(c, "gain", 5.0))
    time.sleep(0.05)
    stop2.set()
    th2.join(timeout=3)
    check(not rd_fail,
          f"B4 产品读路径 Config.load() 在并发写期间 100% 成功且配置完整"
          f"（实际 {len(rd_fail)} 次异常：{rd_fail[:2]}）")


# ── C. 解析失败不回退默认 ────────────────────────────────────────────────────
def case_c() -> None:
    _reset_disk()
    cfg = config.Config.load()
    cfg.gain = 7.5
    cfg.save()                                   # 顺带刷新进程内"最近有效"快照

    config.CONFIG_PATH.write_text("{ 这不是 JSON", encoding="utf-8")   # 写坏磁盘

    got = config.Config.load()
    check(got.gain == 7.5,
          f"C1 磁盘被写坏时保住用户设置（期望 gain=7.5，实际 {got.gain}）")

    # C0 反例：没有快照时**确实**会退回默认 —— 证明"退回默认"这条路真实存在
    config._LAST_GOOD_RAW = None
    got2 = config.Config.load()
    check(got2.gain == config.Config().gain,
          f"C0 反例：无历史快照 → 退回默认值 gain={config.Config().gain}"
          f"（实际 {got2.gain}）—— 说明 C1 的通过是靠快照，不是碰巧")


# ── D. 未知字段不回退 ────────────────────────────────────────────────────────
def case_d() -> None:
    _reset_disk()
    raw = config.Config().to_dict()
    raw["gain"] = 3.0
    raw["__definitely_unknown__"] = 123          # 手改 / 降级装回旧版会留下这种
    config.CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    config._LAST_GOOD_RAW = None                 # 只依赖文件本身，不靠快照

    got = config.Config.load()
    check(got.gain == 3.0,
          f"D1 含未知字段的 config.json 仍能读出已知字段（期望 gain=3.0，实际 {got.gain}）")
    check(got.config_version == config.CONFIG_VERSION,
          "D2 未知字段被摘掉之后，已知字段照常参与迁移")

    # D0 反例：老写法把未知字段直接喂构造函数 → TypeError → 整份回退
    raised = False
    try:
        config.Config(**raw)
    except TypeError:
        raised = True
    check(raised,
          "D0 反例：把未知字段直接喂给 dataclass 构造函数确实抛 TypeError"
          "（这就是老写法整份回退的原因）")


# ── E. save() 是原子替换（静态） ─────────────────────────────────────────────
def _save_body(src: str) -> str | None:
    """取出 `save` 函数的源码（不含周边注释）—— 用 AST，避免被注释误伤。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "save":
            return ast.get_source_segment(src, node) or ""
    return None


def _save_is_atomic(body: str | None) -> bool:
    """save() 必须：① 用 os.replace 做原子替换；② 不再直接截断写 CONFIG_PATH。"""
    if not body:
        return False
    return ("os.replace" in body) and ("CONFIG_PATH.write_text" not in body)


def case_e() -> None:
    src = (REPO / "config.py").read_text(encoding="utf-8")
    body = _save_body(src)
    check(body is not None, "E1 能从 config.py 里取出 save() 的源码")
    check(body is not None and "os.replace" in body,
          "E2 save() 用 os.replace 做原子替换")
    check(body is not None and "CONFIG_PATH.write_text" not in body,
          "E3 save() 不再直接截断写 CONFIG_PATH")

    # E0 反例：老写法必须被判不合格 —— 否则 E2/E3 可能是"永远绿"
    old = (
        "import json\n"
        "class C:\n"
        "    def save(self) -> None:\n"
        "        CONFIG_PATH.write_text(\n"
        "            json.dumps(self.to_dict()), encoding='utf-8')\n"
    )
    check(not _save_is_atomic(_save_body(old)),
          "E0 反例：老写法（CONFIG_PATH.write_text）被判不合格")
    check(_save_is_atomic(body),
          "E4 真源码被判合格（E0 与 E4 用同一个判定函数）")


def main() -> int:
    for fn in (case_a, case_b, case_c, case_d, case_e):
        try:
            fn()
        except Exception as e:                   # noqa: BLE001
            FAILS.append(f"{fn.__name__} 抛异常：{type(e).__name__}: {e}")

    shutil.rmtree(_SANDBOX, ignore_errors=True)

    for m in FAILS:
        print(f"  FAIL {m}")
    if FAILS:
        print(f"CONFIG ATOMIC FAILED（{len(FAILS)} 项）")
        print("  提示：配置读改写必须串行 + 落盘必须原子。"
              "「设置自己没了」是最难查的一类 bug —— 用户只会说『它不听话』。")
        return 1
    print(f"  OK   {PASSES} 项全过（并发不丢 / 原子读 / 坏文件保设置 / 未知字段 / 静态）")
    print("CONFIG ATOMIC OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
