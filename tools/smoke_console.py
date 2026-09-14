"""
控制台冒烟测试 —— 把本地控制台服务真的起起来，把所有接口都跑一遍。

为什么需要它：控制台是 Web UI + 一堆 JSON 接口，正常只有人点开才执行。
一个拼错的字段名、漏掉的键、或者 HTML/CSS/JS 没打进包里，
在打包成 exe 之后才会炸 —— 那时用户已经装上了。
这里用真实 HTTP 请求把「静态资源 + 状态组装 + 各类写入接口」全跑一遍。

⚠ 沙箱：会把 APPDATA 指向临时目录，因此**不会碰到用户真实的 config.json**。
   这一点很重要 —— 冒烟测试本身绝不能改用户的配置。

用法： python tools/smoke_console.py
输出： SMOKE OK   /   SKIPPED（环境不支持）/   FAIL（附具体哪几项）
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request

# ── 沙箱：必须在 import config 之前改 APPDATA ─────────────────────────────────
_SANDBOX = tempfile.mkdtemp(prefix="rvb-smoke-")
os.environ["APPDATA"] = _SANDBOX
os.environ["RVB_NO_AUTO_CONSOLE"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pystray  # noqa: F401
except ImportError:  # pragma: no cover
    # 冒烟测试只跑控制台，不碰托盘。托盘库在部分环境装不上，
    # 不该因此让控制台的测试跑不起来 —— 缺了就塞个空壳。
    import types
    sys.modules["pystray"] = types.ModuleType("pystray")
    print("[smoke] pystray 不可用，已用空壳替代（只影响托盘）")

import console_server  # noqa: E402

FAILS: list[str] = []
PORT = 0


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
    return bool(cond)


def req(path, method="GET", body=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        return (json.loads(raw.decode("utf-8")) if "json" in ctype
                else raw.decode("utf-8")), resp.status


def main() -> int:
    global PORT
    try:
        srv = console_server.ensure_started(0)
    except OSError as e:
        print(f"SKIPPED (端口不可用：{e})")
        return 0
    PORT = srv.port
    print(f"[smoke] 控制台端口 {PORT}　沙箱配置目录 {_SANDBOX}")

    try:
        # ── 1. 静态资源 ──
        html, code = req("/")
        check(code == 200, "GET / 未返回 200")
        check("语音触发键" in html and "混合输出" in html,
              "index.html 里没有关键区块（语音触发键 / 混合输出）")
        check("style.css" in html and "app.js" in html, "index.html 没有引用样式/脚本")

        css, _ = req("/style.css")
        check("--card" in css, "style.css 内容异常")
        js, _ = req("/app.js")
        check("/api/state" in js, "app.js 没有拉取状态")

        # 路径穿越必须被挡住（本地服务也不能随便读文件）
        try:
            req("/../config.py")
            FAILS.append("目录穿越 /../config.py 没有被拒绝")
        except urllib.error.HTTPError as e:
            check(e.code in (403, 404), f"目录穿越返回了意外状态码 {e.code}")

        # ── 2. 状态接口 ──
        st, code = req("/api/state")
        check(code == 200, "GET /api/state 未返回 200")
        for key in ("version", "status", "levels", "mix", "config", "devices",
                    "diagnostics", "hotkey", "hotkey_presets", "buttons",
                    "targets", "supported_devices", "checklist", "autostart",
                    "waves"):
            check(key in st, f"/api/state 缺少字段 {key}")

        # 轻量实时接口：只回状态 + 电平/波形，前端用它高频轮询画波形。
        # 它必须**不含**设备列表那类重字段，否则 130ms 一轮会拖垮页面。
        live, lcode = req("/api/live")
        check(lcode == 200, "GET /api/live 未返回 200")
        for key in ("status", "levels", "waves", "mix", "diagnostics"):
            check(key in live, f"/api/live 缺少字段 {key}")
        for heavy in ("devices", "config", "buttons", "targets", "checklist"):
            check(heavy not in live, f"/api/live 不该包含重字段 {heavy}（会拖慢波形轮询）")

        # 三路波形：空的时候也得是数组（前端靠这个判断"还没开始采"），
        # 但光判空没意义 —— 真的推一段进去，看它是不是原样从接口出来。
        import state as _st
        for ch in ("sys", "remote", "mix"):
            w = (st.get("waves") or {}).get(ch)
            check(isinstance(w, list), f"waves.{ch} 不是数组")

        pushed = [0, 8000, -16000, 32767, -32768, 120, 0]
        _st.push_sys_audio(pushed)
        _st.push_audio(pushed, 100, 1, 32767, 16000, 40)
        _st.push_mix_audio(pushed)
        live2, _ = req("/api/live")
        for ch in ("sys", "remote", "mix"):
            w = (live2.get("waves") or {}).get(ch) or []
            check(len(w) == len(pushed),
                  f"waves.{ch} 点数应是 {len(pushed)}，实际 {len(w)}")
            check(list(w) == pushed, f"waves.{ch} 的数据没有原样传出来：{w}")
            check(all(isinstance(v, int) for v in w),
                  f"waves.{ch} 里不是整数采样值")
        # 环形缓冲上限：推超过 256 点后不该无限增长
        _st.push_sys_audio([1] * 400)
        w = (req("/api/live")[0].get("waves") or {}).get("sys") or []
        check(len(w) == 128, f"波形只该回最近的 128 点，实际 {len(w)}")
        _st.reset()          # 清干净，别影响后面的断言

        check(len(st.get("buttons", [])) >= 14, "按键列表少于 14 个")
        check(len(st.get("checklist", [])) >= 5, "状态清单条目过少")
        for ch in ("sys", "remote", "mix"):
            check(ch in st.get("levels", {}), f"缺少 {ch} 路电平")

        # 输出设备下拉：配置里存的是 "CABLE Input" 这种前缀名，设备列表里是
        # "CABLE Input (VB-Audio Virtual Cable)" 这种全名。不做前缀解析的话
        # <select> 匹配不上任何 option，会渲染成空白 —— 看着像"没设置"。
        devs = st.get("devices", {})
        for k in ("input_list", "output_list", "output_resolved", "output"):
            check(k in devs, f"/api/state.devices 缺少字段 {k}")
        check(devs.get("output_resolved") in (devs.get("output_list") or [""]),
              f"output_resolved={devs.get('output_resolved')!r} 不在输出设备列表里，"
              f"设置页下拉框会显示空白")
        # 设备名去重：同一只声卡在 MME/DirectSound/WASAPI 下会重复出现，
        # 不去重的话列表里会有 4 个长得一样的 CABLE Input。
        ol = devs.get("output_list") or []
        check(len(ol) == len(set(ol)), "输出设备列表里有完全重名项")
        trunc_pairs = [(a, b) for a in ol for b in ol
                       if a != b and len(a) >= 12 and b.lower().startswith(a.lower())]
        check(not trunc_pairs,
              f"输出设备列表里有截断重复项（MME 只给 31 字符）：{trunc_pairs[:2]}")

        # ── 3. 写入接口 ──
        st2 = req("/api/state")[0]
        check(st2["hotkey"]["preset"] == "ctrl+win",
              f"默认触发键预设应是 ctrl+win，实际 {st2['hotkey']['preset']}")

        r, _ = req("/api/config", "POST", {"gain": 12.5, "hotkey_mode": "tap"})
        check(r.get("ok"), "POST /api/config 失败")
        st2, _ = req("/api/state")
        check(abs(st2["config"]["gain"] - 12.5) < 0.01, "增益没有写进配置")
        check(st2["config"]["hotkey_mode"] == "tap", "触发方式没有写进配置")

        r, _ = req("/api/mix", "POST", {"sys_muted": True})
        check(r.get("mix", {}).get("sys_muted") is True, "POST /api/mix 没有生效")
        req("/api/mix", "POST", {"sys_muted": False})

        r, _ = req("/api/mapping", "POST", {"button": "ok", "value": "ctrl+c"})
        check(r.get("ok"), "POST /api/mapping 失败")
        st3, _ = req("/api/state")
        ok_btn = next((b for b in st3["buttons"] if b["id"] == "ok"), None)
        check(ok_btn and ok_btn["value"] == "ctrl+c", "按键映射没有写进配置")

        r, _ = req("/api/mapping", "POST", {"button": "voice", "value": "ctrl+c"})
        check(r.get("error"), "语音键本应拒绝映射，却接受了")
        r, _ = req("/api/mapping", "POST", {"button": "不存在", "value": "up"})
        check(r.get("error"), "未知按键本应报错，却接受了")

        r, _ = req("/api/mapping/reset", "POST", {})
        check(r.get("ok"), "恢复默认映射失败")

        r, _ = req("/api/hotkey", "POST", {"keys": ["ctrl", "win", "shift"]})
        check(r.get("ok"), "POST /api/hotkey 失败")
        st4, _ = req("/api/state")
        check(st4["hotkey"]["preset"] == "ctrl+win+shift",
              f"触发键预设识别错误：{st4['hotkey']['preset']}")

        # 自定义组合键：内置预设里没有，必须能存下来并识别成 custom。
        # 这条是用户报过的「自定义也不能设置」的回归测试。
        r, _ = req("/api/hotkey", "POST", {"keys": ["ralt", "space"]})
        check(r.get("ok"), "POST /api/hotkey 自定义组合键失败")
        st4, _ = req("/api/state")
        check(st4["hotkey"]["preset"] == "ralt+space",
              f"ralt+space 预设识别错误：{st4['hotkey']['preset']}")

        r, _ = req("/api/hotkey", "POST", {"keys": ["alt", "shift", "m"]})
        check(r.get("ok"), "POST /api/hotkey 任意自定义组合键失败")
        st4, _ = req("/api/state")
        check(st4["hotkey"]["preset"] == "custom", "任意组合键应识别成 custom")
        check("M" in (st4["hotkey"]["label"] or ""),
              f"自定义组合键的标签不对：{st4['hotkey']['label']!r}")

        # 解析不出来的键名必须被拒 —— 写进配置只会静默失效
        try:
            req("/api/hotkey", "POST", {"keys": ["这个键不存在"]})
            FAILS.append("无法解析的键名本应被拒，却写进了配置")
        except urllib.error.HTTPError as e:
            check(e.code == 400, f"坏键名应返回 400，实际 {e.code}")

        req("/api/hotkey", "POST", {"keys": ["ctrl", "win"]})

        # 录制器归一化：keyboard 库把右 Alt 报成 `right menu`（不是 `right alt`），
        # 归一化之后必须等于 ralt —— 否则录制出来的组合键解析不了、
        # SendInput 静默跳过，用户看到的就是「自定义快捷键设了完全没用」。
        from keys import combo_bad_parts, is_modifier, normalize_combo_part
        for raw, want in [("right menu", "ralt"), ("left menu", "lalt"),
                          ("alt gr", "ralt"), ("right ctrl", "rctrl"),
                          ("left ctrl", "lctrl"), ("caps lock", "capslock"),
                          ("page up", "pageup"), ("print screen", "printscreen"),
                          ("volume up", "volumeup"), ("spacebar", "space")]:
            got = normalize_combo_part(raw)
            check(got == want, f"键名归一化错误：{raw!r} → {got!r}，应为 {want!r}")
        for raw in ("right menu", "alt gr", "left shift", "right windows"):
            check(is_modifier(raw), f"{raw!r} 应被识别为修饰键")
        check(combo_bad_parts("ctrl+win") == [], "ctrl+win 本应完全可解析")
        check(combo_bad_parts("right menu+space") == [],
              "右 Alt + 空格 本应完全可解析（曾是解析不出的 bug）")
        check(combo_bad_parts("不存在的键") == ["不存在的键"],
              "无法解析的键名没有被 combo_bad_parts 抓出来")

        r, _ = req("/api/open", "POST", {"what": "不知道什么"})
        check(r.get("error"), "白名单外的 open 请求本应被拒")

        r, _ = req("/api/record/poll")
        check("status" in r, "/api/record/poll 返回异常")

        r, _ = req("/api/log")
        check("text" in r and "size" in r, "/api/log 返回异常")

        # ── 4. 关键映射目标的键名必须真的能解析 ──
        from keys import _resolve_key
        from config import MAPPING_TARGETS
        for value in MAPPING_TARGETS:
            if value in ("", "native", "voice"):
                continue
            for part in value.split("+"):
                check(_resolve_key(part) is not None,
                      f"映射目标 {value!r} 里的键名 {part!r} 无法解析")

    finally:
        srv.stop()
        shutil.rmtree(_SANDBOX, ignore_errors=True)

    if FAILS:
        for f in FAILS:
            print(f"FAIL {f}")
        print(f"SMOKE FAILED（{len(FAILS)} 项）")
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
