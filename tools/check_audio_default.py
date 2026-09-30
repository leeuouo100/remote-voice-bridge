"""「遥控器优先」闸 —— 默认录音设备必须钉在 CABLE Output。

为什么单独一条闸
----------------
输入法（微信输入法）读的是**系统默认录音设备**，不是本程序选的那只。
用户插上一个 USB 麦克风（BOYA mini）之后，Windows 会自动把默认录音设备换成
它 ⇒ 输入法去听 BOYA，而遥控器的声音一路好好地写进了 `CABLE Output` ——
**没人听**。现象就是「按语音键说话，一个字都出不来」。

用户的原话：「如果和我们这个遥控器同时存在的话，优先使用我们这个遥控器，
要有这种权利。」

实现见 `audiodefault.py`（纯 ctypes 手写**未公开**的 `IPolicyConfig`：
Windows 没有公开 API 能"设置"默认音频设备，`MediaDevice.GetDefaultAudioCaptureId()`
只有 Get 没有 Set）。

这条闸盯四件容易**静默失效**的事
--------------------------------
1. **永不抛**：音频设备在别人的机器上什么怪状态都有。钉不住默认设备只是
   "少一个便利"，绝不能因此让整个桥起不来 ⇒ 对外函数必须全部自己吞异常。
2. **COM 的 vtable 下标**：手写 COM 全是魔法数字，写错一位**不报错**，
   只是行为不对（或者崩在别人机器上）。`SetDefaultEndpoint` 必须是 **13**。
3. **COM 是"按线程"初始化的**：`CoInitializeEx` 只作用于调用它的那个线程。
   所以"已初始化"这个标记**必须**是 thread-local —— 写成模块级全局的话，
   在 A 线程初始化过、B 线程就会跳过初始化，而 B 线程上的 COM 指针是
   未初始化的 ⇒ `CoCreateInstance` 以 `CO_E_NOTINITIALIZED` 失败。
   本模块就是**跨线程**用的（`run_in_executor` 丢给线程池），早晚撞上。
4. **接线**：`main.py` 里三处调用（启动 / 5 秒巡检 / 语音会话开始）缺一不可 ——
   用户是**边用边插**的，只在启动时钉一次不够。

用法
----
    python tools/check_audio_default.py

退出码 0 = 通过；1 = 失败。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── 共用工具 ──────────────────────────────────────────────────────────────────
def _code_only(src: str, keep_strings: bool = False) -> str:
    """剥掉注释与（默认还有）字符串字面量。

    ⚠ 两种用法**不能混**：
    · 负向断言（"某段老代码不许出现"）用默认 `keep_strings=False` ——
      本闸的注释里**故意**引用了老写法来说明它为什么错，不剥的话检查会
      把自己的注释当成违规。
    · 正向断言（"某段新代码必须存在"）必须 `keep_strings=True` ——
      否则 `"CABLE Output"` 会被剥成空的，断言永远找不到 = 永远红的假警报。
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
    """取模块级函数 `name` 的函数体（到下一个**顶格** `def`/`class` 为止）。

    ⚠ 判据必须只看**这个函数自己**的函数体：整文件搜 `except Exception` 会
      被别的函数"借"到 ⇒ 这个函数把守卫删了闸门还是绿的。
    """
    m = re.search(rf"^def {re.escape(name)}\(", src, re.M)
    if not m:
        return ""
    rest = src[m.end():]
    nxt = re.search(r"^(?:def |class |@)", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


# ── 1. 模块契约：对外函数「永不抛」 ──────────────────────────────────────────
_PUBLIC = ("list_capture_endpoints", "get_default_capture", "set_default_capture",
           "find_capture_by_name", "ensure_default_capture")


def _guarded(fn_src: str) -> bool:
    """函数体里有没有 `except Exception`（= "永不抛"的实现方式）。"""
    return bool(re.search(r"except\s+Exception", _code_only(fn_src)))


def check_contract(verbose: bool = True) -> bool:
    ok = True
    src = (ROOT / "audiodefault.py").read_text(encoding="utf-8", errors="replace")

    for name in _PUBLIC:
        body = _fn_body(src, name)
        if not body:
            print(f"   ❌ audiodefault.py 里找不到函数 {name}（改名了？闸门要跟着改）")
            ok = False
            continue
        if not _guarded(body):
            print(f"   ❌ {name}() 没有 `except Exception` —— 音频设备在别人的机器上"
                  f"什么怪状态都有，钉不住只是「少个便利」，"
                  f"绝不能因此让整个桥起不来")
            ok = False
    if ok:
        print(f"   OK {len(_PUBLIC)} 个对外函数都自带异常兜底（永不抛）")

    # 不许引第三方依赖（打包成 exe 后不想多带东西）
    third = [m for m in ("import comtypes", "import pycaw", "import win32com",
                         "from comtypes", "from pycaw")
             if m in _code_only(src)]
    if third:
        print(f"   ❌ audiodefault.py 引了第三方 COM 库：{third} —— "
              f"本模块的设计铁律是**纯 ctypes、零第三方依赖**")
        ok = False
    elif verbose:
        print("   OK 纯 ctypes，没有引 comtypes / pycaw / win32com")

    # 只许 import 标准库
    imports = re.findall(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", _code_only(src), re.M)
    bad = [m for m in imports
           if m.split(".")[0] not in ("ctypes", "logging", "threading", "__future__")]
    if bad:
        print(f"   ❌ 出现了预期外的 import：{sorted(set(bad))}")
        ok = False

    return ok


# ── 2. COM 的 vtable 下标（手写 COM 的魔法数字）──────────────────────────────
# 正确值：IUnknown::Release=2 / IMMDeviceEnumerator::EnumAudioEndpoints=3、
#         GetDefaultAudioEndpoint=4 / IMMDeviceCollection::GetCount=3、Item=4 /
#         IMMDevice::OpenPropertyStore=4、GetId=5 / IPropertyStore::GetValue=5 /
#         **IPolicyConfig::SetDefaultEndpoint=13**
_EXPECTED = {
    "_release": 2,              # IUnknown::Release
    "_device_id": 5,            # IMMDevice::GetId
    "_device_name": 4,          # IMMDevice::OpenPropertyStore
}


def _vtbl_index_in(fn_src: str, recv: str) -> int | None:
    """在函数体里找 `_vtbl_call(<recv>, <N>` 的 N。找不到返回 None。"""
    m = re.search(rf"_vtbl_call\(\s*{re.escape(recv)}\s*,\s*(\d+)", _code_only(fn_src))
    return int(m.group(1)) if m else None


def check_vtable(verbose: bool = True) -> bool:
    ok = True
    src = (ROOT / "audiodefault.py").read_text(encoding="utf-8", errors="replace")

    for name, want in _EXPECTED.items():
        got = _vtbl_index_in(_fn_body(src, name), "p" if name == "_release" else "dev")
        if got != want:
            print(f"   ❌ {name}() 的 vtable 下标是 {got}，应为 {want}")
            ok = False

    # SetDefaultEndpoint —— 本模块唯一"会改系统状态"的调用，下标错了最危险
    set_body = _fn_body(src, "set_default_capture")
    got = _vtbl_index_in(set_body, "pol")
    if got != 13:
        print(f"   ❌ set_default_capture() 的 vtable 下标是 {got}，"
              f"IPolicyConfig::SetDefaultEndpoint 必须是 **13** —— "
              f"写错一位不报错，只是把默认设备设到别的地方去")
        ok = False
    elif verbose:
        print("   OK SetDefaultEndpoint 下标 = 13（未公开接口，手写 COM 的命门）")

    # 三种角色都要设（Console / Multimedia / Communications）—— 只设一种的话
    # 输入法读的那个角色可能还是旧的
    roles = re.findall(r"_ROLE_(CONSOLE|MULTIMEDIA|COMMUNICATIONS)", _code_only(set_body))
    if sorted(set(roles)) != ["COMMUNICATIONS", "CONSOLE", "MULTIMEDIA"]:
        print(f"   ❌ set_default_capture() 只设了 {sorted(set(roles))} 这些角色 —— "
              f"必须三种都设（Console/Multimedia/Communications），"
              f"否则输入法读的那个角色可能还是旧的")
        ok = False
    elif verbose:
        print("   OK 三种角色（Console/Multimedia/Communications）一起设")

    # GUID 常量（写错就拿到别的接口，表现为 HRESULT 失败或诡异行为）
    for const, want in (("_CLSID_MMDeviceEnumerator", "BCDE0395-E52F-467C-8E3D-C4579291692E"),
                        ("_IID_IMMDeviceEnumerator", "A95664D2-9614-4F35-A746-DE8DB63617E6"),
                        ("_CLSID_PolicyConfigClient", "870AF99C-171D-4F9E-AF0D-E63DF40C2BC9"),
                        ("_IID_IPolicyConfig", "F8679F50-850A-41CF-9C72-430F290290C8")):
        if want.lower() not in src.lower():
            print(f"   ❌ {const} 不是 {want}")
            ok = False
    if ok and verbose:
        print("   OK 四个 GUID 常量都对")

    return ok


# ── 3. COM 的「按线程」初始化 ────────────────────────────────────────────────
def _uses_threadlocal(src: str) -> bool:
    """`_ensure_com` 的"已初始化"标记必须是 thread-local。

    ⚠ 判据只看**标记本身**：`threading.local()` 出现了、且**没有**模块级的
      `_co_ready = <值>`。写成全局 bool 的话，B 线程会跳过 `CoInitializeEx`。
    """
    code = _code_only(src)
    if "threading.local()" not in code:
        return False
    # 模块级 `_co_ready = ...`（顶格）不许存在
    if re.search(r"^_co_ready\s*=", code, re.M):
        return False
    return True


def check_com_threading(verbose: bool = True) -> bool:
    src = (ROOT / "audiodefault.py").read_text(encoding="utf-8", errors="replace")
    if not _uses_threadlocal(src):
        print("   ❌ `_ensure_com` 的初始化标记不是 thread-local —— "
              "COM 是**按线程**初始化的（CoInitializeEx 只作用于调用它的那个线程），"
              "写成模块级全局的话 B 线程会跳过初始化，"
              "CoCreateInstance 以 CO_E_NOTINITIALIZED 失败")
        return False
    if verbose:
        print("   OK 初始化标记是 thread-local（跨线程调用安全）")
    return True


# ── 4. 接线：main.py 三处调用 + 配置开关 ─────────────────────────────────────
def _main_calls(src: str) -> dict[str, bool]:
    """main.py 里该有的三处调用，逐条判定。"""
    code = _code_only(src, keep_strings=True)
    return {
        "启动时": bool(re.search(
            r'await\s+_ensure_default_capture\(\s*cfg\s*,\s*reason="启动时检查默认录音设备"',
            code)),
        "巡检": bool(re.search(
            r"if\s*\(\s*cfg\.force_default_capture", code)
            and "_CAPTURE_CHECK_INTERVAL" in code),
        "会话开始": bool(re.search(
            r'run_coroutine_threadsafe\(\s*_ensure_default_capture\(\s*cfg\s*,'
            r'\s*reason="语音会话开始"', code)),
        "开关短路": bool(re.search(
            r"if\s+not\s+cfg\.force_default_capture:\s*\n\s*return", code)),
    }


def check_wiring(verbose: bool = True) -> bool:
    ok = True
    src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")
    got = _main_calls(src)
    missing = [k for k, v in got.items() if not v]
    if missing:
        print(f"   ❌ main.py 缺了：{missing}")
        print("      · 启动时 —— 桥刚起来就钉一次")
        print("      · 巡检   —— 用户**边用边插**，桥不会因此重连，必须定期复核")
        print("      · 会话开始 —— 用户可能刚插上 BOYA 就按语音键（最要紧的一刻）")
        print("      · 开关短路 —— 用户明确关了就不许改他的设置")
        ok = False
    elif verbose:
        print("   OK main.py 三处调用 + 开关短路都在")

    # 投递必须走 run_coroutine_threadsafe（on_control 跑在 BLE 回调线程上）
    code = _code_only(src, keep_strings=True)
    m = re.search(r"# ── 「遥控器优先」：会话开始的这一刻(.*?)run_coroutine_threadsafe",
                  src, re.S)
    if m is None:
        print("   ⚠ 找不到「会话开始」那一段的锚点注释（判据可能失效，请核对）")
    return ok


def check_config(verbose: bool = True) -> bool:
    ok = True
    src = (ROOT / "config.py").read_text(encoding="utf-8", errors="replace")
    # ⚠ 必须 keep_strings=True —— 要判的就是 `= "CABLE Output"` 那个**字符串字面量**，
    #   默认模式会把它剥掉，判据就永远红了（这正是"正向断言必须留字符串"）。
    code = _code_only(src, keep_strings=True)
    if not re.search(r"force_default_capture:\s*bool\s*=\s*True", code):
        print("   ❌ config.py 里没有 `force_default_capture: bool = True`")
        ok = False
    if not re.search(r'capture_device_name:\s*str\s*=\s*"CABLE Output"', code):
        print('   ❌ config.py 里没有 `capture_device_name: str = "CABLE Output"`')
        ok = False
    if ok and verbose:
        print("   OK 配置项齐全（默认开 + 目标设备名可配）")

    # 真跑一遍 Config()，确认默认值和序列化（静态判据看不出 to_dict 漏字段）
    try:
        sys.path.insert(0, str(ROOT))
        import config as _cfg
        c = _cfg.Config()
        if c.force_default_capture is not True:
            print(f"   ❌ Config().force_default_capture 默认是 {c.force_default_capture!r}，应为 True")
            ok = False
        if c.capture_device_name != "CABLE Output":
            print(f"   ❌ Config().capture_device_name 默认是 {c.capture_device_name!r}")
            ok = False
        d = c.to_dict()
        for k in ("force_default_capture", "capture_device_name"):
            if k not in d:
                print(f"   ❌ to_dict() 里没有 {k} —— 保存后重启会丢")
                ok = False
        if ok and verbose:
            print("   OK Config() 默认值 + to_dict() 序列化都对")
    except Exception as e:                            # noqa: BLE001
        print(f"   ❌ 实例化 Config() 失败：{type(e).__name__}: {e}")
        ok = False
    return ok


# ── 5. 行为级：真调用不抛（含**跨线程**，这是 thread-local 那条的实证）────────
def check_behavior(verbose: bool = True) -> bool:
    ok = True
    try:
        import audiodefault as A
    except Exception as e:                            # noqa: BLE001
        print(f"   ❌ import audiodefault 失败：{type(e).__name__}: {e}")
        return False

    # ① 正常只读调用不许抛（CI 上没有音频端点时应返回空值，也不许抛）
    try:
        cur = A.get_default_capture()
        if not (isinstance(cur, tuple) and len(cur) == 2):
            print(f"   ❌ get_default_capture() 返回 {cur!r}，应为二元组")
            ok = False
        elif verbose:
            print(f"   OK get_default_capture() → {cur[1] or '(无默认录音设备)'}")
    except Exception as e:                            # noqa: BLE001
        print(f"   ❌ get_default_capture() 抛了：{type(e).__name__}: {e}")
        ok = False

    # ② 垃圾输入不许抛（这条**真的会走到** except / 错误分支）
    for call, desc in ((lambda: A.set_default_capture(""), "空 device_id"),
                       (lambda: A.set_default_capture("{这不是一个合法的端点ID}"),
                        "非法端点 ID"),
                       (lambda: A.ensure_default_capture("绝对不存在的设备名"),
                        "找不到目标设备")):
        try:
            r = call()
            if verbose:
                print(f"   OK {desc} → {r!r}（不抛）")
        except Exception as e:                        # noqa: BLE001
            print(f"   ❌ {desc} 抛了：{type(e).__name__}: {e} —— 对外函数必须永不抛")
            ok = False

    # ③ 跨线程调用（thread-local 的实证）。
    #    写成模块级全局 `_co_ready` 的话，这里在**新线程**上会跳过
    #    CoInitializeEx ⇒ CoCreateInstance 以 CO_E_NOTINITIALIZED 失败。
    import threading
    box: dict = {}

    def _worker():
        try:
            box["v"] = A.get_default_capture()
        except Exception as e:                        # noqa: BLE001
            box["err"] = f"{type(e).__name__}: {e}"

    A.get_default_capture()                           # 先在**主线程**初始化一次
    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=20)
    if "err" in box:
        print(f"   ❌ 子线程里调用抛了：{box['err']} —— "
              f"COM 的初始化标记没有按线程隔离（thread-local 那条判据是假的）")
        ok = False
    elif "v" not in box:
        print("   ❌ 子线程 20 秒没返回（COM 调用挂死？）")
        ok = False
    elif verbose:
        print(f"   OK 跨线程调用可用（子线程 → {box['v'][1] or '(无默认录音设备)'}）")

    return ok


# ── 6. 反例自证 ──────────────────────────────────────────────────────────────
def run_counter_examples() -> bool:
    """把每条判据的"反例"喂进去，确认它们**真的会变红**。

    ⚠ 反例必须走**同一个判据函数**（否则反例验的是另一条路 = 假绿）。
    """
    ok = True
    print("\n── 反例自证（判据真的会红吗）──")

    # 反例 1：函数没有 except → _guarded 必须为 False
    if _guarded("def f():\n    return 1\n"):
        print("   ❌ [反例] 无 except 的函数被判成「有兜底」→ 「永不抛」判据无效")
        ok = False
    else:
        print("   OK [反例] 去掉 except → 「永不抛」判据变红")

    # 反例 2：SetDefaultEndpoint 下标写错 → 必须能被抓
    if _vtbl_index_in("    _vtbl_call(pol, 12, ctypes.c_long, [], x)",
                      "pol") == 13:
        print("   ❌ [反例] 下标 12 被判成 13 → vtable 判据无效")
        ok = False
    else:
        print("   OK [反例] 下标写成 12 → vtable 判据变红")

    # 反例 3：模块级全局标记 → thread-local 判据必须变红
    bad = "_co_ready = False\n\n\ndef _ensure_com():\n    global _co_ready\n"
    if _uses_threadlocal(bad):
        print("   ❌ [反例] 模块级全局 `_co_ready` 被判成 thread-local → 判据无效")
        ok = False
    else:
        print("   OK [反例] 退回模块级全局 `_co_ready` → thread-local 判据变红")

    # 反例 4：main.py 少一处调用 → 接线判据必须变红
    good_src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")
    stripped = re.sub(
        r'await\s+_ensure_default_capture\(\s*cfg\s*,\s*reason="启动时检查默认录音设备"',
        "pass", good_src)
    if _main_calls(stripped)["启动时"]:
        print("   ❌ [反例] 删掉启动调用后判据仍是绿的 → 接线判据无效")
        ok = False
    else:
        print("   OK [反例] 删掉启动调用 → 接线判据变红")

    # 反例 5：注释里引用老写法不许被误判（_code_only 生效）
    probe = "# 老写法：_co_ready = False  ← 模块级全局，错的\n"
    if re.search(r"^_co_ready\s*=", _code_only(probe), re.M):
        print("   ❌ [反例] 注释里的老写法被误判 → 闸门会对着自己的注释报红")
        ok = False
    else:
        print("   OK [反例] 注释里引用老写法不会被误判（_code_only 生效）")

    # 反例 6：正向断言必须 keep_strings=True，否则永远找不到
    probe2 = 'reason="语音会话开始"\n'
    if 'reason="语音会话开始"' in _code_only(probe2):
        print("   ❌ [反例] 默认模式保留了字符串字面量 → 两种模式没区别，"
              "keep_strings 这个参数是摆设")
        ok = False
    elif 'reason="语音会话开始"' not in _code_only(probe2, keep_strings=True):
        print("   ❌ [反例] keep_strings=True 也没保住字符串 → 正向断言不可用")
        ok = False
    else:
        print("   OK [反例] 两种模式确有区别（正向断言必须 keep_strings=True）")

    return ok


def main() -> int:
    print("=" * 60)
    print("「遥控器优先」闸 —— 默认录音设备必须钉在 CABLE Output")
    print("=" * 60)

    print("\n── 1. 模块契约：对外函数永不抛 / 零第三方依赖 ──")
    ok = check_contract()

    print("\n── 2. 手写 COM 的 vtable 下标与 GUID ──")
    ok = check_vtable() and ok

    print("\n── 3. COM 按线程初始化（thread-local）──")
    ok = check_com_threading() and ok

    print("\n── 4. 接线：main.py 三处调用 + 配置开关 ──")
    ok = check_wiring() and ok

    print("\n── 5. 配置项 ──")
    ok = check_config() and ok

    print("\n── 6. 行为：真调用不抛（含跨线程）──")
    ok = check_behavior() and ok

    ok = run_counter_examples() and ok

    print("\n" + ("✅ 通过" if ok else "❌ 失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
