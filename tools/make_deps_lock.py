"""生成 / 校验 `requirements.lock.txt` —— 带 SHA-256 的精确依赖锁（2026-09-29 P2-7）。

## 为什么需要它（"依赖未完全锁定"具体指什么）

`requirements.txt` 里**直接依赖**都写了 `==`，但**传递依赖没有**：

    pyinstaller → altgraph / pefile / pywin32-ctypes / packaging / setuptools /
                  pyinstaller-hooks-contrib
    frida       → cffi / pycparser / six
    sounddevice → cffi
    pystray     → six

这些包今天解析到 A 版、下个月可能解析到 B 版 —— **同一份代码、同一个 tag，
构建出来的产物不一样**。而本项目最怕的正是"产物悄悄变了"：
按键旁路全靠 frida 的原生扩展，它一变就是"语音正常、按键全不灵"的静默故障。

锁文件把**版本 + 每个 wheel 的 SHA-256** 固定下来，CI 用

    pip install --require-hashes -r requirements.lock.txt

装 —— 哈希对不上就**装不上**，构建当场失败，而不是产出一个没人验过的包。
（这一步同时是"锁文件不是摆设"的证明：它被真的用来安装。）

## ⚠ 这个锁是**按目标平台**生成的

win_amd64 / CPython 3.11 —— 与本项目"唯一支持 Windows x64 + Python 3.11"
的声明一致（见 `requirements.txt` 头注释）。**换平台必须重新生成**，
否则 `--require-hashes` 会找不到匹配的 wheel 而直接失败（这是好事：
失败得明明白白，而不是装上一个没人验过的包）。

## 用法

    python tools/make_deps_lock.py            # 生成（需要联网）
    python tools/make_deps_lock.py --check    # 只校验现有锁与 requirements 一致（离线）
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _utf8 import setup as _setup_utf8  # noqa: E402
_setup_utf8()

REPO = Path(__file__).resolve().parent.parent
REQ = REPO / "requirements.txt"
REQ_BUILD = REPO / "requirements-build.txt"
LOCK = REPO / "requirements.lock.txt"

# 目标平台 —— 与 requirements.txt 头注释里的"支持范围"必须一致。
TARGET = ["--only-binary=:all:", "--platform", "win_amd64",
          "--python-version", "3.11", "--implementation", "cp", "--abi", "cp311"]

_HEADER = """\
# 自动生成 —— **不要手改**。改依赖请改 requirements.txt / requirements-build.txt，
# 然后重跑 `python tools/make_deps_lock.py`。
#
# 目标平台：win_amd64 / CPython 3.11（与本项目声明的支持范围一致）
# 用途：`pip install --require-hashes -r requirements.lock.txt`
#   —— CI 就是这么装的。哈希对不上就装不上、构建当场失败，
#      而不是产出一个没人验过的包（2026-09-29 审查报告 P2-7）。
#
# ⚠ 为什么连传递依赖也锁：pyinstaller / frida / sounddevice 会拉进
#   altgraph、pefile、cffi、six、setuptools…… 这些包不锁的话，
#   同一份代码在不同时间构建出的产物**不一样**；而按键旁路依赖的
#   frida 原生扩展一变，表现就是"语音正常、按键全不灵"的静默故障。
"""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm(name: str) -> str:
    """PEP 503 规范化：小写 + 把 `-` / `_` / `.` 一律折成 `-`。

    ⚠ 三样都要折：`winrt-Windows.Devices.Bluetooth` 与 wheel 里解出来的
      `winrt-windows-devices-bluetooth` 必须落成同一个键 —— 只折 `_` 的话，
      requirements 里那 8 个 winrt 包会被判成"不在锁里"（第一次生成时就踩了）。
    """
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _parse_req_names(text: str) -> dict[str, str]:
    """从 requirements 文本里取 {规范名: 版本}（忽略注释与空行）。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, ver = line.split("==", 1)
        out[_norm(name)] = ver.strip()
    return out


def _parse_lock(text: str) -> dict[str, tuple[str, list[str]]]:
    """从锁文件里取 {规范名: (版本, [哈希…])}。"""
    out: dict[str, tuple[str, list[str]]] = {}
    cur: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", line)
        if m:
            cur = _norm(m.group(1))
            out[cur] = (m.group(2), [])
            rest = line[m.end():]
        else:
            rest = line
        if cur:
            for h in re.findall(r"--hash=sha256:([0-9a-f]{64})", rest):
                out[cur][1].append(h)
    return out


def cmd_generate() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rvb-lock-"))
    try:
        print(f"→ 按目标平台下载 wheel：{' '.join(TARGET)}")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "download", *TARGET,
             "-r", str(REQ), "-r", str(REQ_BUILD), "-d", str(tmp)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print(r.stdout[-3000:])
            print(r.stderr[-3000:], file=sys.stderr)
            print("❌ pip download 失败（锁没生成，原锁保持不动）")
            return 1

        from packaging.utils import parse_wheel_filename
        entries: list[tuple[str, str, str]] = []
        for whl in sorted(tmp.glob("*.whl")):
            name, ver, _build, _tags = parse_wheel_filename(whl.name)
            entries.append((_norm(str(name)), str(ver), _sha256(whl)))
        if not entries:
            print("❌ 一个 wheel 都没下到 —— 拒绝写出空锁")
            return 1
        entries.sort()

        body = [_HEADER]
        for name, ver, digest in entries:
            body.append(f"{name}=={ver} \\\n    --hash=sha256:{digest}\n")
        LOCK.write_text("\n".join(body), encoding="utf-8")
        print(f"✅ 已写出 {LOCK.name}：{len(entries)} 个包（含传递依赖），全部带 SHA-256")

        # 写完立刻自校验一次：直接依赖的版本必须与 requirements.txt 一致。
        return cmd_check()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_check() -> int:
    """离线校验：锁与 requirements 必须一致、且每行都带哈希。"""
    if not LOCK.exists():
        print(f"❌ 缺少 {LOCK.name} —— 跑 `python tools/make_deps_lock.py` 生成")
        return 1
    lock = _parse_lock(LOCK.read_text(encoding="utf-8"))
    if not lock:
        print(f"❌ {LOCK.name} 里一个包都没解析出来")
        return 1

    bad: list[str] = []
    # ① 每个包都要有哈希 —— `--require-hashes` 会因为缺哈希而拒绝安装，
    #    但那是 CI 上才炸；这里提前炸，省一轮往返。
    for name, (ver, hashes) in lock.items():
        if not hashes:
            bad.append(f"{name}=={ver} 没有 --hash")
    # ② 直接依赖的版本必须与 requirements*.txt 完全一致（锁漂了 = 装的不是你要的）
    direct = {}
    for f in (REQ, REQ_BUILD):
        direct.update(_parse_req_names(f.read_text(encoding="utf-8")))
    for name, ver in direct.items():
        if name not in lock:
            bad.append(f"直接依赖 {name}=={ver} 不在锁里")
        elif lock[name][0] != ver:
            bad.append(f"{name} 版本不一致：requirements={ver} 锁={lock[name][0]}")
    # ③ requirements 里不许再出现范围写法（`>=` / `<` / `~=`）——
    #    范围 = "同一份代码可能装出不同产物"，正是这一条要消灭的。
    for f in (REQ, REQ_BUILD):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            s = line.split("#", 1)[0].strip()
            if s and not re.match(r"^[A-Za-z0-9_.\-]+==[^\s;]+$", s):
                bad.append(f"{f.name}:{i} 不是精确钉版：{s}")

    if bad:
        for b in bad:
            print(f"  ❌ {b}")
        print(f"DEPS LOCK FAILED（{len(bad)} 项）")
        return 1
    print(f"  OK   锁里有 {len(lock)} 个包，全部带 SHA-256；"
          f"直接依赖 {len(direct)} 个版本一致；requirements 全是精确钉版")
    print("DEPS LOCK OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="生成/校验 requirements.lock.txt")
    ap.add_argument("--check", action="store_true", help="只校验，不生成（离线）")
    a = ap.parse_args()
    return cmd_check() if a.check else cmd_generate()


if __name__ == "__main__":
    raise SystemExit(main())
