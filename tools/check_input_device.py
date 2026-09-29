"""电脑麦克风解析闸 —— 不许把"本程序自己的输出"当成麦克风。

为什么单独一条闸
----------------
本程序把混音结果**写进** `CABLE Input`，而 VB-CABLE 会把 `CABLE Input` 上的东西
原样送到 `CABLE Output`。一旦"电脑麦克风"解析到 `CABLE Output`，就等于
**把自己的输出采回来再混进去** —— 闭环。

2026-09-29 真机实测（控制台 `/api/state`）：

    levels.sys    = -10.6 dBFS   ← 系统麦克风（= CABLE Output = 自己的输出回灌）
    levels.remote = -31.4 dBFS   ← 遥控器麦克风

回灌进来的自己比遥控器还响 **24 dB** ⇒ 送到输入法的信号里约一半是延迟回声，
用户报的就是「语音输入时好时坏、有时候一个字都识别不出来」。

原代码的守卫**写在了错的地方**：过滤只加在 `list_input_devices()`（喂下拉框），
而真正决定"开哪只设备"的两处都绕过了它 —— 而且绕过的原因是同一个：

    `sd.default.device` 的类型是 `_InputOutputPair`，**不是 list/tuple**，
    所以 `isinstance(dev, (list, tuple))` 判出来是 False。

    · `mixer.find_input_device("")`  → 拿对象和 0 比大小 → TypeError → 被
      `except` 吞掉 → 退回 `query_devices(kind="input")` → **恰好绕过过滤**；
    · `console_server._resolve_in_device` → 静默掉到 `in_list[0]` → 报出
      `Microsoft Sound Mapper - Input` 并打**绿勾**。

⇒ 用户看到「检查项全绿，语音却识别不出字」。

这条闸用**假的 sounddevice** 把上面每种情形都摆出来跑一遍，并带反例自证。
假模块里的 `_Pair` 刻意做成"不是 list/tuple 但支持 [i]"，与真实类型一致 ——
否则闸门自己就复现不出那个坑。

用法
----
    python tools/check_input_device.py

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


# ── 假的 sounddevice ──────────────────────────────────────────────────────────
class _Pair:
    """模仿 sounddevice 的 `_InputOutputPair`。

    ⚠⚠ 关键点：它**不是** `list`/`tuple` 的子类，只支持 `[i]`。
       这正是历史上两处代码静默跑偏的根因 —— 如果这里图省事用 list，
       这条闸就永远复现不出那个 bug，等于白写。
       另外它**没有** `__lt__`，所以 `pair < 0` 会抛 TypeError，
       与真实类型的行为一致（老代码就是被这个 TypeError 推进 except 的）。
    """

    def __init__(self, pair):
        self._pair = tuple(pair)

    def __getitem__(self, i):
        return self._pair[i]

    def __len__(self):
        return len(self._pair)

    def __repr__(self):
        return f"_InputOutputPair({list(self._pair)!r})"


def _dev(name: str, ins: int = 2, outs: int = 0, rate: int = 48000) -> dict:
    return {"name": name, "max_input_channels": ins,
            "max_output_channels": outs, "default_samplerate": rate}


class FakeSD:
    """只实现本闸用到的那几个方法，形状与 sounddevice 对齐。"""

    def __init__(self, devices, default_in=None, kind_input=None):
        self.devices = list(devices)
        self.default = _Pair([-1 if default_in is None else default_in, 0])
        self._kind_input = default_in if kind_input is None else kind_input

    def query_devices(self, device=None, kind=None):
        if kind is not None:                       # kind="input"
            idx = self._kind_input
            if idx is None or idx < 0:
                raise RuntimeError("no default input device")
            d = dict(self.devices[idx])
            d["index"] = idx
            return d
        if device is None:
            return [dict(d, index=i) for i, d in enumerate(self.devices)]
        if isinstance(device, str):
            for i, d in enumerate(self.devices):
                if d["name"] == device:
                    dd = dict(d)
                    dd["index"] = i
                    return dd
            raise ValueError(device)
        if isinstance(device, int) and 0 <= device < len(self.devices):
            d = dict(self.devices[device])
            d["index"] = device
            return d
        raise ValueError(f"bad device index {device!r}")


def _install(sd_obj):
    old = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = sd_obj
    return old


def _restore(old):
    if old is None:
        sys.modules.pop("sounddevice", None)
    else:
        sys.modules["sounddevice"] = old


# ── 设备表 ────────────────────────────────────────────────────────────────────
CABLE_OUT = "CABLE Output (VB-Audio Virtual Cable)"
CABLE_IN = "CABLE Input (VB-Audio Virtual Cable)"
REAL_MIC = "麦克风阵列 (Realtek(R) Audio)"
MAPPER = "Microsoft Sound Mapper - Input"

# 本机的真实布局：**系统默认输入就是 CABLE Output**
DEVICES_DEFAULT_IS_LOOPBACK = [_dev(CABLE_OUT), _dev(REAL_MIC), _dev(MAPPER)]
# 一台没有任何真实麦克风的机器（只有虚拟声卡）
DEVICES_NO_REAL_MIC = [_dev(CABLE_OUT), _dev(CABLE_IN, ins=0, outs=2)]
# 系统默认就是真实麦克风
DEVICES_DEFAULT_IS_MIC = [_dev(CABLE_OUT), _dev(REAL_MIC)]


def _resolve(devices, default_in, name=""):
    """在假设备表下跑一次 resolve_input_device。"""
    import mixer
    old = _install(FakeSD(devices, default_in))
    try:
        return mixer.resolve_input_device(name)
    finally:
        _restore(old)


# ── 1. 名单 ───────────────────────────────────────────────────────────────────
def check_markers(verbose: bool = True) -> bool:
    import mixer

    ok = True
    for nm in (CABLE_OUT, CABLE_IN, "CABLE 2 Input (VB-Audio Cable A)",
               "CABLE Output", "Voicemeeter Input", "Output (VB-Audio Point)"):
        if not mixer.is_loopback_name(nm):
            print(f"   ❌ 「{nm}」没被认成回环设备 —— 它会把本程序自己的输出喂回来")
            ok = False
    for nm in (REAL_MIC, "麦克风 (USB Audio Device)", "Microphone (HD Webcam)"):
        if mixer.is_loopback_name(nm):
            print(f"   ❌ 「{nm}」被误判成回环设备（这是真实麦克风，不该挡）")
            ok = False

    for nm in (MAPPER, "主声音捕获驱动程序", "主声音驱动程序"):
        if not mixer.is_alias_name(nm):
            print(f"   ❌ 「{nm}」没被认成别名设备 —— 它指向当前默认，"
                  f"而默认可能就是 CABLE Output")
            ok = False
    if mixer.is_alias_name(REAL_MIC):
        print(f"   ❌ 「{REAL_MIC}」被误判成别名设备")
        ok = False

    if ok:
        print("   OK 回环名单与别名名单都对（含 4 只真实麦克风不误伤）")
    return ok


# ── 2. 解析行为 ───────────────────────────────────────────────────────────────
def check_resolve(verbose: bool = True) -> bool:
    import mixer

    ok = True

    # ① 本机形态：默认 = CABLE Output → 必须**回退到真实麦克风**，绝不返回回环
    idx, label, reason = _resolve(DEVICES_DEFAULT_IS_LOOPBACK, 0)
    if idx is None or mixer.is_loopback_name(label):
        print(f"   ❌ 默认设备是 CABLE Output 时解析成了 {idx}/{label!r}"
              f"（reason={reason}）—— 这就是自听自的回环")
        ok = False
    elif reason != "ok":
        print(f"   ❌ 应当回退到真实麦克风并报 ok，实际 reason={reason}")
        ok = False
    else:
        print(f"   OK 默认是 CABLE Output → 回退到「{label}」(idx={idx})")

    # ② 没有任何真实麦克风 → 必须**明确放弃**，不许退回默认
    idx, label, reason = _resolve(DEVICES_NO_REAL_MIC, 0)
    if idx is not None:
        print(f"   ❌ 没有真实麦克风时仍然返回了 idx={idx}（{label!r}）—— "
              f"退回默认＝退回那个闭环")
        ok = False
    elif reason != "loopback":
        print(f"   ❌ 原因码应当是 loopback（说清「为什么没有」），实际 {reason}")
        ok = False
    else:
        print(f"   OK 没有真实麦克风 → 返回 None + reason=loopback（不退回默认）")

    # ③ 显式指定回环设备 → 拒绝
    idx, label, reason = _resolve(DEVICES_DEFAULT_IS_LOOPBACK, 1, CABLE_OUT)
    if idx is not None or reason != "loopback":
        print(f"   ❌ 显式指定 CABLE Output 没有被拒绝：idx={idx} reason={reason}")
        ok = False
    else:
        print("   OK 显式指定 CABLE Output → 拒绝（loopback）")

    # ④ 显式指定别名设备 → 拒绝
    idx, label, reason = _resolve(DEVICES_DEFAULT_IS_LOOPBACK, 1, MAPPER)
    if idx is not None or reason != "alias":
        print(f"   ❌ 显式指定 Sound Mapper 没有被拒绝：idx={idx} reason={reason}")
        ok = False
    else:
        print("   OK 显式指定 Sound Mapper → 拒绝（alias）")

    # ⑤ 显式指定不存在的设备 → missing（而不是静默用默认）
    idx, label, reason = _resolve(DEVICES_DEFAULT_IS_LOOPBACK, 1, "某只已拔掉的麦克风")
    if idx is not None or reason != "missing":
        print(f"   ❌ 设备不存在时应报 missing，实际 idx={idx} reason={reason}")
        ok = False
    else:
        print("   OK 设备不存在 → missing（不静默退回默认）")

    # ⑥ 默认就是真实麦克风 → 直接用
    idx, label, reason = _resolve(DEVICES_DEFAULT_IS_MIC, 1)
    if idx != 1 or reason != "ok":
        print(f"   ❌ 默认是真实麦克风时应当直接用，实际 idx={idx} reason={reason}")
        ok = False
    else:
        print(f"   OK 默认是真实麦克风 → 直接用「{label}」")

    # ⑦ 显式指定真实麦克风 → 正常
    idx, label, reason = _resolve(DEVICES_DEFAULT_IS_LOOPBACK, 0, REAL_MIC)
    if idx is None or reason != "ok":
        print(f"   ❌ 显式指定真实麦克风被拒了：idx={idx} reason={reason}")
        ok = False
    else:
        print(f"   OK 显式指定真实麦克风 → 「{label}」")

    # ⑧ find_input_device 也必须安全（它是运行时的旧入口）
    old = _install(FakeSD(DEVICES_DEFAULT_IS_LOOPBACK, 0))
    try:
        got = mixer.find_input_device("")
        # ⚠ 名字必须在**假模块还装着的时候**取。放到 finally 之后取，
        #   `_device_name` 会去查真实设备表，索引 1 恰好也是 CABLE Output，
        #   于是这条断言会**误报**（闸门第一版就踩了）。
        got_name = mixer._device_name(got) if got is not None else ""
    finally:
        _restore(old)
    if got is None or mixer.is_loopback_name(got_name):
        print(f"   ❌ find_input_device('') 返回了 {got}（{got_name!r}）—— 仍是回环设备")
        ok = False
    else:
        print(f"   OK find_input_device('') = {got}（{got_name}）")

    return ok


# ── 3. 下拉框列表 ─────────────────────────────────────────────────────────────
def check_device_list(verbose: bool = True) -> bool:
    import mixer

    old = _install(FakeSD(DEVICES_DEFAULT_IS_LOOPBACK + [_dev("Voicemeeter Output")], 0))
    try:
        names = mixer.list_input_devices()
    finally:
        _restore(old)

    bad = [n for n in names if mixer.is_loopback_name(n) or mixer.is_alias_name(n)]
    if bad:
        print(f"   ❌ 下拉框里还有回环/别名设备：{bad} —— 等于给用户第二个踩坑入口")
        return False
    print(f"   OK 下拉框已剔除回环与别名设备：{names}")
    return True


# ── 4. 控制台检查项与运行时同源 ───────────────────────────────────────────────
def check_console_same_source(verbose: bool = True) -> bool:
    """控制台检查项必须和运行时给出**同一个**结论，且回环时报红。"""
    try:
        import console_server as cs
    except Exception as e:                          # noqa: BLE001
        print(f"   ⚠ 跳过：控制台模块导不进来（{e}）")
        return True

    class _Cfg:
        system_mic_device = ""
        system_mic_enabled = True

    ok = True
    for devices, default_in, want_ok in (
            (DEVICES_DEFAULT_IS_LOOPBACK, 0, True),    # 能回退到真实麦克风 → 绿
            (DEVICES_NO_REAL_MIC, 0, False),           # 只剩回环 → 必须红
    ):
        old = _install(FakeSD(devices, default_in))
        try:
            name, mic_ok, reason = cs._resolve_in_device(_Cfg())
        finally:
            _restore(old)
        if mic_ok != want_ok:
            print(f"   ❌ 检查项 ok={mic_ok}，期望 {want_ok}"
                  f"（设备={name!r} reason={reason}）")
            ok = False
        elif not want_ok and "CABLE" not in name.upper():
            print(f"   ❌ 报红时应当把**真正出问题的设备名**说出来，实际 {name!r}")
            ok = False
        else:
            print(f"   OK 检查项与运行时同源：{name!r} ok={mic_ok} reason={reason}")
    return ok


def _code_only(src: str, keep_strings: bool = False) -> str:
    """剥掉注释与（默认还有）字符串字面量。

    ⚠⚠ 两种用法**不能混**，混了就出错（闸门第一版两个坑都踩了）：

    · 负向断言（"某段老代码不许出现"）用默认的 `keep_strings=False` ——
      本闸的注释里**故意**引用了老形态
      （`isinstance(dev, (list, tuple))`）来说明它为什么错，
      不剥的话检查会把自己的注释当成违规。
    · 正向断言（"某段新代码必须存在"）必须 `keep_strings=True` ——
      否则 `query_devices(kind="input")` 会被剥成 `query_devices(kind=)`，
      断言永远找不到，变成一条**永远红的假警报**。
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


# ── 5. 静态：那两处老形态不许复活 ─────────────────────────────────────────────
def check_static(verbose: bool = True) -> bool:
    ok = True
    mixer_src = (ROOT / "mixer.py").read_text(encoding="utf-8", errors="replace")
    cs_src = (ROOT / "console_server.py").read_text(encoding="utf-8", errors="replace")
    mixer_code = _code_only(mixer_src)                        # 负向：连字符串一起剥
    cs_code = _code_only(cs_src)
    mixer_nc = _code_only(mixer_src, keep_strings=True)       # 正向：保留字符串字面量
    cs_nc = _code_only(cs_src, keep_strings=True)

    # ① `isinstance(dev, (list, tuple))` 是对 `_InputOutputPair` 失效的那个写法
    for fname, code in (("mixer.py", mixer_code), ("console_server.py", cs_code)):
        if re.search(r"isinstance\(\s*dev\s*,\s*\(?\s*list\s*,\s*tuple", code):
            print(f"   ❌ {fname} 又出现了 `isinstance(dev, (list, tuple))` —— "
                  f"`sd.default.device` 是 `_InputOutputPair`，对它判 False，"
                  f"这正是两处静默跑偏的根因")
            ok = False
    if ok:
        print("   OK 没有 `isinstance(dev, (list, tuple))` 这个老形态")

    # ② 默认设备必须走 query_devices(kind="input")
    if 'query_devices(kind="input")' not in mixer_nc:
        print('   ❌ mixer.py 里没有 `query_devices(kind="input")` —— '
              "默认输入设备只能用这个拿，它返回的 dict 带 index")
        ok = False
    else:
        print('   OK 默认输入设备用 query_devices(kind="input") 取')

    # ③ 控制台检查项必须复用同一个函数，不许自己再写一份
    if "resolve_input_device" not in cs_nc:
        print("   ❌ console_server.py 没有复用 mixer.resolve_input_device —— "
              "同一份判断有两个出处，两边迟早不一致（真机上就发生过）")
        ok = False
    else:
        print("   OK 控制台检查项复用 mixer.resolve_input_device")

    return ok


# ── 6. 软限幅 ─────────────────────────────────────────────────────────────────
def _limiter_ok(sl) -> tuple[bool, str]:
    """返回 (是否合规, 说明)。"""
    knee = 0.70
    # 拐点以下严格线性
    for x in (0.0, 0.05, 0.2, 0.5, 0.7, -0.05, -0.5, -0.7):
        if abs(sl(x) - x) > 1e-12:
            return False, f"拐点以下被改动了：x={x} → {sl(x)}（应原样通过）"
    # 单调递增
    xs = [i / 100.0 for i in range(-2000, 2001)]
    ys = [sl(x) for x in xs]
    if any(b < a for a, b in zip(ys, ys[1:])):
        return False, "不是单调递增（会有非线性失真）"
    # 奇对称
    for x in (0.9, 2.0, 10.0):
        if abs(sl(x) + sl(-x)) > 1e-12:
            return False, f"不是奇对称：x={x}"
    # 输出恒在 int16 范围内（这才是"不再硬削顶"的硬指标）
    for i in range(-300000, 300001, 13):
        iv = int(sl(i / 1000.0) * 32767)
        if not (-32768 <= iv <= 32767):
            return False, f"越界：x={i / 1000.0} → {iv}"
    # 大输入必须被压到接近满量程但**到不了**
    if not (0.98 < sl(10.0) < 1.0):
        return False, f"x=10 时输出 {sl(10.0)}，期望压到 1.0 以内"
    return True, "拐点以下严格线性 / 单调 / 奇对称 / 输出恒在 int16 内"


def check_limiter(verbose: bool = True) -> bool:
    import main

    good, why = _limiter_ok(main._soft_limit)
    if not good:
        print(f"   ❌ 软限幅不合规：{why}")
        return False
    print(f"   OK 软限幅：{why}")

    main_src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")
    # 负向用默认（连字符串一起剥，杜绝"注释里写了就算过"）；
    # 正向用 keep_strings=True（否则带引号的写法会被剥掉，变成永远红的假警报）。
    main_code = _code_only(main_src)
    main_nc = _code_only(main_src, keep_strings=True)
    if "if iv > 32767:" in main_code or "iv = 32767" in main_code:
        print("   ❌ main.py 里还有手写硬削顶（`if iv > 32767: iv = 32767`）—— "
              "真机上遥控器解码峰值本来就到满量程，乘大增益后整段被切平")
        return False
    print("   OK main.py 里没有手写硬削顶")

    if "mix_limit_pct" not in main_nc:
        print("   ❌ main.py 没有把限幅占比报给 state —— "
              "「增益是不是开太大」又只能靠耳朵猜")
        return False
    print("   OK 限幅占比已上报（mix_limit_pct）")
    return True


# ── 反例自证 ──────────────────────────────────────────────────────────────────
def run_counter_examples(verbose: bool = True) -> bool:
    """每条断言都要能变红，否则等于没有断言。"""
    import mixer

    print("\n── 反例自证 ──")
    ok = True

    # 反例 1：清空回环名单 → 「默认是 CABLE Output」那条必须变红
    orig_markers = mixer._LOOPBACK_MARKERS
    mixer._LOOPBACK_MARKERS = ()
    try:
        idx, label, reason = _resolve(DEVICES_DEFAULT_IS_LOOPBACK, 0)
        if idx == 0 or mixer.is_loopback_name(CABLE_OUT) is False:
            # 期望：此时它**认不出**回环 → 会直接用默认设备（idx=0）
            if idx == 0:
                print("   OK [反例] 清空回环名单 → 直接用 CABLE Output（idx=0）"
                      "⇒ 「回环必须被挡」这条检查有效")
            else:
                print(f"   ❌ [反例] 清空回环名单后仍返回 idx={idx} → 检查无效")
                ok = False
        else:
            print(f"   ❌ [反例] 清空回环名单后仍返回 idx={idx} → 检查无效")
            ok = False
    finally:
        mixer._LOOPBACK_MARKERS = orig_markers

    # 反例 2：把 is_loopback_name 改成永远 False（＝老代码的实际效果）
    orig_fn = mixer.is_loopback_name
    mixer.is_loopback_name = lambda name: False
    try:
        idx, label, reason = _resolve(DEVICES_DEFAULT_IS_LOOPBACK, 0)
        if idx == 0:
            print("   OK [反例] 让回环判断恒为假 → 直接吃下 CABLE Output"
                  "⇒ 检查有效")
        else:
            print(f"   ❌ [反例] 回环判断恒为假时仍返回 idx={idx} → 检查无效")
            ok = False
    finally:
        mixer.is_loopback_name = orig_fn

    # 反例 3：把"没有真实麦克风"改成退回默认（＝最危险的写法）
    orig_first = mixer._first_real_input
    mixer._first_real_input = lambda: 0            # 假装"第一只真实麦克风"就是默认
    try:
        idx, label, reason = _resolve(DEVICES_NO_REAL_MIC, 0)
        if idx is None:
            print("   ❌ [反例] 把回退改成「直接给默认索引」后仍返回 None → 检查无效")
            ok = False
        else:
            print(f"   OK [反例] 让它「退回默认」→ 立刻吃下 idx={idx}"
                  f"（{label!r}）⇒ 「宁可放弃也不退回默认」这条检查有效")
    finally:
        mixer._first_real_input = orig_first

    # 反例 4：把软限幅换回硬削顶 → 限幅检查必须变红
    import main
    orig_sl = main._soft_limit
    main._soft_limit = lambda x: max(-1.0, min(1.0, x))       # 硬削顶
    try:
        good, why = _limiter_ok(main._soft_limit)
        if good:
            print("   ❌ [反例] 换成硬削顶后仍判定合规 → 限幅检查无效")
            ok = False
        else:
            print(f"   OK [反例] 换回硬削顶 → 检查变红（{why}）⇒ 限幅检查有效")
    finally:
        main._soft_limit = orig_sl

    # 反例 5：老形态 `isinstance(dev, (list, tuple))` 必须能被静态检查抓到
    probe = ('dev = sd.default.device\n'
             'idx = dev[0] if isinstance(dev, (list, tuple)) else dev\n')
    if not re.search(r"isinstance\(\s*dev\s*,\s*\(?\s*list\s*,\s*tuple", _code_only(probe)):
        print("   ❌ [反例] 静态检查抓不到老形态 → 那条断言无效")
        ok = False
    else:
        print("   OK [反例] 老形态能被静态检查抓到 ⇒ 那条断言有效")

    # 反例 6：把老形态**写在注释里**不许被误判（否则闸门会对着自己的注释报红）
    probe2 = "# 老写法：idx = dev[0] if isinstance(dev, (list, tuple)) else dev\n"
    if re.search(r"isinstance\(\s*dev\s*,\s*\(?\s*list\s*,\s*tuple", _code_only(probe2)):
        print("   ❌ [反例] 注释里的老形态被误判成违规 → 闸门会假红")
        ok = False
    else:
        print("   OK [反例] 注释里引用老形态不会被误判（_code_only 生效）")

    # 反例 7：正向断言**必须** keep_strings=True，否则会变成永远红的假警报
    probe3 = 'sd.query_devices(kind="input")\n'
    if 'query_devices(kind="input")' in _code_only(probe3):
        print("   ❌ [反例] 默认模式居然保留了字符串字面量 → 两种模式没区别，"
              "keep_strings 这个参数是摆设")
        ok = False
    elif 'query_devices(kind="input")' not in _code_only(probe3, keep_strings=True):
        print("   ❌ [反例] keep_strings=True 也没保住字符串字面量 → 正向断言不可用")
        ok = False
    else:
        print("   OK [反例] 两种模式确有区别（正向断言必须 keep_strings=True）")

    return ok


def main() -> int:
    print("=" * 60)
    print("电脑麦克风解析闸 —— 不许把本程序自己的输出当成麦克风")
    print("=" * 60)

    print("\n── 1. 回环 / 别名设备名单 ──")
    ok = check_markers()

    print("\n── 2. resolve_input_device 行为 ──")
    ok = check_resolve() and ok

    print("\n── 3. 下拉框列表 ──")
    ok = check_device_list() and ok

    print("\n── 4. 控制台检查项与运行时同源 ──")
    ok = check_console_same_source() and ok

    print("\n── 5. 静态：老形态不许复活 ──")
    ok = check_static() and ok

    print("\n── 6. 软限幅 ──")
    ok = check_limiter() and ok

    ok = run_counter_examples() and ok

    print("\n" + ("✅ 通过" if ok else "❌ 失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
