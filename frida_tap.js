/*
 * 遥控器 HID 报文旁路（Frida 脚本，注入 WUDFHost.exe 里跑）
 * ──────────────────────────────────────────────────────────────
 * 原理
 *   BLE HID（HOGP）在 Windows 上是 UMDF 驱动，跑在 WUDFHost.exe 进程里。
 *   驱动通过 ntdll!NtDeviceIoControlFile + IOCTL 0x80018483 把 GATT 特征
 *   （= HID 输入报告）读上来，报告就在这次调用的**输出缓冲区**里。
 *   在那个缓冲区上抄一份，就绕开了整个 HID 映射层 —— 这正是本项目
 *   在此之前拿不到按键的原因（详见 docs/遥控器按键-结论与下一步.md：
 *   报告不是没发，是在 WUDFHost 内部就被消费掉了，用户态看不见）。
 *
 * 只做三件事
 *   1. 把这次 IOCTL 的输出（原始报告）送给 Python 侧解码
 *   2. 把「已被映射的键」的 usage 原地写 0 —— 驱动以为没按键，原生动作
 *      不再发生，于是不会「原生 + 映射」双发（音量键就不会顺带调系统音量）
 *   3. 20 秒一次心跳，把诊断计数报给 Python（命中次数 / 长度分布 / 见过的 usage）
 *
 * 为什么按长度分格式
 *   同一个 WUDFHost 还服务别的蓝牙键鼠，它们吐 9 字节键盘报告，混在一起。
 *   遥控器只发两种：
 *     · 3 字节消费类页：[0x02][usage_lo][usage_hi]（16 位 usage）
 *     · N 字节厂商页：  [0x01][usage_lo]…（8 位 usage）
 *   解码交给 Python（frida_hid.decode_tap_report），这里只负责抄和抹。
 *
 * 来源
 *   机制移植自 VibeMote 的 tap.js（https://github.com/Tilkmilk/vibe-mote，
 *   MIT License），按本项目的报告格式与配置做了改写。详见 THIRD_PARTY_NOTICES.md。
 */
const READ_IOCTL = 0x80018483;

let lastRaw = null;          // 内容去重：空闲帧不刷屏
let sent = 0;
let total = 0;
let blocked = 0;
let blockCC = {};            // 消费类页：16 位 usage → true（要抹掉原生动作）
let blockVendor = {};        // 厂商页：8 位 usage → true

/* 诊断计数：真机上出现过「钩子挂上了、界面显示就绪、却一条报文都没有」
 * （挂错了宿主 / 另一款遥控器格式不同）。只看成功与否分不清是哪种，
 * 所以把原始计数报给 Python：
 *   ioctl = 命中「遥控器那个读 IOCTL」的次数（0 → 多半挂错了宿主）
 *   lens  = 该 IOCTL 的输出长度分布（出现意料之外的长度 → 格式跟预期不同）
 *   seen  = 见过的 usage（用来「认人」：这台遥控器实际发过哪些键） */
let ioctl = 0;
let lens = {};
let seen = {};
let weird = {};

function toHex(ptr, len) {
  const b = new Uint8Array(ptr.readByteArray(len));
  let s = "";
  for (let i = 0; i < b.length; i++) {
    s += b[i].toString(16).padStart(2, "0");
    if (i + 1 < b.length) s += " ";
  }
  return s;
}

/* 若这份报告是「已被映射的键」，把它的 usage 字节写 0，抹掉原生动作。
 *   · 消费类页：3 字节、首字节 0x02 → usage 在第 2、3 字节
 *   · 厂商页：  首字节 0x01     → usage 在第 2 字节 */
function nullify(ptr, len) {
  if (ptr.isNull() || len < 2) return;
  try {
    const b = new Uint8Array(ptr.readByteArray(len));
    if (len === 3 && b[0] === 0x02) {
      const usage = b[1] | (b[2] << 8);
      if (usage && blockCC[usage]) {
        ptr.writeByteArray([0x00, 0x00]);          // usage_lo / usage_hi = 0
        blocked++;
      }
    } else if (b[0] === 0x01) {
      const usage = b[1];
      if (usage && blockVendor[usage]) {
        ptr.add(1).writeByteArray([0x00]);         // usage = 0
        blocked++;
      }
    }
  } catch (e) {
    /* 写失败就算了：最坏情况是原生动作也会发生一次 */
  }
}

recv(function (msg) {
  if (msg && msg.type === "block") {
    const cc = {};
    (msg.cc || []).forEach(function (u) { cc[u >>> 0] = true; });
    const vd = {};
    (msg.vendor || []).forEach(function (u) { vd[u >>> 0] = true; });
    blockCC = cc;
    blockVendor = vd;
    send({ kind: "block_ack", count: Object.keys(cc).length + Object.keys(vd).length });
  }
});

const ntdll = Process.findModuleByName("ntdll.dll");
const target = ntdll ? ntdll.findExportByName("NtDeviceIoControlFile") : null;
if (target === null) {
  send({ kind: "error", message: "没找到 ntdll!NtDeviceIoControlFile" });
} else {
  send({ kind: "ready", pid: Process.id });
  Interceptor.attach(target, {
    onEnter(args) {
      // 0=FileHandle 1=Event 2=ApcRoutine 3=ApcContext 4=IoStatusBlock
      // 5=IoControlCode 6=InBuf 7=InLen 8=OutBuf 9=OutLen
      total++;
      if (args[5].toUInt32() === READ_IOCTL) {
        this.cap = true;
        this.out = args[8];
        this.outLen = args[9].toUInt32();
        ioctl++;
      }
    },
    onLeave(retval) {
      if (!this.cap) return;
      this.cap = false;
      const n = this.outLen;
      lens[n] = (lens[n] || 0) + 1;
      if (retval.toUInt32() !== 0 || this.out.isNull() || n < 2) return;

      let raw;
      try {
        raw = toHex(this.out, n);
      } catch (e) {
        return;
      }

      // 记「见过哪些 usage」，用来认人（换遥控器 / 兼容款时靠这一行）
      try {
        const b = new Uint8Array(this.out.readByteArray(n));
        if (n === 3 && b[0] === 0x02) {
          const u = b[1] | (b[2] << 8);
          if (u) seen["cc:" + u] = (seen["cc:" + u] || 0) + 1;
        } else if (b[0] === 0x01) {
          if (b[1]) seen["vp:" + b[1]] = (seen["vp:" + b[1]] || 0) + 1;
        } else if (Object.keys(weird).length < 6) {
          weird[raw] = 1;
        }
      } catch (e) { /* 读不到就算了 */ }

      if (raw !== lastRaw) {           // 内容有变化才上报：空闲帧不刷屏
        lastRaw = raw;
        sent++;
        send({ kind: "report", raw: raw, len: n });
      }
      // 快照已发出，现在才可以安全改写输出缓冲区
      nullify(this.out, n);
    }
  });
}

// 轻量心跳：只给 Python 做健康判断用（20 秒一次，可忽略不计）
setInterval(function () {
  send({ kind: "hb", total: total, sent: sent, blocked: blocked,
         ioctl: ioctl, lens: lens, weird: weird, seen: seen });
}, 20000);
