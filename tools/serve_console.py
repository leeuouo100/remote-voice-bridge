"""临时脚本：只把 Web 控制台跑起来（不连蓝牙），用于人工/截图验收 UI。

加 `--wave` 会持续灌入三段"像人说话"的模拟波形，好把三条波形画出来的效果
看仔细（不然没接硬件时波形永远是三条平线，看不出渲染对不对）。
"""
import math
import os
import random
import sys
import threading
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import console_server  # noqa: E402
import state  # noqa: E402


def _fake_wave():
    """推三段形状不同的波形，模拟"三路都在工作"的样子。"""
    t = 0.0
    while True:
        pts_sys, pts_rmt, pts_mix = [], [], []
        for i in range(4):
            t += 1.0
            # 电脑麦克风：稳定的小底噪 + 缓慢起伏（像房间环境音）
            pts_sys.append(int(1800 * math.sin(t * 0.31) + random.randint(-260, 260)))
            # 遥控器：脉冲式（像人按着语音键说话，一段一段的）
            burst = 9000 * math.sin(t * 1.7) if int(t / 26) % 3 != 2 else 700
            pts_rmt.append(int(burst + random.randint(-900, 900)))
            # 混合：两路相加
            pts_mix.append(int(pts_sys[-1] * 1.4 + pts_rmt[-1] * 0.9))
        state.push_sys_audio(pts_sys)
        state.push_audio(pts_rmt, 60, int(t), 9000, 16000, 40)
        state.push_mix_audio(pts_mix)
        state.push_levels(sys_db=-38.0, remote_db=-22.0, mix_db=-16.0)
        time.sleep(0.02)


def main():
    if "--wave" in sys.argv:
        threading.Thread(target=_fake_wave, daemon=True).start()
    srv = console_server.ensure_started(0)
    print("URL", srv.url(), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
