# 从真机日志量出「我们发 MIC_OPEN → 遥控器回声 audio_start」的真实延迟分布
import io, re

P = r"C:\Users\leeway\AppData\Roaming\remote-voice-bridge\bridge.log"
lines = io.open(P, encoding="utf-8", errors="replace").read().splitlines()

TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")


def ts(l):
    m = TS.match(l)
    if not m:
        return None
    from datetime import datetime
    d = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return d.timestamp() + int(m.group(2)) / 1000.0


OPEN = ("📤 MIC_OPEN 补发", "🎤 松手后自动重新开麦")
START = "▶ Audio START"

opened = []   # (t, line)
starts = []
for l in lines:
    t = ts(l)
    if t is None:
        continue
    if any(k in l for k in OPEN):
        opened.append((t, l))
    elif START in l:
        starts.append(t)

print("MIC_OPEN 次数:", len(opened), " Audio START 次数:", len(starts))

# 对每个 MIC_OPEN，找之后 3 秒内最近的 Audio START = 回声
gaps = []
for t, l in opened:
    nxt = [s for s in starts if 0 <= s - t <= 3.0]
    if nxt:
        gaps.append(min(nxt) - t)
    else:
        gaps.append(None)

found = [g for g in gaps if g is not None]
found.sort()
print(f"\n能配上回声的 {len(found)}/{len(gaps)}   没回声的 {len(gaps)-len(found)}")
if found:
    import statistics
    print(f"  最小 {found[0]*1000:.0f}ms   中位 {statistics.median(found)*1000:.0f}ms"
          f"   最大 {found[-1]*1000:.0f}ms")
    print(f"  >0.8s 的占 {sum(1 for g in found if g > 0.8)} 条"
          f"  ← 这些就是会被 0.8s 窗口漏掉的")
    print(f"  >1.5s 的占 {sum(1 for g in found if g > 1.5)} 条")
    print(f"  >2.0s 的占 {sum(1 for g in found if g > 2.0)} 条")
    print("\n  分布（每 200ms 一档）：")
    from collections import Counter
    c = Counter(int(g / 0.2) for g in found)
    for k in sorted(c):
        print(f"    {k*0.2:.1f}~{k*0.2+0.2:.1f}s  {'#'*c[k]} {c[k]}")

# 反向：两次 Audio START 的间隔（用来估"真按键"离上一个 MIC_OPEN 多远）
print("\n相邻 Audio START 间隔（前 40 个 <5s 的）：")
prev = None
n = 0
for s in starts:
    if prev is not None and 0 < s - prev < 5:
        print(f"    {s - prev:5.2f}s")
        n += 1
        if n >= 40:
            break
    prev = s
