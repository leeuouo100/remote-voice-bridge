"""读仓库根目录那些 `.bat` 的**唯一入口** —— 因为它们的编码不止一种。

为什么必须有这个文件
====================
仓库里的 `.bat` 分两种编码，而且**两种都是对的**（各自跟自己的 `chcp` 配套）：

  · `chcp 936`   ⇒ GBK 字节（三个 `测*.bat`、`修复蓝牙配对.bat`、`run.bat`…）
  · `chcp 65001` ⇒ UTF-8 字节

哪个是哪个由 `check_bat_env.py` 管，这里不重复判断 —— 这里只负责**把文本解出来**。

2026-09-29 真机验收时踩到的
==========================
把那几个 UTF-8 的 bat 转成 GBK 之后，`check_run_bat.py` / `check_ci_workflow.py` /
`check_takeover_guard.py` 里**各自写死**的 `decode("utf-8")` 当场解出乱码，
判据全部变成"找不到那个字符串"，报出来的却是

    「run.bat 没启动 tray_app.py」

这种**指向完全错误**的结论 —— 而真正的原因只是编码变了。
三个地方各写一遍 = 三个地方会忘。所以收到这里，一处改、三处都跟上。

（顺带：这也解释了为什么"判据不许拿整份文件判"那条经验还不够 ——
 判据读文件的方式本身也会漂。）
"""
from __future__ import annotations

from pathlib import Path


def read(path: str | Path) -> str:
    """把一份 `.bat` 解成文本。

    顺序是**先 UTF-8、后 GBK**：
      · 纯 ASCII 的文件两种都能解，先 UTF-8 得到的就是对的；
      · UTF-8 的中文（三字节）几乎不可能恰好是合法的 GBK 双字节序列，
        所以反过来"先 GBK"会把 UTF-8 文件解成乱码 —— 顺序不能反。

    两种都解不出来时用 `errors="replace"` 兜底：**乱码可以接受，抛异常不行**
    （判据挂掉比判据判错更难查）。
    """
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")
