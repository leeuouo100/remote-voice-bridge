# -*- coding: utf-8 -*-
"""把 Windows 的默认**录音**设备钉在 `CABLE Output` 上（遥控器优先）。

## 为什么需要它（2026-09-30 用户真机）

用户插上一个 USB 麦克风（BOYA mini）之后，Windows 自动把「默认录音设备」换成了它。
而输入法（微信输入法）读的就是**系统默认录音设备** ⇒ 它去听 BOYA 了。
遥控器的声音其实一路好好地写进了 `CABLE Output`，**输入法根本听不到** ——
现象就是「按语音键说话，一个字都出不来 / 麦克风音量很低」。

用户的原话：**「如果和我们这个遥控器同时存在的话，优先使用我们这个遥控器」**。
所以这里做的是：只要默认录音设备不是 `CABLE Output`，就把它改回来。

## 为什么是手写 COM

Windows **没有公开 API 能"设置"默认音频设备** —— WinRT 的
`MediaDevice.GetDefaultAudioCaptureId()` 只有 Get，没有 Set。唯一入口是
**未公开的** `IPolicyConfig` 接口。这里用纯 ctypes 手写 vtable，
不引第三方依赖（`pycaw`/`comtypes` 都没装，打包成 exe 后也不想多带东西）。

## ⚠ 本模块**永不抛异常**

音频设备在别人的机器上什么怪状态都有。钉不住默认设备只是"少一个便利"，
绝不能因此让整个桥起不来。所以对外三个函数全部自己吞异常并返回安全值。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import byref, c_uint32, c_uint16, c_ubyte, c_void_p, c_wchar_p

logger = logging.getLogger(__name__)

_ole32 = ctypes.WinDLL("ole32")

# ── COM 基础设施 ───────────────────────────────────────────────────────────────
_E_RENDER, _E_CAPTURE = 0, 1
_ROLE_CONSOLE, _ROLE_MULTIMEDIA, _ROLE_COMMUNICATIONS = 0, 1, 2
_DEVICE_STATE_ACTIVE = 0x00000001
_CLSCTX_ALL = 0x17
_VT_LPWSTR = 31

# {BCDE0395-E52F-467C-8E3D-C4579291692E}
_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
# {A95664D2-9614-4F35-A746-DE8DB63617E6}
_IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
# {870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}  ← 未公开
_CLSID_PolicyConfigClient = "{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"
# {F8679F50-850A-41CF-9C72-430F290290C8}  ← 未公开
_IID_IPolicyConfig = "{F8679F50-850A-41CF-9C72-430F290290C8}"

# PKEY_Device_FriendlyName = {A45C254E-DF1C-4EFD-8020-67D146A850E0}, pid 14
_PKEY_FRIENDLY_FMTID = "{A45C254E-DF1C-4EFD-8020-67D146A850E0}"
_PKEY_FRIENDLY_PID = 14


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", c_uint32), ("Data2", c_uint16), ("Data3", c_uint16),
                ("Data4", c_ubyte * 8)]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", c_uint32)]


class _PROPVARIANT(ctypes.Structure):
    # x64 上是 16 字节：vt+保留 8 字节，然后是 8 字节的联合体。
    _fields_ = [("vt", c_uint16), ("r1", c_uint16), ("r2", c_uint16),
                ("r3", c_uint16), ("data", c_void_p)]


def _guid(s: str) -> _GUID:
    g = _GUID()
    if _ole32.CLSIDFromString(c_wchar_p(s), byref(g)) < 0:
        raise OSError(f"CLSIDFromString 失败：{s}")
    return g


def _vtbl_call(ptr: c_void_p, index: int, restype, argtypes, *args):
    """按 vtable 下标调 COM 方法（纯 ctypes 手写，避开 comtypes）。"""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vtbl[index])(ptr, *args)


_ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(_GUID), c_void_p, c_uint32, ctypes.POINTER(_GUID),
    ctypes.POINTER(c_void_p)]
_ole32.CoCreateInstance.restype = ctypes.c_long
_ole32.CoInitializeEx.argtypes = [c_void_p, c_uint32]
_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoTaskMemFree.argtypes = [c_void_p]
_ole32.PropVariantClear.argtypes = [ctypes.POINTER(_PROPVARIANT)]

# ⚠⚠ COM 是**按线程**初始化的（`CoInitializeEx` 只作用于**调用它的那个线程**）。
#     所以这个标记**必须**是 thread-local —— 写成模块级全局是错的：
#     一旦在 A 线程初始化过，B 线程再来就会跳过 CoInitialize，而 B 线程上
#     的 COM 指针是未初始化的，CoCreateInstance 会以 `CO_E_NOTINITIALIZED`
#     （0x800401F0）失败。
#     本模块就是**跨线程**用的：`ensure_default_capture()` 通过
#     `run_in_executor` 丢给线程池执行，而调用点可能在主线程 —— 早晚撞上。
_co_local = threading.local()


def _ensure_com() -> None:
    """在**当前线程**上初始化 COM（幂等；已是别的模式也不报错）。"""
    if getattr(_co_local, "ready", False):
        return
    hr = _ole32.CoInitializeEx(None, 0)          # COINIT_MULTITHREADED
    # S_OK / S_FALSE / RPC_E_CHANGED_MODE 都可以继续用
    if hr not in (0, 1) and (hr & 0xFFFFFFFF) != 0x80010106:
        raise OSError(f"CoInitializeEx 失败：0x{hr & 0xFFFFFFFF:08X}")
    _co_local.ready = True


def _create(clsid: str, iid: str) -> c_void_p:
    p = c_void_p()
    hr = _ole32.CoCreateInstance(byref(_guid(clsid)), None, _CLSCTX_ALL,
                                 byref(_guid(iid)), byref(p))
    if hr < 0 or not p.value:
        raise OSError(f"CoCreateInstance 失败：0x{hr & 0xFFFFFFFF:08X} ({clsid})")
    return p


def _release(p: c_void_p) -> None:
    if p and p.value:
        try:
            _vtbl_call(p, 2, ctypes.c_ulong, [])      # IUnknown::Release
        except Exception:                             # noqa: BLE001
            pass


def _device_id(dev: c_void_p) -> str:
    """IMMDevice::GetId → 端点 ID（调用方负责 CoTaskMemFree）。"""
    out = c_void_p()
    if _vtbl_call(dev, 5, ctypes.c_long, [ctypes.POINTER(c_void_p)],
                  byref(out)) < 0 or not out.value:
        return ""
    try:
        return ctypes.wstring_at(out.value)
    finally:
        _ole32.CoTaskMemFree(out)


def _device_name(dev: c_void_p) -> str:
    """IMMDevice::OpenPropertyStore → PKEY_Device_FriendlyName。"""
    store = c_void_p()
    if _vtbl_call(dev, 4, ctypes.c_long, [c_uint32, ctypes.POINTER(c_void_p)],
                  c_uint32(0), byref(store)) < 0 or not store.value:
        return ""
    try:
        key = _PROPERTYKEY(fmtid=_guid(_PKEY_FRIENDLY_FMTID),
                           pid=_PKEY_FRIENDLY_PID)
        pv = _PROPVARIANT()
        if _vtbl_call(store, 5, ctypes.c_long,
                      [ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT)],
                      byref(key), byref(pv)) < 0:
            return ""
        try:
            if pv.vt == _VT_LPWSTR and pv.data:
                return ctypes.wstring_at(pv.data)
            return ""
        finally:
            _ole32.PropVariantClear(byref(pv))
    finally:
        _release(store)


# ── 对外接口 ───────────────────────────────────────────────────────────────────
def list_capture_endpoints() -> list[tuple[str, str]]:
    """当前**已启用**的录音端点 [(端点ID, 友好名)]。失败返回 []。"""
    try:
        _ensure_com()
        enum = _create(_CLSID_MMDeviceEnumerator, _IID_IMMDeviceEnumerator)
    except Exception as e:                            # noqa: BLE001
        logger.debug("枚举录音端点失败（初始化）：%s", e)
        return []
    coll = c_void_p()
    try:
        if _vtbl_call(enum, 3, ctypes.c_long,
                      [ctypes.c_int, c_uint32, ctypes.POINTER(c_void_p)],
                      ctypes.c_int(_E_CAPTURE), c_uint32(_DEVICE_STATE_ACTIVE),
                      byref(coll)) < 0 or not coll.value:
            return []
        count = c_uint32()
        if _vtbl_call(coll, 3, ctypes.c_long, [ctypes.POINTER(c_uint32)],
                      byref(count)) < 0:
            return []
        out: list[tuple[str, str]] = []
        for i in range(count.value):
            dev = c_void_p()
            if _vtbl_call(coll, 4, ctypes.c_long,
                          [c_uint32, ctypes.POINTER(c_void_p)],
                          c_uint32(i), byref(dev)) < 0 or not dev.value:
                continue
            try:
                did = _device_id(dev)
                if did:
                    out.append((did, _device_name(dev)))
            finally:
                _release(dev)
        return out
    except Exception as e:                            # noqa: BLE001
        logger.debug("枚举录音端点失败：%s", e)
        return []
    finally:
        _release(coll)
        _release(enum)


def get_default_capture() -> tuple[str, str]:
    """默认录音端点 `(端点ID, 友好名)`；取不到返回 `("", "")`。"""
    try:
        _ensure_com()
        enum = _create(_CLSID_MMDeviceEnumerator, _IID_IMMDeviceEnumerator)
    except Exception as e:                            # noqa: BLE001
        logger.debug("取默认录音端点失败（初始化）：%s", e)
        return "", ""
    dev = c_void_p()
    try:
        if _vtbl_call(enum, 4, ctypes.c_long,
                      [ctypes.c_int, ctypes.c_int, ctypes.POINTER(c_void_p)],
                      ctypes.c_int(_E_CAPTURE), ctypes.c_int(_ROLE_CONSOLE),
                      byref(dev)) < 0 or not dev.value:
            return "", ""
        return _device_id(dev), _device_name(dev)
    except Exception as e:                            # noqa: BLE001
        logger.debug("取默认录音端点失败：%s", e)
        return "", ""
    finally:
        _release(dev)
        _release(enum)


def set_default_capture(device_id: str) -> bool:
    """把 `device_id` 设为默认录音设备（三种角色一起设）。成功返回 True。"""
    if not device_id:
        return False
    try:
        _ensure_com()
        pol = _create(_CLSID_PolicyConfigClient, _IID_IPolicyConfig)
    except Exception as e:                            # noqa: BLE001
        logger.debug("设置默认录音端点失败（初始化）：%s", e)
        return False
    try:
        ok = True
        for role in (_ROLE_CONSOLE, _ROLE_MULTIMEDIA, _ROLE_COMMUNICATIONS):
            hr = _vtbl_call(pol, 13, ctypes.c_long,
                            [c_wchar_p, ctypes.c_int],
                            c_wchar_p(device_id), ctypes.c_int(role))
            ok = ok and hr >= 0
        return ok
    except Exception as e:                            # noqa: BLE001
        logger.debug("设置默认录音端点失败：%s", e)
        return False
    finally:
        _release(pol)


def find_capture_by_name(substr: str) -> tuple[str, str]:
    """按名字（不区分大小写，子串）找录音端点；找不到返回 `("", "")`。

    ⚠ 本模块的铁律是「对外函数永不抛」—— 这里也不例外（`substr` 万一不是
      字符串、`list_capture_endpoints` 万一将来改成会抛，都不该炸到调用方）。
    """
    try:
        want = substr.strip().lower()
        if not want:
            return "", ""
        for did, name in list_capture_endpoints():
            if want in name.lower():
                return did, name
        return "", ""
    except Exception as e:                            # noqa: BLE001
        logger.debug("按名字找录音端点失败：%s", e)
        return "", ""


def ensure_default_capture(prefer: str = "CABLE Output") -> tuple[bool, str]:
    """默认录音设备不是 `prefer` 那一只就改过去。

    返回 `(是否改动, 说明)`。说明直接可以进日志 —— 用户看日志要能一眼看懂
    "为什么我的麦克风被换了"。
    """
    try:
        if not prefer.strip():
            return False, "未配置要钉住的录音设备名，跳过"
        cur_id, cur_name = get_default_capture()
        if not cur_id:
            return False, "取不到当前默认录音设备（本次跳过）"
        if prefer.strip().lower() in cur_name.lower():
            return False, ""                      # 已经是它了，安静
        target_id, target_name = find_capture_by_name(prefer)
        if not target_id:
            return False, f"系统里没有找到「{prefer}」这只录音设备（本次跳过）"
        if not set_default_capture(target_id):
            return False, f"改默认录音设备失败：{cur_name} → {target_name}"
        return True, (f"默认录音设备原来是「{cur_name}」，已改回「{target_name}」"
                      f"—— 遥控器的声音要送到这一只，输入法才听得到")
    except Exception as e:                            # noqa: BLE001
        return False, f"钉默认录音设备时出错（已忽略）：{e}"
