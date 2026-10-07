"""BLE「假连接」闸 —— `connection_status` 说已连接，不算数。

为什么单独一条闸
----------------
2026-10-07 真机：用户报「连接很不顺畅，时间一停就再也不能连接上了，
只有关掉软件重来」。日志给出了**铁证**：

    14:57:52,944 🔗 Connecting...
    14:57:53,144 ✅ Connected  status=1        ← 200 毫秒就"连上"了
    14:57:53,146 📡 Discovering ATVV service...
    14:57:53,384 ❌ ATVV characteristic(s) not found   ← 238 毫秒（纯读缓存）
    ……之后每 30 秒重来一次，**连着失败 17 次、整整 7 分钟**……

对照一次**真的**连接（同一台机器、同一天）：

    15:09:07,857 🔗 Connecting...
    15:09:15,246 ✅ Connected  status=1        ← 用了 **7.3 秒**

⇒ 200 毫秒那个是**假的**。上一次断开之后 Windows 的 BLE 栈会**残留**
`connection_status = CONNECTED`，而 `_hold_ble_connection` 一看它是 CONNECTED
就立刻返回 True —— 于是下游读到的是**脏缓存**，报「ATVV 特征找不到」，
然后重连、又秒过、又失败，**死循环**。

这条闸钉四件事
--------------
1. **CONNECTED 之后必须真读一次 GATT**（`_gatt_really_reachable`），
   而且必须用 `BluetoothCacheMode.UNCACHED` —— 默认的 `Cached` 在链路断掉时
   **照样返回缓存**，当探针等于没验。
2. **验不了 ≠ 判失败**：这个 winrt 版本没有 cache_mode 重载时（`TypeError`）
   必须返回 `None`（"我不知道"），**绝不能返回 False** —— 否则那些机器
   **永远连不上**。
3. **探针要节流**（一次 1~3 秒，20 秒窗口里不能每 0.5 秒扎一次）。
4. **超时后要留活路**：系统仍坚持说「已连接」就先信它一次 ——
   宁可退回老行为，也不能因为"探针本身不好使"把用户挡在门外。

外加：ATVV 服务/特征发现失败时要用 UNCACHED 重试，并**报出缺的是哪一个**
（原来一句笼统的 `characteristic(s)`，什么都看不出来）。

用法
----
    python tools/check_ble_stale_connection.py

退出码 0 = 通过；1 = 失败。
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _code_only(src: str, keep_strings: bool = False) -> str:
    """剥掉注释与（默认还有）字符串字面量。

    ⚠ 本闸的**注释里逐字引用了** `connection_status == CONNECTED` 这种老写法
      （说明它为什么错），不剥的话检查会把自己的注释当成违规。
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "#":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith('"""', i) or src.startswith("'''", i):
            q = src[i:i + 3]
            j = src.find(q, i + 3)
            i = n if j < 0 else j + 3
        elif c in "\"'":
            q = c
            j = i + 1
            while j < n and src[j] != q:
                if src[j] == "\\":
                    j += 1
                j += 1
            if keep_strings:
                out.append(src[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _fn_body(src: str, name: str) -> str:
    """取模块级函数 `name` 的函数体（到下一个**顶格** def/class 为止）。"""
    m = re.search(rf"^(?:async )?def {re.escape(name)}\(", src, re.M)
    if not m:
        return ""
    rest = src[m.end():]
    nxt = re.search(r"^(?:async )?def |^class |^@", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


# ── 1. 探针本身：必须 UNCACHED、且「验不了」要返回 None ─────────────────────
def check_probe(verbose: bool = True) -> bool:
    ok = True
    src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")
    body = _fn_body(src, "_gatt_really_reachable")
    code = _code_only(body, keep_strings=True)

    if not body:
        print("   ❌ main.py 里找不到 _gatt_really_reachable（改名了？闸门要跟着改）")
        return False

    if "BluetoothCacheMode.UNCACHED" not in code:
        print("   ❌ 探针没有用 UNCACHED —— 默认的 Cached 在链路已经断掉时"
              "**照样返回缓存里的服务表**，拿它当探针等于什么都没验")
        ok = False
    elif verbose:
        print("   OK 探针走 UNCACHED（真的去问设备）")

    # TypeError（这个 winrt 版本没有重载）必须返回 None，不能返回 False
    m = re.search(r"except TypeError:(.*?)(?=\n    except |\n    return )", body, re.S)
    if m is None:
        print("   ❌ 探针没有 `except TypeError` 分支 —— winrt 版本没有 cache_mode "
              "重载时（pywinrt 会抛 TypeError）整个连接流程会直接崩")
        ok = False
    else:
        seg = _code_only(m.group(1), keep_strings=True)
        if "return None" not in seg:
            print("   ❌ `except TypeError` 分支没有返回 None —— "
                  "**验不了 ≠ 判失败**：返回 False 会让那些机器永远连不上")
            ok = False
        elif verbose:
            print("   OK 验不了（TypeError）返回 None，不会误判成失败")

    return ok


# ── 2. CONNECTED 之后必须真验，不许裸 return ────────────────────────────────
def check_hold_logic(verbose: bool = True) -> bool:
    ok = True
    src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")
    body = _fn_body(src, "_hold_ble_connection")
    code = _code_only(body, keep_strings=True)

    if not body:
        print("   ❌ main.py 里找不到 _hold_ble_connection")
        return False

    # ① 必须调探针
    if "_gatt_really_reachable(" not in code:
        print("   ❌ _hold_ble_connection 里没有调 _gatt_really_reachable —— "
              "`connection_status == CONNECTED` 就 return True 正是"
              "「假连接」被当成真连接的原因")
        ok = False
    elif verbose:
        print("   OK CONNECTED 之后会真读一次 GATT")

    # ② 不许出现「一看到 CONNECTED 就裸 return True」
    #    （`_real_link` 里那句 `if ble.connection_status != CONNECTED: return False`
    #      是**反向**判断，不算）
    naked = re.findall(
        r"if\s+ble\.connection_status\s*==\s*BluetoothConnectionStatus\.CONNECTED:\s*\n\s*return\s+True",
        code)
    if naked:
        print(f"   ❌ 还有 {len(naked)} 处「CONNECTED 就裸 return True」—— "
              f"那就是把假连接当真连接")
        ok = False
    elif verbose:
        print("   OK 没有「CONNECTED 就裸 return True」")

    # ③ 探针要节流
    if "_GATT_PROBE_INTERVAL" not in code:
        print("   ❌ 探针没有节流（_GATT_PROBE_INTERVAL）—— UNCACHED 一次要 1~3 秒，"
              "20 秒窗口里每 0.5 秒扎一次根本等不到真连接")
        ok = False
    elif verbose:
        print("   OK 探针有节流")

    # ④ 超时后要留活路
    tail = code.rsplit("while waited < timeout", 1)[-1]
    if not re.search(r"CONNECTED[\s\S]{0,400}?return\s+True", tail):
        print("   ❌ 超时之后没有「最后一搏」—— UNCACHED 探针在某些驱动/环境下"
              "可能**总是失败**，一律判失败会让那些机器永远连不上")
        ok = False
    elif verbose:
        print("   OK 超时后仍报 CONNECTED 会先按已连接继续（留活路）")

    return ok


# ── 3. ATVV 发现失败要能穿透脏缓存，并报出缺哪个 ────────────────────────────
def check_atvv_discovery(verbose: bool = True) -> bool:
    ok = True
    src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")

    for fn, must in (("_gatt_services_uncached", "BluetoothCacheMode.UNCACHED"),
                     ("_read_atvv_chars", "BluetoothCacheMode.UNCACHED")):
        b = _code_only(_fn_body(src, fn), keep_strings=True)
        if not b:
            print(f"   ❌ 找不到 {fn}")
            ok = False
        elif must not in b:
            print(f"   ❌ {fn} 没有用 UNCACHED")
            ok = False
    if ok and verbose:
        print("   OK 服务 / 特征都能走 UNCACHED 重新发现")

    # 调用处：失败要有 UNCACHED 重试 + 报出缺哪个
    seg = _code_only(_fn_body(src, "_run_bridge_inner"), keep_strings=True)
    if "_gatt_services_uncached(" not in seg:
        print("   ❌ 服务读不到时没有走 UNCACHED 重试")
        ok = False
    if "_read_atvv_chars(" not in seg:
        print("   ❌ 特征读不到时没有走 UNCACHED 重试")
        ok = False
    if "缺 %s" not in seg:
        print("   ❌ 报错没有指出**缺的是哪个特征**（原来一句笼统的 "
              "`characteristic(s)`，真机上什么都看不出来）")
        ok = False
    elif ok and verbose:
        print("   OK 失败时会报出缺的是哪个特征（TX/AUDIO/CTL）")

    return ok


# ── 4. 行为级：拿假 ble 把三种结果都跑一遍 ──────────────────────────────────
class _Res:
    def __init__(self, status, services):
        self.status = status
        self.services = services


class _FakeBle:
    def __init__(self, behavior, success, unreachable):
        self.behavior = behavior
        self._ok = success
        self._bad = unreachable

    async def get_gatt_services_async(self, *a):
        if self.behavior == "typeerror":
            raise TypeError("no overload for this winrt build")
        if self.behavior == "boom":
            raise RuntimeError("链路炸了")
        if self.behavior == "empty":
            return _Res(self._ok, [])
        if self.behavior == "bad_status":
            return _Res(self._bad, ["svc"])
        return _Res(self._ok, ["svc"])


def check_behavior(verbose: bool = True) -> bool:
    ok = True
    try:
        import main
    except Exception as e:                            # noqa: BLE001
        print(f"   ❌ import main 失败：{type(e).__name__}: {e}")
        return False

    probe = getattr(main, "_gatt_really_reachable", None)
    if probe is None:
        print("   ❌ main 里没有 _gatt_really_reachable")
        return False

    # ⚠ 用**真实**的枚举值构造假数据 —— 别硬编码。
    #   `GattCommunicationStatus.SUCCESS` 不是 1（UWP 里是 0），
    #   硬编码 1 会让「正常」这一条永远判红（闸门第一版就是这么错的）。
    try:
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCommunicationStatus,
        )
        _ok_v = int(GattCommunicationStatus.SUCCESS)
        _bad_v = int(GattCommunicationStatus.UNREACHABLE)
    except Exception as e:                            # noqa: BLE001
        print(f"   ❌ 取 GattCommunicationStatus 失败：{type(e).__name__}: {e}")
        return False
    if verbose:
        print(f"   （真实枚举值：SUCCESS={_ok_v}  UNREACHABLE={_bad_v}）")

    def _ble(b):
        return _FakeBle(b, _ok_v, _bad_v)

    cases = (
        ("正常（有服务）", "ok", True),
        ("空服务表", "empty", False),
        ("status 不是 SUCCESS", "bad_status", False),
        ("链路炸了（异常）", "boom", False),
        ("没有 cache_mode 重载", "typeerror", None),
    )
    for desc, behavior, want in cases:
        try:
            got = asyncio.run(probe(_ble(behavior)))
        except Exception as e:                        # noqa: BLE001
            print(f"   ❌ {desc}：探针抛了 {type(e).__name__}: {e}（它必须永不抛）")
            ok = False
            continue
        if got != want:
            print(f"   ❌ {desc}：探针返回 {got!r}，应为 {want!r}")
            ok = False
        elif verbose:
            print(f"   OK {desc} → {got!r}")

    # ⚠ 单独强调这一条：验不了必须返回 None，不能是 False
    got = asyncio.run(probe(_ble("typeerror")))
    if got is not None:
        print(f"   ❌ 「没有 cache_mode 重载」时返回 {got!r}，必须是 None —— "
              f"返回 False 会让这个 winrt 版本的用户**永远连不上**")
        ok = False

    return ok


# ── 5. 反例自证 ─────────────────────────────────────────────────────────────
def run_counter_examples() -> bool:
    ok = True
    print("\n── 反例自证（判据真的会红吗）──")
    src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")

    # 反例 1：把探针的 UNCACHED 换成 CACHED → 探针判据必须红
    # ⚠ 锚点必须落在**代码**上，不能落在注释上 —— 探针的 docstring 里也写着
    #   `BluetoothCacheMode.UNCACHED`（说明为什么必须用它），用裸字符串替换
    #   会先命中注释，剥掉注释后代码里还是 UNCACHED ⇒ 反例假绿。
    broken = src.replace(
        "ble.get_gatt_services_async(BluetoothCacheMode.UNCACHED)",
        "ble.get_gatt_services_async(BluetoothCacheMode.CACHED)")
    if broken == src:
        print("   ❌ [反例] 找不到探针里的 UNCACHED 调用（闸门锚点过期了）")
        ok = False
    b = _code_only(_fn_body(broken, "_gatt_really_reachable"), keep_strings=True)
    if "BluetoothCacheMode.UNCACHED" in b:
        print("   ❌ [反例] 换成 CACHED 后仍被判成 UNCACHED → 判据无效")
        ok = False
    else:
        print("   OK [反例] 探针换成 CACHED → UNCACHED 判据变红")

    # 反例 2：TypeError 分支改成 return False → 必须红
    b2 = _code_only(_fn_body(
        src.replace("        return None\n    except Exception as e:                       # noqa: BLE001\n        logger.debug(\"GATT 探针抛异常",
                    "        return False\n    except Exception as e:                       # noqa: BLE001\n        logger.debug(\"GATT 探针抛异常"),
        "_gatt_really_reachable"), keep_strings=True)
    if "return None" in b2:
        print("   ❌ [反例] 把「验不了」改成 False 后仍能通过 → 判据无效")
        ok = False
    else:
        print("   OK [反例] 「验不了」改成返回 False → 判据变红")

    # 反例 3：退回「CONNECTED 就裸 return True」→ 必须红
    naked_src = src.replace(
        "        if await _real_link(waited):",
        "        if ble.connection_status == BluetoothConnectionStatus.CONNECTED:\n"
        "            return True, sess")
    naked = re.findall(
        r"if\s+ble\.connection_status\s*==\s*BluetoothConnectionStatus\.CONNECTED:\s*\n\s*return\s+True",
        _code_only(naked_src, keep_strings=True))
    if not naked:
        print("   ❌ [反例] 塞回「CONNECTED 就裸 return True」后判据抓不到 → 判据无效")
        ok = False
    else:
        print("   OK [反例] 塞回「CONNECTED 就裸 return True」→ 判据变红")

    # 反例 4：注释里引用老写法不许被误判（_code_only 生效）
    probe = ("# 老写法：if ble.connection_status == BluetoothConnectionStatus.CONNECTED:\n"
             "#             return True, sess\n")
    if re.search(r"==\s*BluetoothConnectionStatus\.CONNECTED:\s*\n\s*return\s+True",
                 _code_only(probe)):
        print("   ❌ [反例] 注释里的老写法被误判 → 闸门会对着自己的注释报红")
        ok = False
    else:
        print("   OK [反例] 注释里引用老写法不会被误判（_code_only 生效）")

    # 反例 5：正向断言必须 keep_strings=True
    # ⚠ 探针里那个字符串要和**真实代码**一致：代码里报的是
    #   `❌ ATVV characteristic(s) not found（缺 %s）`，而"缓存里缺…"那条
    #   警告里并没有「缺 %s」这个子串 —— 第一版反例就是拿后者当探针，
    #   结果测的是另一条路（假绿）。
    probe2 = '"❌ ATVV characteristic(s) not found（缺 %s）。"\n'
    if "缺 %s" in _code_only(probe2):
        print("   ❌ [反例] 默认模式保留了字符串字面量 → 两种模式没区别")
        ok = False
    elif "缺 %s" not in _code_only(probe2, keep_strings=True):
        print("   ❌ [反例] keep_strings=True 也没保住字符串 → 正向断言不可用")
        ok = False
    else:
        print("   OK [反例] 两种模式确有区别（正向断言必须 keep_strings=True）")

    return ok


def main() -> int:
    print("=" * 60)
    print("BLE「假连接」闸 —— connection_status 说已连接，不算数")
    print("=" * 60)

    print("\n── 1. 探针：UNCACHED + 验不了返回 None ──")
    ok = check_probe()

    print("\n── 2. 连接流程：CONNECTED 之后必须真验 ──")
    ok = check_hold_logic() and ok

    print("\n── 3. ATVV 发现：穿透脏缓存 + 报出缺哪个 ──")
    ok = check_atvv_discovery() and ok

    print("\n── 4. 行为级：假 ble 跑五种情形 ──")
    ok = check_behavior() and ok

    ok = run_counter_examples() and ok

    print("\n" + ("✅ 通过" if ok else "❌ 失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
