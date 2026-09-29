/* 控制台 XSS 的真实 DOM 验收 —— 2026-09-29 审查报告 P1-2。
 *
 * 为什么要真的开浏览器跑
 * ----------------------
 * 静态检查只能证明"源码里没写 innerHTML"。而 P1-2 的原始 bug 恰恰是
 * **一处忘了转义**：`renderStatusList` 把 `checklist[].value` 拼进 innerHTML，
 * 而那个 value 可以是 `/api/config` 写进去的设备名。这类洞的判定标准是
 * 行为 —— 只有真塞一段 `<img onerror=...>` 进数据、真渲染一遍、
 * 再看脚本有没有执行，才算验过。
 *
 * 做法
 * ----
 * 起一个**假的控制台**（只伺服 ui/ 目录 + 伪造 /api/state），把 payload 塞进
 * 所有"能由配置写入"的字段，然后：
 *   ① 断言 window.__XSS__ 没被置起来（脚本没执行）
 *   ② 断言 payload 是**当纯文本**显示出来的
 *   ③ 断言 #status-list 里没有真的生成 <img> 元素
 *   ④ **反例自证**：在同一页上故意用 innerHTML 塞一次同样的 payload，
 *      必须能把 window.__XSS__ 置起来 —— 否则说明这套检测本身是坏的
 *      （"没触发"可能只是 payload 根本没生效的假绿）。
 *
 * 依赖 playwright（本机在 %APPDATA%\npm\node_modules）。
 * 没有 playwright 时打印 SKIPPED 并以 0 退出 —— 由调用方决定算不算失败。
 *
 * 用法： node tools/check_ui_xss.js
 * 输出： UI XSS OK  /  FAIL  /  SKIPPED
 */
'use strict';

const fs = require('fs');
const http = require('http');
const path = require('path');

const UI = path.join(__dirname, '..', 'ui');
const PAYLOAD = '<img src=x onerror="window.__XSS__=1">';

let chromium;
try {
  ({ chromium } = require('playwright'));
} catch (e) {
  console.log(`SKIPPED 没有 playwright（${e.message.split('\n')[0]}）`);
  console.log('       装了就能跑：npm i -g playwright && npx playwright install chromium');
  process.exit(0);
}

const FAILS = [];
function check(cond, msg) {
  if (!cond) FAILS.push(msg);
  return !!cond;
}

const MIME = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
               '.js': 'application/javascript; charset=utf-8', '.svg': 'image/svg+xml' };

/* 本机的 playwright 版本和已下载的 chromium 版本不一定对得上
   （实测：playwright 1.59.1 找 chromium_headless_shell-1217，而机器上装的是 1234）。
   这种时候默认 launch 会直接报"Executable doesn't exist" —— 不是我们的问题，
   但会让这道闸在本来能跑的环境上变成假红。所以先试默认，失败再自己去
   %LOCALAPPDATA%\ms-playwright 里挑一个已安装的 chromium 顶上。 */
function findChromium() {
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  if (!fs.existsSync(root)) return null;
  const found = [];
  for (const d of fs.readdirSync(root)) {
    if (!/^chromium(-headless_shell)?-\d+$/.test(d)) continue;
    const exe = d.startsWith('chromium_headless_shell')
      ? path.join(root, d, 'chrome-headless-shell-win64', 'chrome-headless-shell.exe')
      : path.join(root, d, 'chrome-win64', 'chrome.exe');
    if (fs.existsSync(exe)) found.push({ d, exe });
  }
  found.sort((a, b) => b.d.localeCompare(a.d));
  return found.length ? found[0].exe : null;
}

async function launchBrowser() {
  try {
    return await chromium.launch();
  } catch (e) {
    const exe = findChromium();
    if (!exe) throw e;
    console.log(`  INFO 默认 chromium 不可用，改用本机已安装的：${exe}`);
    return await chromium.launch({ executablePath: exe });
  }
}

// 伪造的 /api/state：**凡是能由配置写入的字段都塞一遍 payload**。
// 少塞一个字段就可能漏掉一条渲染路径，所以这里宁可多塞。
function fakeState() {
  return {
    version: '0.0.0-xss-test',
    config_dir: 'C:\\fake',
    status: { connected: true, streaming: false, device: PAYLOAD },
    levels: { sys: -20, remote: -20, mix: -20 },
    waves: { sys: [], remote: [], mix: [] },
    mix: { sys_enabled: true, remote_enabled: true, sys_muted: false,
           remote_muted: false, sys_solo: false, remote_solo: false,
           sys_gain: 1, remote_gain: 1 },
    diagnostics: { frames: 0, peak: 0, sample_rate: 16000, frame_bytes: 0,
                   last_audio_ago: null, mix_limit_pct: 0 },
    config: { gain: 1, audio_output: PAYLOAD, system_mic_device: PAYLOAD,
              hotkey_mode: 'tap', input_method: 'wechat', device: 'chromecast',
              voice_mode: 'hold', mapping_enabled: true, suppress_keys: false,
              swallow_ok_during_voice: false, hid_vendor_keys: true,
              system_mic_enabled: true, remote_mic_enabled: true,
              send_after_voice: false, send_after_voice_delay_ms: 300,
              send_after_voice_key: 'enter', keyboard_page_keys: false },
    hotkey: { preset: 'ctrl+win', label: PAYLOAD, keys: ['ctrl', 'win'] },
    hotkey_presets: [],
    buttons: [{ id: 'ok', label: PAYLOAD, value: PAYLOAD, voice: false },
              { id: 'voice', label: PAYLOAD, value: 'voice', voice: true }],
    targets: { '': PAYLOAD },
    target_groups: [{ label: PAYLOAD, ids: [''] }],
    native_hint: PAYLOAD,
    supported_devices: [{ name: PAYLOAD, signature: PAYLOAD, active: true, connected: true }],
    // ⚠ 这就是 P1-2 的原始入口
    checklist: [{ name: PAYLOAD, value: PAYLOAD, ok: false, warn: true, mono: false }],
    autostart: false,
    devices: { input_list: [PAYLOAD], output_list: [PAYLOAD],
               output_resolved: PAYLOAD, output: PAYLOAD,
               system_mic: PAYLOAD, system_mic_resolved: PAYLOAD,
               system_mic_reason: 'ok' },
  };
}

function serve() {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      const p = req.url.split('?')[0];
      if (p === '/api/state') {
        res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
        return res.end(JSON.stringify(fakeState()));
      }
      if (p === '/api/live') {
        const s = fakeState();
        res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
        return res.end(JSON.stringify({ status: s.status, levels: s.levels,
                                        waves: s.waves, mix: s.mix,
                                        diagnostics: s.diagnostics }));
      }
      if (p === '/api/log') {
        res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
        return res.end(JSON.stringify({ text: PAYLOAD, size: 1 }));
      }
      if (p.startsWith('/api/')) {
        res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
        return res.end('{}');
      }
      const rel = p === '/' ? 'index.html' : p.replace(/^\/+/, '');
      const file = path.join(UI, rel);
      if (!file.startsWith(UI) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
        res.writeHead(404); return res.end('not found');
      }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
      res.end(fs.readFileSync(file));
    });
    srv.listen(0, '127.0.0.1', () => resolve(srv));
  });
}

(async () => {
  const srv = await serve();
  const port = srv.address().port;
  const browser = await launchBrowser();
  const page = await browser.newPage();
  const consoleErrors = [];
  page.on('pageerror', (e) => consoleErrors.push(String(e)));

  try {
    await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: 'load' });
    await page.waitForTimeout(1800);        // 等慢档轮询把 /api/state 渲上去

    const xss = await page.evaluate(() => window.__XSS__);
    check(xss === undefined,
          `脚本被执行了（window.__XSS__=${xss}）—— payload 从数据变成了可执行的 HTML`);

    const statusText = (await page.textContent('#status-list')) || '';
    check(statusText.includes(PAYLOAD),
          `payload 没有被当**纯文本**显示出来（#status-list 实际内容 ${JSON.stringify(statusText.slice(0, 120))}）`);
    check((await page.locator('#status-list img').count()) === 0,
          '#status-list 里真的生成了 <img> 元素 —— 说明值被当成 HTML 解析了');

    const devText = (await page.textContent('#device-list')) || '';
    check(devText.includes(PAYLOAD), '设备名没有被当纯文本显示（#device-list）');
    check((await page.locator('#device-list img').count()) === 0,
          '#device-list 里真的生成了 <img> 元素');

    check(consoleErrors.length === 0,
          `页面有 JS 报错：${consoleErrors.slice(0, 2).join(' | ')}`);

    // ── 反例自证 ──────────────────────────────────────────────────────────
    // 故意用 innerHTML 塞一次同样的 payload。它**必须**能把 window.__XSS__
    // 置起来 —— 否则上面那句"没执行"就是假绿（可能 payload 压根没生效）。
    await page.evaluate((p) => {
      const d = document.createElement('div');
      d.innerHTML = p;
      document.body.appendChild(d);
    }, PAYLOAD);
    await page.waitForTimeout(400);
    const after = await page.evaluate(() => window.__XSS__);
    check(after === 1,
          `反例没触发（window.__XSS__=${after}）—— 说明这套检测本身是坏的，`
          + `上面所有"没执行"的结论都不算数`);
  } finally {
    await browser.close();
    srv.close();
  }

  for (const m of FAILS) console.log(`  FAIL ${m}`);
  if (FAILS.length) {
    console.log(`UI XSS FAILED（${FAILS.length} 项）`);
    console.log('  提示：自由文本进 DOM 只能走 textContent。拼 innerHTML 的路只要');
    console.log('        有一处忘了转义，往配置里塞一段 <img onerror=...> 就能执行脚本。');
    process.exit(1);
  }
  console.log('  OK   真实浏览器渲染：payload 只当纯文本，脚本未执行（含反例自证）');
  console.log('UI XSS OK');
})().catch((e) => {
  console.error(`  FAIL 运行异常：${e && e.stack ? e.stack : e}`);
  console.log('UI XSS FAILED（1 项）');
  process.exit(1);
});
