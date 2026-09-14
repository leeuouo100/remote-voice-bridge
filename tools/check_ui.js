/* 控制台 UI 自动验收：截图 + 波形是否真的在动 + 有没有前端报错。
 *
 * 为什么需要"波形在动"这一项：波形是高频轮询驱动的，只截图看不出它是不动的。
 * 曾经的问题就是波形数据一直在后端采、但压根没接到前端 —— 静态看 HTML 很正常。
 *
 * 用法：
 *   node tools/check_ui.js <url> [输出前缀]
 *   node tools/check_ui.js http://127.0.0.1:9083/ ./_shot
 *
 * 依赖：playwright + 已安装的 chromium（playwright 自己知道装在哪，无需配置）。
 *   需要指定别的 chrome 时，用 CHROME_EXE 指向 chrome.exe。
 *
 * ⚠️ 截图内容来自**真实的控制台状态**，因此可能包含本机路径
 *   （「设置 → 关于」里的配置目录会显示成 C:\Users\<你的用户名>\AppData\...）。
 *   这类截图**不要直接提交到公开仓库**。
 */
const { chromium } = require('playwright');

// 不要在这里写死某台机器的绝对路径 —— 那既不可移植，也会把用户名带进仓库。
// 默认交给 playwright 自己解析浏览器位置；只有显式设了 CHROME_EXE 才覆盖。
const EXE = process.env.CHROME_EXE || null;

(async () => {
  const url = process.argv[2];
  const out = process.argv[3] || './_shot';
  if (!url) {
    console.log('用法: node tools/check_ui.js <url> [输出前缀]');
    process.exit(2);
  }

  const browser = await chromium.launch(EXE ? { executablePath: EXE } : {});
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2,
  });
  const errs = [];
  page.on('console', m => { if (m.type() === 'error') errs.push('CONSOLE ' + m.text()); });
  page.on('pageerror', e => errs.push('PAGEERROR ' + e.message));

  await page.goto(url, { waitUntil: 'load' });
  await page.waitForTimeout(1500);

  // ── 波形：抓三个时间点的路径，必须都在变 ──
  const grab = () => page.evaluate(() => {
    const o = {};
    for (const k of ['sys', 'remote', 'mix']) {
      for (const part of ['line', 'fill']) {
        const el = document.getElementById(`${k}-wave-${part}`);
        o[`${k}-${part}`] = (el && el.getAttribute('d')) || '';
      }
      const idle = document.getElementById(`${k}-wave-idle`);
      o[`${k}-idle`] = idle ? idle.classList.contains('is-on') : null;
    }
    return o;
  });

  const shot = async (name) => {
    await page.screenshot({ path: `${out}-${name}.png`, fullPage: true });
  };

  await shot('audio');
  const a = await grab();
  await page.waitForTimeout(700);
  const b = await grab();
  await page.waitForTimeout(700);
  const c = await grab();

  const bad = [];
  if (!a['sys-line']) bad.push('sys 波形路径为空（SVG 没画出来）');
  for (const k of ['sys', 'remote', 'mix']) {
    if (a[`${k}-line`] === b[`${k}-line`] && b[`${k}-line`] === c[`${k}-line`])
      bad.push(`${k} 波形 1.4 秒内没变化（快轮询没生效？）`);
  }
  if (a['sys-line'] && a['sys-line'] === a['remote-line'])
    bad.push('sys 和 remote 画的是同一份数据');
  if (a['remote-line'] && a['remote-line'] === a['mix-line'])
    bad.push('remote 和 mix 画的是同一份数据');

  // ── 其余页面截图，顺便确认切页不炸 ──
  for (const tab of ['mapping', 'settings', 'log']) {
    const btn = await page.$(`[data-page="${tab}"]`);
    if (!btn) { errs.push('NO TAB ' + tab); continue; }
    await btn.click();
    await page.waitForTimeout(600);
    await shot(tab);
  }

  // 切回音频页，波形应恢复更新（快轮询降频后要能提速回来）
  await page.click('[data-page="audio"]');
  await page.waitForTimeout(600);
  const d = await grab();
  if (d['sys-line'] && d['sys-line'] === c['sys-line']) {
    bad.push('切页回来之后波形不再更新');
  }

  console.log('WAVE  :', bad.length ? bad.join(' | ') : 'ok');
  console.log('ERRORS:', errs.length ? errs.join(' | ') : 'none');
  await browser.close();
  process.exit(bad.length || errs.length ? 1 : 0);
})();
