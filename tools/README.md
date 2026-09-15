# tools/ —— 自检与实测脚本

分两类，别混着用：

- **`check_*` / `smoke_*`** —— **自动化自检**，不需要人在旁边，CI 每次都跑。
  出问题＝代码有 bug，必须修。
- **`test_*` / `apply_*` / `serve_*`** —— **手动工具**，要人看着屏幕操作，
  或者会改动你的配置。CI **不跑**这些。

## 一键

```bat
python tools\check_all.py        :: 把下面所有 check_* 跑一遍，输出 ALL CHECKS PASSED 即通过
```

改完代码、发版前跑它。

> **看到 `[.... ] xxx：疑似机器忙，重跑一次…` 是正常的，不用管。**
> 只有 `check_injection.py` 会真的往系统里注入按键、读系统实时状态，是唯一受
> 机器负载影响的一关。`check_all` 会先等 2 秒再跑它，失败且原因是
> 「钩子没收到」时整步重跑一次。**这一条不要"优化"掉** ——
> 换成固定 sleep 会有假红，去掉重试又会让真回归藏进噪声里。

## 自动化自检（CI 也跑）

| 脚本 | 检查什么 | 为什么要它 |
|---|---|---|
| `check_version.py` | `config.py` 的 `APP_VERSION` == `installer.iss` 的 `MyAppVersion` | 防止"tag 打的是 1.0.3、装出来还写着 1.0.2"这种只能靠重新发版来修的事故 |
| `check_keymap.py` | 每个映射目标里的键名都真的能解析成扫描码 | 选项能选、按下去没反应，而且全程不报错 —— 这类故障肉眼查不出来 |
| `check_injection.py` | **真的把组合键按下去**，再用键盘钩子确认系统收到了 | 唯一能拦住「SendInput 静默失效」和「组合键顺序错」的一关 |
| `smoke_console.py` | 离屏把控制台真的建出来跑几轮刷新 | 控件名写错、变量漏定义，在 CI 阶段就被拦下，而不是等用户点开才炸 |
| `check_ble_callback_thread.py` | 在没有 asyncio 事件循环的 `Dummy-XXXX` 线程里跑完整语音链路 | v1.0.3 的真机事故：BLE 回调线程里 `get_event_loop()` 直接抛异常，`MIC_OPEN` 一次都没发出去，日志还假报成功。纯标准库、不需要真机，所以放进 CI 当回归闸 |
| `check_ui.js` | 控制台截图 + 波形动画（需 Playwright，可选） | 前端改挂了不至于没人发现 |

> `check_keymap.py` 会跳过 `config.VIRTUAL_TARGETS` 里的虚拟目标
> （禁用 / 原样直通 / 语音键 / 按住说话）—— 它们不是组合键，解析必然失败。
> **新增虚拟目标时记得同步登记到 `VIRTUAL_TARGETS`**，否则 CI 会红。

## 手动工具（要人看着屏幕）

| 脚本 | 什么时候用 | 怎么用 |
|---|---|---|
| `test_voice_hotkey.py` | 拿不准输入法认不认这组语音键 | `python tools\test_voice_hotkey.py ctrl win`（5 秒内切到记事本，它按住 6 秒再松开）；加 `--tap` / `-t` 测"按一下开始、再按一下结束" |
| `test_recorder.py` | 改了按键录制逻辑之后 | `python tools\test_recorder.py` |
| `apply_voice_mode.py` | 想一键配好语音（或退回按住模式） | `python tools\apply_voice_mode.py`（推荐配置）/ `--show`（只看）/ `--hold`（退回按住说话） |
| `serve_console.py` | 单独起控制台做前端调试 | `python tools\serve_console.py` |
| `diag_remote.py` | **遥控器/按键出任何问题，先跑它**。让程序把现象测出来，而不是靠人描述 | 双击仓库根目录的 **`diag-remote.bat`**（或 `python tools\diag_remote.py`）。会分 7 段引导你按遥控器和物理键盘，约 90 秒，产出 `%APPDATA%\remote-voice-bridge\remote-diag.txt` |

> `diag_remote.py` 的**真机部分**必须有人按键，没法自动化；但它的「报告生成器」
> 是纯函数式的，`check_all` 会用假数据把两条分支（能区分设备 / 不能区分）都跑一遍 ——
> 否则那段代码第一次运行就是在用户机器上。跑法：`python tools\diag_remote.py --selftest`。

## 公共小工具

`_utf8.py` —— 钉住控制台编码。Windows 控制台默认 cp936/GBK，
脚本里只要有中文输出，在 CI（或某些机器）上就会 `UnicodeEncodeError` 直接崩。
**每个会打印中文的脚本都要在最开头调用它**，别裸 `print("中文")`。
