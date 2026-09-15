"""「界面上关了、后端还在用」的回归闸 —— v1.0.8 的真机反馈。

武哥原话：「我明明不让系统麦克风参与说话了，结果它还是在输出，很奇怪呀。」

查出来的根因有两处，都是**界面上的开关管不到后端**：

  ① 没落盘：音频页的「参与混合 / 增益」只改内存里的 state._mix，
     config.json 一个字没动。于是只要在设置页改了任何一项、或者重连/重启，
     _patch_config / run_bridge 就会拿 config.json 把 set_mix **重灌一遍** ——
     用户的勾选被悄悄回滚。
  ② 被白名单丢掉：/api/config 的 allowed 里没有 system_mic_enabled /
     remote_mic_enabled，前端发过来就**静默忽略**，后端仍按旧配置混音。

这两条都属于"用户做了什么、系统没当回事"，而且**一点声音都没有** ——
最容易被当成玄学。所以在这里钉死：

  A. /api/mix 改 sys_enabled → config.json 真的写进去了
  B. **模拟"设置页动了任何一项" → 刚关掉的那一路还是关着的**（核心断言，
     原来这一步就会把它翻回 True）
  C. /api/config 不再吞掉 system_mic_enabled / remote_mic_enabled
  D. 被忽略的字段必须报出来（不再静默）
  E. 静音/独奏这类临时操作**不落盘**（别把临时状态写进配置）

⚠ 沙箱：跟 smoke_console 一样把 APPDATA 指向临时目录，
   **绝不碰用户真实的 config.json**。

用法： python tools/check_mix_persist.py
输出： MIX PERSIST OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import urllib.request

from _utf8 import setup as _setup_utf8

_setup_utf8()

# ── 沙箱：必须在 import config 之前改 APPDATA ─────────────────────────────────
_SANDBOX = tempfile.mkdtemp(prefix="rvb-mixpersist-")
os.environ["APPDATA"] = _SANDBOX
os.environ["RVB_NO_AUTO_CONSOLE"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pystray  # noqa: F401
except ImportError:  # pragma: no cover
    import types
    sys.modules["pystray"] = types.ModuleType("pystray")

import console_server  # noqa: E402
import config  # noqa: E402
import state  # noqa: E402

FAILS: list[str] = []
PORT = 0


def check(cond, msg) -> bool:
    if not cond:
        FAILS.append(msg)
    return bool(cond)


def req(path, method="GET", body=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8")), resp.status


def saved() -> dict:
    """直接读沙箱里的 config.json —— 不经过进程内缓存，才是真的"落盘了吗"。"""
    with open(config.CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    global PORT
    srv = console_server.ensure_started(0)
    PORT = srv.port

    # 先让配置文件存在（Config.save 会建目录）
    cfg = config.Config.load()
    cfg.save()

    # ── A. /api/mix 关掉系统麦克风 → 必须落盘 ─────────────────────────────
    body, _ = req("/api/mix", "POST", {"sys_enabled": False})
    check(body.get("saved") is True, "A1 /api/mix 改 sys_enabled 后 saved=True")
    check(saved().get("system_mic_enabled") is False,
          "A2 config.json 里 system_mic_enabled 真的变成 False")
    check(state.mix_params().get("sys_enabled") is False,
          "A3 内存里的 sys_enabled 也是 False")

    # ── B. 核心断言：设置页动一下，不能把刚关掉的又翻回来 ──────────────────
    req("/api/config", "POST", {"suppress_keys": True})
    check(state.mix_params().get("sys_enabled") is False,
          "B1 在设置页改了别的项之后，刚关掉的系统麦克风【仍然关着】"
          "（v1.0.7 这里会被 config.json 悄悄翻回 True）")
    check(saved().get("system_mic_enabled") is False,
          "B2 config.json 里的选择也没被覆盖")

    # 再模拟一次"重连"：run_bridge 启动时就是照 cfg 灌 set_mix 的
    state.set_mix(sys_enabled=config.Config.load().system_mic_enabled)
    check(state.mix_params().get("sys_enabled") is False,
          "B3 重启/重连那条路径（照 cfg 灌 set_mix）也保持关着")

    # ── C. /api/config 不再吞掉这两个开关 ────────────────────────────────
    body, _ = req("/api/config", "POST",
                  {"system_mic_enabled": True, "remote_mic_enabled": False})
    ign = body.get("ignored") or []
    check("system_mic_enabled" not in ign and "remote_mic_enabled" not in ign,
          f"C1 这两个开关没被白名单丢掉（ignored={ign}）")
    check(saved().get("system_mic_enabled") is True
          and saved().get("remote_mic_enabled") is False,
          "C2 写进 config.json 的值与请求一致")

    # ── D. 真的不认识的字段必须报出来，不许静默 ───────────────────────────
    body, _ = req("/api/config", "POST", {"sys_mic_enabled": True})  # 少个 tem
    check("sys_mic_enabled" in (body.get("ignored") or []),
          "D1 写错的字段名会被放进 ignored 报出来（不再静默丢）")

    # ── E. 临时操作不落盘 ────────────────────────────────────────────────
    before = saved()
    req("/api/mix", "POST", {"sys_muted": True, "remote_solo": True,
                             "remote_gain": 7.5})
    after = saved()
    check(after.get("sys_muted") is None and after.get("remote_solo") is None,
          "E1 静音/独奏是临时操作，不会被写进 config.json")
    check(before.get("system_mic_enabled") == after.get("system_mic_enabled"),
          "E2 临时操作不会顺带改到别的配置项")
    # 增益是"有配置对应项"的，必须落盘
    check(after.get("gain") == 7.5, "E3 遥控器增益落盘到 config.gain")

    # ── F. 反证：落盘这件事只能由接口带来，不是 set_mix 自带的 ─────────────
    # 没有这条，本闸有可能是"永远绿"：万一 set_mix 以后自己也写盘了，
    # A/B/C 三组会因为别的原因通过，而"接口有没有真的落盘"根本没人验。
    req("/api/mix", "POST", {"sys_enabled": False})   # 先把磁盘钉在 False
    state.set_mix(sys_enabled=True)                   # 绕开接口，只动内存
    check(saved().get("system_mic_enabled") is False,
          "F1 绕开接口只动内存时磁盘纹丝不动（证明落盘只由接口保证）")
    check(state.mix_params().get("sys_enabled") is True,
          "F2 反证的前提成立：内存确实被改成了 True")

    srv.stop()
    shutil.rmtree(_SANDBOX, ignore_errors=True)

    for m in FAILS:
        print(f"  FAIL {m}")
    if FAILS:
        print(f"MIX PERSIST FAILED（{len(FAILS)} 项）")
        print("  提示：界面给了开关就必须让它留得住。"
              "静默回滚是最难查的一类 bug —— 用户只会说『它自己不听话』。")
        return 1
    print("  OK   11 项全过（落盘 / 不回滚 / 不静默 / 临时不落盘 / 反证）")
    print("MIX PERSIST OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
