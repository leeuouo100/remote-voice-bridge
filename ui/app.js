/* Remote Voice Bridge · 控制台
   纯原生 JS，无依赖。所有数据都来自本地 http://127.0.0.1:<port>/api/*  */

'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

/* ── 安全建 DOM（2026-09-29 审查报告 P1-2）────────────────────────────────────
   以前渲染状态清单 / 设备列表时是把设备名、配置值直接**拼进 innerHTML**：

       $('#status-list').innerHTML = rows;   // rows 里有 ${r.value}

   而 r.value 的来源包括 `system_mic_device` 这类**可以由 /api/config 写入**
   的字段 —— 往配置里塞一个 `<img src=x onerror=...>` 就是存储型 XSS。
   拼字符串这条路只要有一处忘了转义就中招，所以整个文件**不再拼 innerHTML**：
   自由文本一律走 textContent（它永远当纯文本，不解析标签）。

   `el()` 就是为此准备的最小构造器；`iconFor()` 是唯一例外，见那里的注释。 */
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}

const DB_FLOOR = -60;          // 电平条最低刻度（dBFS）
const METER_SEGS = 14;         // 电平条格数
let   state = null;            // 最近一次 /api/state
let   pollTimer = null;
let   lastLogSize = -1;

// ── 小工具 ────────────────────────────────────────────────────────────────────
function toast(msg, bad) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.toggle('bad', !!bad);
  t.classList.add('is-on');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('is-on'), 2200);
}

async function api(path, body) {
  const opt = body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  if (!r.ok) throw new Error(await r.text() || r.statusText);
  return r.headers.get('content-type')?.includes('json') ? r.json() : r.text();
}

function fmtDb(db) {
  if (db === null || db === undefined || db <= DB_FLOOR) return '—';
  return db.toFixed(0) + ' dB';
}

// ── 波形 ──────────────────────────────────────────────────────────────────────
/* 三条波形（电脑麦克风 / 遥控器麦克风 / 混合输出）用同一套画法：
      · 浅色填充 = 上下对称的包络，一眼看出"这一段有没有声音、有多响"
      · 亮色细线 = 真实带正负的采样值，形状上能认出是人在说话还是底噪
   数值是 int16 量纲，先归一化再套一个 0.55 次方压缩。
   为什么压缩：正常说话大概只到满量程的 5%~15%，线性画出来几乎贴着中线，
   看着像"没声音"。开方之后小信号也能顶起来，这正是用户要的"明显一点"。 */
const WAVE_W = 300, WAVE_MID = 28, WAVE_AMP = 25;
const WAVE_COMPRESS = 0.55;

function waveShapes(samples) {
  const n = samples.length;
  if (n < 2) return ['', ''];
  const step = WAVE_W / (n - 1);
  const up = [], dn = [], tr = [];
  for (let i = 0; i < n; i++) {
    const raw = Math.max(-32768, Math.min(32767, samples[i] | 0));
    const v = raw / 32768;
    const h = Math.pow(Math.min(1, Math.abs(v)), WAVE_COMPRESS) * WAVE_AMP;
    const x = (i * step).toFixed(1);
    up.push(`${x} ${(WAVE_MID - h).toFixed(1)}`);
    dn.push(`${x} ${(WAVE_MID + h).toFixed(1)}`);
    // 带符号的真实波形（同样压缩，保持可读）
    const s = Math.sign(v) * Math.pow(Math.abs(v), WAVE_COMPRESS) * WAVE_AMP;
    tr.push(`${x} ${(WAVE_MID - s).toFixed(1)}`);
  }
  const fill = `M${up.join(' L')} L${dn.reverse().join(' L')} Z`;
  return [fill, `M${tr.join(' L')}`];
}

function paintWave(key, samples) {
  const fillEl = $(`#${key}-wave-fill`);
  const lineEl = $(`#${key}-wave-line`);
  if (!fillEl || !lineEl) return;
  const [fill, line] = waveShapes(samples || []);
  fillEl.setAttribute('d', fill);
  lineEl.setAttribute('d', line);
  // 完全静音时压一条提示文案盖在中线上，比"一条平线"信息量大
  const idle = $(`#${key}-wave-idle`);
  if (idle) {
    const peak = (samples || []).reduce((m, v) => Math.max(m, Math.abs(v)), 0);
    idle.classList.toggle('is-on', peak < 24);
  }
}

// 电平表（分段 LED，对标 vRemoter 的样子：一格一格亮起来，"响不响"一眼看完）
/* 为什么用分段而不是一条连续的细条：
     连续条只能看出"大概多长"，眼睛没法把它读成具体量级；
     分 14 格之后，亮到第几格是个可数的整数，"刚才够不够响"就有答案了。
     顶部三格过亮时转红 = 快到削顶，提醒把增益调小。 */
function paintMeter(key, db) {
  const meter = $(`#${key}-meter`);
  if (!meter) return;
  if (meter.childElementCount !== METER_SEGS) {        // 骨架只建一次，别每帧重建
    meter.replaceChildren(...Array.from({ length: METER_SEGS },
                                        () => document.createElement('i')));
  }
  const pct = db === null || db === undefined || db <= DB_FLOOR
    ? 0
    : Math.max(0, Math.min(1, (db - DB_FLOOR) / -DB_FLOOR));
  const lit = Math.round(pct * METER_SEGS);
  const kids = meter.children;
  for (let i = 0; i < METER_SEGS; i++) {
    const on = i < lit;
    kids[i].classList.toggle('is-on', on);
    kids[i].classList.toggle('is-hot', on && i >= METER_SEGS - 3);
  }
}

// ── 页面切换 ──────────────────────────────────────────────────────────────────
function activePage() {
  return $('.page.is-active')?.dataset.page || '';
}

/* `persist=false` 用于**自动翻页**（鼠标模式进出那一下）。
   ⚠ 自动翻的页绝不能写进 localStorage：下次打开控制台会去"恢复"它，
     而那时鼠标模式根本没开 —— 用户一进来就看到一张「鼠标模式 · 使用中」
     的说明页，状态和事实相反（这个项目栽过很多次的那类 bug）。 */
function activatePage(page, persist = true) {
  const btn = $(`.tab[data-page="${page}"]`);
  if (!btn || btn.hidden) return false;       // 隐藏的页签（＝鼠标模式页）不许进
  $$('.tab').forEach(b => b.classList.toggle('is-active', b === btn));
  $$('.page').forEach(p => p.classList.toggle('is-active', p.dataset.page === page));
  if (persist) localStorage.setItem('rvb.tab', page);
  if (page === 'log') refreshLog(true);
  // 波形要画得流畅就得高频轮询；离开音频页时降回低频省电
  fastTimer();
  return true;
}

function initTabs() {
  $$('.tab').forEach(btn => btn.addEventListener('click', () => activatePage(btn.dataset.page)));
  const saved = localStorage.getItem('rvb.tab');
  // ⚠ 不恢复「鼠标模式」那一页：它只在真的进入鼠标模式时才存在。
  if (saved && saved !== 'mouse') activatePage(saved);
}

// ── 渲染：顶部运行状态 ────────────────────────────────────────────────────────
function renderRunState() {
  const s = state.status;
  const el = $('#run-state');
  let text = '正在运行', cls = 'ok';
  if (!s.connected) { text = '等待遥控器'; cls = ''; }
  else if (s.streaming) { text = '语音中'; cls = 'warn'; }
  el.querySelector('.dot').className = 'dot ' + cls;
  $('#run-text').textContent = text;
}

// ── 渲染：触发键 ──────────────────────────────────────────────────────────────
function renderHotkey() {
  const sel = $('#hotkey-preset');
  if (sel.options.length === 0) {
    state.hotkey_presets.forEach(p => {
      const o = document.createElement('option');
      o.value = p.id;
      // 下拉项只显示按键本身（对标 vRemoter 的 "Option (⌥)"）；
      // 那句"哪个输入法用它"放 tooltip，免得下拉被撑得又宽又长。
      o.textContent = p.label;
      o.title = p.hint || '';
      sel.appendChild(o);
    });
  }
  const cur = state.hotkey.preset || 'custom';
  if (document.activeElement !== sel) sel.value = cur;

  const label = state.hotkey.label || '（未设置）';
  const isCustom = cur === 'custom';
  const set = state.hotkey.mode === 'hold' ? '按住说话' : '按一下开始/结束';
  const curEl = $('#hotkey-current');
  if (curEl) curEl.textContent = label;
  const modeEl = $('#hotkey-mode-label');
  if (modeEl) modeEl.textContent = set + (isCustom ? ' · 自定义' : '');
  const dot = $('#hotkey-dot');
  if (dot) dot.className = 'dot ' + (state.hotkey.keys.length ? 'ok' : 'warn');
  $('#set-hotkey-label').textContent = label;
}

// ── 渲染：三路音频 ────────────────────────────────────────────────────────────
function renderChannels() {
  const lv = state.levels, mx = state.mix, wv = state.waves || {};

  // 波形 + 分段电平表：三条路径各自独立，哪一段断了立刻看得出来
  ['sys', 'remote', 'mix'].forEach(k => {
    paintWave(k, wv[k]);
    paintMeter(k, lv[k]);
  });

  $('#sys-db').textContent    = fmtDb(lv.sys);
  $('#remote-db').textContent = fmtDb(lv.remote);
  $('#mix-db').textContent    = fmtDb(lv.mix);

  const active = db => db > DB_FLOOR;
  const setState = (dot, txt, db, onWord, offWord) => {
    $(dot).className = 'dot ' + (active(db) ? 'ok' : '');
    txt.textContent = active(db) ? onWord : offWord;
  };
  // ⚠ 参与混音的状态必须**写在卡片上**，不能只靠那个不起眼的勾选框。
  //   原因：麦克风一直在采集，没参与混音时电平条和波形照样在动 ——
  //   用户看到就以为"我明明关掉它了还在输出"。把这一路的状态词直接换成
  //   「未参与混音」/「已静音」、并把整张卡压暗，"它没有送出去"才一目了然。
  //   2026-09-15 武哥问的正是这句："我明明不让系统麦克风参与说话了，它还是在输出"。
  const soloAny = mx.sys_solo || mx.remote_solo;
  const live = (solo, muted, enabled) => !!(solo || (!soloAny && !muted && enabled));
  const sysLive = live(mx.sys_solo, mx.sys_muted, mx.sys_enabled);
  const remoteLive = live(mx.remote_solo, mx.remote_muted, mx.remote_enabled);

  setState('#sys-dot',  $('#sys-state'),  sysLive ? lv.sys : DB_FLOOR,
          '有声音', mx.sys_muted ? '已静音' : '未参与混音');
  setState('#remote-dot', $('#remote-state'), lv.remote, '有声音', '等待语音');
  setState('#mix-dot',  $('#mix-state'),  lv.mix,   '正在输出', '等待录音');
  document.querySelector('[data-chan="sys"]')?.classList.toggle('is-excluded', !sysLive);
  document.querySelector('[data-chan="remote"]')?.classList.toggle('is-excluded', !remoteLive);

  const devEl = $('#sys-dev');
  if (devEl) devEl.textContent = state.devices.system_mic || '未找到电脑麦克风';
  const rdevEl = $('#remote-dev');
  if (rdevEl) rdevEl.textContent = state.status.device || '蓝牙语音遥控器';
  $('#mix-dev').textContent = state.config.audio_output;

  // 混合输出卡上那行小结：说清"现在到底混进了哪几路"
  const sum = $('#mix-summary');
  if (sum) {
    const list = [];
    if (sysLive)    list.push('电脑麦克风');
    if (remoteLive) list.push('遥控器');
    sum.textContent = list.length
      ? `正在混合：${list.join(' + ')}`
      : '两路都没送出去（被静音或没参与）';
  }

  // 静音 / 独奏 / 参与混音
  $$('[data-mute]').forEach(b => b.classList.toggle('is-on', !!mx[b.dataset.mute + '_muted']));
  $$('[data-solo]').forEach(b => b.classList.toggle('is-on', !!mx[b.dataset.solo + '_solo']));
  $('#sys-enabled').checked    = !!mx.sys_enabled;
  $('#remote-enabled').checked = !!mx.remote_enabled;
}

// ── 渲染：状态清单 ────────────────────────────────────────────────────────────
// ⚠ 这里原来是把 name/value **拼进 innerHTML**。而 value 里可能是
//   `system_mic_device` 这类能由 /api/config 写入的字段 —— 往配置里塞一段
//   `<img src=x onerror=...>` 就成了存储型 XSS（2026-09-29 审查报告 P1-2）。
//   改成 DOM 节点 + textContent：textContent 永远当纯文本，不解析标签。
function renderStatusList() {
  $('#status-list').replaceChildren(...state.checklist.map(r => {
    const row = el('div', 'status-row');
    row.append(el('span', 'dot ' + (r.ok ? 'ok' : (r.warn ? 'warn' : ''))));
    row.append(el('span', 'name', r.name));
    row.append(el('span', 'val ' + (r.mono ? '' : 'muted'), r.value));
    return row;
  }));
}

// ── 渲染：诊断 ────────────────────────────────────────────────────────────────
function renderDiag() {
  const d = state.diagnostics;
  $('#d-frames').textContent = d.frames;
  $('#d-peak').textContent   = d.peak;
  $('#d-rate').textContent   = d.sample_rate ? d.sample_rate + ' Hz' : '—';
  $('#d-bytes').textContent  = d.frame_bytes ? d.frame_bytes + ' B' : '—';
  $('#d-ago').textContent    = d.last_audio_ago === null ? '—' : d.last_audio_ago.toFixed(1) + ' s 前';

  const hint = $('#diag-hint');
  if (state.status.streaming && d.frames === 0) {
    hint.textContent = '⚠ 本次 0 帧 —— 遥控器没有推音频上来，检查日志里有没有 📤 MIC_OPEN';
    hint.className = 'diag-hint bad';
  } else {
    hint.textContent = '';
    hint.className = 'diag-hint';
  }
}

// ── 渲染：设备列表 + 遥控器示意 ───────────────────────────────────────────────
// 同 renderStatusList：设备名/签名都是自由文本，走 textContent（P1-2）。
function renderDevices() {
  $('#device-list').replaceChildren(...state.supported_devices.map(d => {
    const item = el('div', 'dev-item ' + (d.active ? 'is-active' : ''));
    item.append(el('span', 'dot ' + (d.connected ? 'ok' : '')));
    const box = el('div');
    box.append(el('div', 'dev-name', d.name));
    box.append(el('div', 'dev-id', d.signature));
    item.append(box);
    item.append(el('span', 'dev-state ' + (d.connected ? 'ok' : ''),
                   d.connected ? '已连接' : '未连接'));
    return item;
  }));

  const active = state.supported_devices.find(d => d.active) || state.supported_devices[0];
  $('#map-dev-name').textContent = active ? active.name : '遥控器';
  $('#map-count').textContent = state.buttons.filter(b => !b.voice).length + ' 个可映射键';
}

// ── 渲染：按键映射 ────────────────────────────────────────────────────────────
const BTN_ICONS = {
  up:'M12 5l5 7h-10z', down:'M12 19l5-7h-10z', left:'M5 12l7-5v10z', right:'M19 12l-7-5v10z',
};

/* 图标 SVG 的字符串形式。⚠ 这里出现的**每一个值都是本文件里的常量**
   （BTN_ICONS 表和下面写死的 path/形状），没有任何外部数据进来。
   它是整个 app.js 里唯一还会碰到 innerHTML 的地方 —— 因为要建 SVG 命名空间
   下的节点，用 DOM API 逐个属性写会啰嗦十倍且更容易写错。
   缓存 + cloneNode：解析只做一次，之后每次复用。 */
function iconSvg(id) {
  if (BTN_ICONS[id]) return `<svg class="btn-ico" viewBox="0 0 24 24"><path d="${BTN_ICONS[id]}" fill="currentColor"/></svg>`;
  if (id === 'ok')    return `<svg class="btn-ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="7" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="12" r="2.6" fill="currentColor"/></svg>`;
  if (id === 'back')  return `<svg class="btn-ico" viewBox="0 0 24 24"><path d="M14 6l-6 6 6 6" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  if (id === 'home')  return `<svg class="btn-ico" viewBox="0 0 24 24"><path d="M5 11l7-6 7 6v7a1 1 0 0 1-1 1h-4v-5h-4v5H6a1 1 0 0 1-1-1z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>`;
  if (id === 'voice') return `<svg class="btn-ico" viewBox="0 0 24 24"><path d="M12 15a3.2 3.2 0 0 0 3.2-3.2V6.2a3.2 3.2 0 0 0-6.4 0v5.6A3.2 3.2 0 0 0 12 15z" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M6 11.6a6 6 0 0 0 12 0M12 17.6V20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>`;
  if (id === 'mute')  return `<svg class="btn-ico" viewBox="0 0 24 24"><path d="M5 10h3l4-3v10l-4-3H5z" fill="currentColor"/><path d="M16 9l4 6M20 9l-4 6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`;
  if (id === 'vol_up')return `<svg class="btn-ico" viewBox="0 0 24 24"><path d="M4 10h3l4-3v10l-4-3H4z" fill="currentColor"/><path d="M15 9v6M12 12h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`;
  if (id === 'vol_down')return `<svg class="btn-ico" viewBox="0 0 24 24"><path d="M4 10h3l4-3v10l-4-3H4z" fill="currentColor"/><path d="M12 12h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`;
  if (id === 'youtube')return `<svg class="btn-ico" viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="12" rx="4" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M11 9.5l4 2.5-4 2.5z" fill="currentColor"/></svg>`;
  if (id === 'netflix')return `<svg class="btn-ico" viewBox="0 0 24 24"><path d="M8 5v14M16 5v14M8 5l8 14" stroke="currentColor" stroke-width="1.7" fill="none"/></svg>`;
  if (id === 'power') return `<svg class="btn-ico" viewBox="0 0 24 24"><path d="M12 4v7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M7.5 7a7 7 0 1 0 9 0" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>`;
  if (id === 'input') return `<svg class="btn-ico" viewBox="0 0 24 24"><path d="M8 6v12M12 6v12M12 12h5" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round"/></svg>`;
  return `<svg class="btn-ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="6" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>`;
}

const _iconCache = new Map();
function iconFor(id) {
  let node = _iconCache.get(id);
  if (!node) {
    const tpl = document.createElement('template');
    tpl.innerHTML = iconSvg(id);          // ← 常量，见 iconSvg 的注释
    node = tpl.content.firstElementChild;
    _iconCache.set(id, node);
  }
  return node.cloneNode(true);
}

function renderMapping() {
  const box = $('#map-rows');
  // 只在结构变化（按键集合/可用目标集合）时重建，否则每次轮询重建会打断下拉交互
  const sig = JSON.stringify(state.buttons.map(b => b.id)) + '|' + Object.keys(state.targets).length;
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    const groups = state.target_groups || [{ label: '', ids: Object.keys(state.targets) }];

    // 目标下拉：用 new Option() 而不是拼 <option> 字符串 —— Option 的
    // value/label 都走 textContent 语义，自由文本进来也当纯文本（P1-2）。
    const makeSelect = () => {
      const sel = document.createElement('select');
      sel.className = 'select';
      groups.forEach(g => {
        const ids = g.ids.filter(id => id in state.targets);
        if (!ids.length) return;
        const host = g.label ? document.createElement('optgroup') : sel;
        if (g.label) host.label = g.label;
        ids.forEach(id => host.append(new Option(state.targets[id], id)));
        if (g.label) sel.append(host);
      });
      return sel;
    };

    box.replaceChildren(...state.buttons.map(b => {
      if (b.voice) {
        const item = el('div', 'map-item is-locked');
        item.append(iconFor(b.id), el('span', 'btn-name', b.label),
                    el('span', 'lock', '由语音通道处理（不可映射）'));
        return item;
      }
      const item = el('div', 'map-item');
      item.dataset.btn = b.id;
      const sel = makeSelect();
      sel.dataset.map = b.id;
      item.append(iconFor(b.id), el('span', 'btn-name', b.label), sel);
      return item;
    }));

    box.querySelectorAll('select[data-map]').forEach(sel => {
      sel.addEventListener('change', () => saveMapping(sel.dataset.map, sel.value));
      sel.closest('.map-item').addEventListener('mouseenter', () => highlightBtn(sel.dataset.map, true));
      sel.closest('.map-item').addEventListener('mouseleave', () => highlightBtn(sel.dataset.map, false));
    });
    // 自定义值需要临时补进下拉
    state.buttons.forEach(b => {
      if (b.voice) return;
      const sel = box.querySelector(`select[data-map="${b.id}"]`);
      if (sel && !sel.querySelector(`option[value="${cssEsc(b.value)}"]`)) {
        sel.appendChild(new Option('自定义：' + b.value, b.value));
      }
    });
  }
  // 同步当前值
  state.buttons.forEach(b => {
    if (b.voice) return;
    const sel = box.querySelector(`select[data-map="${b.id}"]`);
    if (sel && document.activeElement !== sel) sel.value = b.value;
  });
  $('#mapping-enabled').checked = !!state.config.mapping_enabled;

  // 「原样直通」说明：文案由后端 config.NATIVE_TARGET_HINT 提供，界面和文档共用一份。
  // ⚠️ 这个 div 以前一直是空的 —— 后端已经在 /api/state 里给了 native_hint，
  // 前端却没人去填，文案只活在文档里，改配置的人以为界面会跟着变。
  const hint = $('#native-hint');
  if (hint) {
    const text = state.native_hint || '';
    if (hint.dataset.text !== text) {
      hint.dataset.text = text;
      hint.textContent = '';
      // 按行渲染（用 textNode 而不是 innerHTML，文案里有自由文本，不拼 HTML）
      text.split('\n').forEach((line, i) => {
        if (i) hint.appendChild(document.createElement('br'));
        hint.appendChild(document.createTextNode(line));
      });
    }
  }
}

const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
const cssEsc = s => String(s ?? '').replace(/["\\]/g, '\\$&');

function highlightBtn(id, on) {
  // ⚠ 必须用 querySelectorAll：一颗按键在图上往往是**多个图形**拼出来的
  // （底圆 + 图标，语音键还有一圈橙色标记环）。只取第一个的话，
  // 悬停时只有底圆亮、图标不亮，看起来像"没选中"。
  const els = document.querySelectorAll(`#remote-svg [data-btn="${cssEsc(id)}"]`);
  els.forEach(el => el.classList.toggle('hl', !!on));
}

async function saveMapping(btn, value) {
  try {
    const r = await api('/api/mapping', { button: btn, value });
    const t = state.targets[value] || value;
    $('#map-note').textContent = `✓ 已保存：${btn} → ${t}`;
    setTimeout(() => { $('#map-note').textContent = ''; }, 3000);
    // effective === false：钩子没确认收到新表（旁路正在重挂 / 注入没起来）。
    // 这时界面显示"已保存"而按键可能还按旧表走 —— 必须说出来，
    // 否则用户会以为"这个键坏了"（P1-6）。
    if (r && r.effective === false) {
      toast('已保存，但按键钩子尚未确认（重连后生效）', true);
    }
  } catch (e) { toast('保存失败：' + e.message, true); }
}

// ── 渲染：设置页 ──────────────────────────────────────────────────────────────
let settingsTouched = 0;      // 用户刚操作过设置控件时，短暂停止从服务端回填
function renderSettings() {
  const c = state.config;
  if (Date.now() - settingsTouched > 2500) {
    const g1 = $('#set-remote-gain');
    if (document.activeElement !== g1) { g1.value = c.gain; }
    $('#set-remote-gain-val').textContent = (+c.gain).toFixed(1) + 'x';

    const g2 = $('#set-sys-gain');
    if (document.activeElement !== g2) { g2.value = c.system_mic_gain; }
    $('#set-sys-gain-val').textContent = (+c.system_mic_gain).toFixed(1) + 'x';

    fillSelect($('#set-output'), state.devices.output_list,
               state.devices.output_resolved || c.audio_output);
    fillSelect($('#set-sys-mic'), ['（系统默认）', ...state.devices.input_list],
               c.system_mic_device || '（系统默认）');
  }
  $('#set-hotkey-mode').value = c.hotkey_mode;
  $('#set-suppress').checked  = !!c.suppress_keys;
  $('#set-swallow-ok').checked = c.swallow_ok !== false;
  // v1.0.13「说完自动发送」：**默认关**（2026-09-17 武哥定案 —— 什么时候发由他决定，
  // 程序不许替他发出还没想好的话）。所以回填必须是 === true，见下面那行的注释。
  // ⚠ 必须是 === true（默认关）：写成 !== false 的话，字段一旦缺失就会显示成"已勾上"，
  //   与后端"缺字段＝不发"的兜底方向相反，用户会以为开着、实际没开（或反过来）。
  $('#set-send-after-voice').checked = c.send_after_voice === true;
  $('#set-send-key').value   = c.send_after_voice_key || 'enter';
  $('#set-send-delay').value = c.send_after_voice_delay_ms || 800;
  $('#set-autostart').checked = !!state.autostart;

  $('#set-device').textContent     = state.status.device || '未连接';
  $('#set-device-type').textContent = c.device;
  $('#set-out-name').textContent   = c.audio_output;
  $('#set-version').textContent    = state.version;
  $('#set-conf-dir').textContent   = state.config_dir;
}

function fillSelect(sel, values, current) {
  const vals = values.slice();
  // 配置里存的设备名可能是「前缀名」，或设备已被拔掉不在列表里。
  // 此时把它补成一项，否则 sel.value 匹配不上任何 option，
  // 下拉框会渲染成空白 —— 看着像没设置，其实是有值的。
  if (current && !vals.includes(current)) vals.unshift(current);
  const sig = vals.join('\u0001');
  if (sel.dataset.sig !== sig) {
    sel.dataset.sig = sig;
    // new Option(text, value) 而不是拼 <option>：自由文本当纯文本处理（P1-2）
    sel.replaceChildren(...vals.map(v => new Option(v, v)));
  }
  if (document.activeElement !== sel && current !== undefined) sel.value = current;
}

// ── 渲染：鼠标模式（v1.0.31）─────────────────────────────────────────────────
let _mouseWasOn = false;       // 上一次渲染时是否在鼠标模式里（用来抓"进入/退出"那一沿）
let _mousePrevPage = null;     // 进鼠标模式之前停在哪个页面（退出时翻回去）

// ⚠ 这个函数必须在**快档**（130ms）也调：鼠标模式是"接管方向键"的状态，
//   晚一秒才显示出来，用户就已经把"按方向键没反应"当成故障了 ——
//   这个项目被"静默状态"坑过太多次（见 CHANGELOG v1.0.25 / v1.0.30）。
function renderMouse() {
  const on = !!(state.status && state.status.mouse_mode);
  const chip = $('#mouse-state');
  if (chip) chip.hidden = !on;                 // 顶栏那颗胶囊只在真的进模式时出现

  // 鼠标模式那一页：**只在进入时存在**，并在进入的那一刻自动翻过去
  // （用户原话：「要进入鼠标模式，就要按那个信源键，然后在控制台里面也会出一个页面，
  //   说明这个鼠标模式是怎么使用的」）。退出时如果正停在这一页就翻回原来那一页 ——
  // 别把用户留在一张页签已经消失的页面上。
  const tab = $('#tab-mouse');
  if (tab) tab.hidden = !on;
  if (on && !_mouseWasOn) {
    const cur = activePage();
    if (cur && cur !== 'mouse') _mousePrevPage = cur;
    activatePage('mouse', false);
  } else if (!on && _mouseWasOn && activePage() === 'mouse') {
    activatePage(_mousePrevPage || 'mapping', false);
  }
  _mouseWasOn = on;

  const box = $('#mouse-status');
  if (box) {
    box.classList.toggle('is-on', on);
    $('#mouse-status-text').textContent = on
      ? '正在鼠标模式：方向键推指针（再按一下「信源」退出）'
      : '未进入鼠标模式';
  }
  const d = state.diagnostics || {};
  // 「到底动没动」是用户最想知道的一件事 —— 只给一个"已进入"没法排查。
  const txt = on
    ? `${(+d.mouse_speed_now || 0).toFixed(1)} px/帧 · 本次已走 ${d.mouse_moved_px || 0} px`
    : '';
  const live = $('#mouse-live');
  if (live) live.textContent = txt;
  const hero = $('#mouse-hero-live');
  if (hero) hero.textContent = txt || '—';
  const hint = $('#mouse-idle-hint');
  if (hint) {
    const s = +(state.config && state.config.mouse_idle_exit_s);
    hint.textContent = (on && s > 0)
      ? `（${s} 秒不碰遥控器会自动退出，防止搁在沙发上被压到）`
      : '';
  }
}

// 鼠标模式的开关 + 四个滑块（值来自 config，和设置页一样受 settingsTouched 保护 ——
// 用户正在拖的时候不许从服务端回填，否则滑块会被拽回去）
function renderMouseConfig() {
  if (Date.now() - settingsTouched <= 2500) return;
  const c = state.config || {};
  const set = (sel, val, fmt) => {
    const r = $(sel);
    if (!r) return;
    if (document.activeElement !== r) r.value = val;
    const lab = $(sel + '-val');
    if (lab) lab.textContent = fmt(val);
  };
  // `!== false`：字段缺失时按"默认开"显示 —— 和 config.py 的默认值同向。
  // 写成 `=== true` 会让老配置（没有这个字段）显示成"已关闭"，与后端实际相反。
  $('#mouse-enabled').checked = c.mouse_mode_enabled !== false;
  set('#mouse-speed',     +c.mouse_speed || 5,        v => (+v).toFixed(1));
  set('#mouse-speed-max', +c.mouse_speed_max || 20,   v => String(Math.round(+v)));
  set('#mouse-accel',     c.mouse_accel_ms ?? 800,    v => String(Math.round(+v)));
  set('#mouse-idle',      c.mouse_idle_exit_s ?? 60,  v => String(Math.round(+v)));
}

// ── 日志 ──────────────────────────────────────────────────────────────────────
async function refreshLog(force) {
  try {
    const r = await api('/api/log');
    if (force || r.size !== lastLogSize) {
      lastLogSize = r.size;
      const el = $('#log-body');
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 60;
      el.textContent = r.text;
      if ($('#log-follow').checked || atBottom || force) el.scrollTop = el.scrollHeight;
    }
  } catch (_) { /* 服务未就绪时静默 */ }
}

// ── 轮询 ──────────────────────────────────────────────────────────────────────
/* 两档节奏：
     · 快档（~130ms）只拉 /api/live —— 状态 + 三路电平/波形，用来把波形画流畅。
       波形要像"活的"，刷新率低于 8fps 就会一顿一顿。
     · 慢档（~900ms）拉完整 /api/state —— 设备列表、按键表、状态清单这些
       变化很慢的东西。它要读配置、枚举输入设备，开销比 live 那点数据大得多，
       塞进快档纯属浪费（还会让波形卡）。
   离开音频页时快档降频，别在别的页面上白烧 CPU。 */
const FAST_MS = 130, SLOW_MS = 900, FAST_IDLE_MS = 700;
let fastTimerId = null;

function onAudioPage() {
  return $('.page[data-page="audio"]')?.classList.contains('is-active');
}

async function pollLive() {
  try {
    const live = await api('/api/live');
    Object.assign(state, live);
    renderRunState();
    renderChannels();
    renderDiag();
    renderMouse();
  } catch (_) { /* 失联时交给慢档去报错，这里不重复刷 */ }
}

function fastTimer() {
  clearInterval(fastTimerId);
  fastTimerId = setInterval(pollLive, onAudioPage() ? FAST_MS : FAST_IDLE_MS);
}

async function poll() {
  try {
    state = await api('/api/state');
    renderRunState();
    renderHotkey();
    renderChannels();
    renderStatusList();
    renderDiag();
    renderDevices();
    renderMapping();
    renderSettings();
    renderMouse();
    renderMouseConfig();
  } catch (e) {
    $('#run-state .dot').className = 'dot bad';
    $('#run-text').textContent = '与控制台服务失联';
  }
}

// ── 录制组合键 ────────────────────────────────────────────────────────────────
let recTimer = null;
async function openRecorder(target, titleText) {
  $('#rec-mask').classList.add('is-open');
  $('#rec-title').textContent = titleText;
  $('#rec-desc').textContent = '请按下要映射的组合键…（按 Esc 取消）';
  $('#rec-keys').replaceChildren();
  await api('/api/record/start', {});
  clearInterval(recTimer);
  recTimer = setInterval(async () => {
    let r;
    try { r = await api('/api/record/poll'); } catch (_) { return; }
    if (r.status === 'recording') {
      const held = r.held || [];
      $('#rec-keys').replaceChildren(...(held.length
        ? held.map(k => el('kbd', null, k))
        : [el('span', 'tiny-note', '等待按键…')]));
      return;
    }
    clearInterval(recTimer);
    $('#rec-mask').classList.remove('is-open');
    if (r.status === 'ok' && r.value) {
      await applyRecorded(target, r.value);
    } else {
      // 取消 / 超时 / 键名解析不了，都要把控件还原成"当前实际生效的值"，
      // 否则下拉框会停在"自定义…"上，看着像设置成功了，其实没有。
      if (r.status === 'cancel') toast('已取消');
      else toast(r.message || '未检测到按键', true);
      revertSelect(target);
    }
  }, 180);
}

// 撤销「选了自定义但没录成」造成的假象：把控件回填成服务端的真实值
function revertSelect(target) {
  if (target === 'voice_hotkey') {
    const sel = $('#hotkey-preset');
    if (sel) sel.value = state.hotkey.preset || 'custom';
    renderHotkey();
  } else {
    renderMapping();
  }
}

async function applyRecorded(target, value) {
  try {
    if (target === 'voice_hotkey') {
      const r = await api('/api/hotkey', { keys: value.split('+') });
      toast('触发键已设为 ' + (r.label || value));
    } else {
      await saveMapping(target, value);
    }
  } catch (e) {
    // 服务端会拒掉发不出去的键名。必须把原话说给用户听 ——
    // 这里静默吞掉的话，用户看到的就是「设了完全没用」。
    toast('设置失败：' + e.message, true);
    revertSelect(target);
  }
}

// ── 事件绑定 ──────────────────────────────────────────────────────────────────
function initEvents() {
  // 触发键下拉
  $('#hotkey-preset').addEventListener('change', async e => {
    const id = e.target.value;
    if (id === 'custom') { openRecorder('voice_hotkey', '录制语音触发键'); return; }
    const preset = state.hotkey_presets.find(p => p.id === id);
    if (!preset) return;
    await api('/api/hotkey', { keys: preset.keys });
    toast('触发键已设为 ' + preset.label);
  });
  $('#btn-record-hotkey').addEventListener('click', () => openRecorder('voice_hotkey', '录制语音触发键'));
  $('#btn-test-hotkey')?.addEventListener('click', async () => {
    await api('/api/test_hotkey', {});
    toast('已模拟按住 1 秒，看输入法有没有弹出语音条');
  });

  // 静音 / 独奏
  $$('[data-mute]').forEach(b => b.addEventListener('click', async () => {
    const k = b.dataset.mute + '_muted';
    await api('/api/mix', { [k]: !state.mix[k] });
  }));
  $$('[data-solo]').forEach(b => b.addEventListener('click', async () => {
    const k = b.dataset.solo + '_solo';
    await api('/api/mix', { [k]: !state.mix[k] });
  }));
  $('#sys-enabled').addEventListener('change', e => api('/api/mix', { sys_enabled: e.target.checked }));
  $('#remote-enabled').addEventListener('change', e => api('/api/mix', { remote_enabled: e.target.checked }));

  // 映射启用开关
  $('#mapping-enabled').addEventListener('change', async e => {
    const r = await api('/api/config', { mapping_enabled: e.target.checked });
    if (r && r.effective === false) {
      toast('已保存，但按键钩子尚未确认（重连后生效）', true);
    }
  });
  $('#btn-reset-map').addEventListener('click', async () => {
    const r = await api('/api/mapping/reset', {});
    if (r && r.effective === false) {
      toast('已恢复默认，但按键钩子尚未确认（重连后生效）', true);
    } else {
      toast('已恢复默认映射');
    }
  });

  // 设置页
  const touch = () => { settingsTouched = Date.now(); };
  $('#set-remote-gain').addEventListener('input', e => {
    touch();
    $('#set-remote-gain-val').textContent = (+e.target.value).toFixed(1) + 'x';
    api('/api/config', { gain: +e.target.value });
  });
  $('#set-sys-gain').addEventListener('input', e => {
    touch();
    $('#set-sys-gain-val').textContent = (+e.target.value).toFixed(1) + 'x';
    api('/api/mix', { sys_gain: +e.target.value });
  });
  $('#set-output').addEventListener('change', async e => {
    touch();
    await api('/api/config', { audio_output: e.target.value });
    toast('已保存 · 点「重新连接」生效');
  });
  $('#set-sys-mic').addEventListener('change', async e => {
    touch();
    const v = e.target.value === '（系统默认）' ? '' : e.target.value;
    await api('/api/config', { system_mic_device: v });
    toast('已保存 · 点「重新连接」生效');
  });
  $('#set-hotkey-mode').addEventListener('change', e => api('/api/config', { hotkey_mode: e.target.value }));
  $('#set-suppress').addEventListener('change', e => {
    api('/api/config', { suppress_keys: e.target.checked });
    toast('需重新连接生效');
  });
  // 立刻生效（映射表按 mtime 热重载），不用重连
  $('#set-swallow-ok').addEventListener('change', e =>
    api('/api/config', { swallow_ok_during_voice: e.target.checked }));

  // v1.0.13 说完自动发送。三处都是**立刻生效**：桥的主循环每轮读一次配置，
  // 改完下一轮就按新值走，不用重连（重连一次遥控器要哑几秒）。
  $('#set-send-after-voice').addEventListener('change', e =>
    api('/api/config', { send_after_voice: e.target.checked }));
  // 输入框用 change 而不是 input：否则边打字边提交，配置被写成一堆半截值
  $('#set-send-key').addEventListener('change', e => {
    const v = (e.target.value || '').trim() || 'enter';
    e.target.value = v;
    api('/api/config', { send_after_voice_key: v });
  });
  $('#set-send-delay').addEventListener('change', e => {
    // ⚠ 服务端白名单只做类型转换、不夹取值域，所以下限必须在这里守。
    //   太小会在输入法把文字落进输入框**之前**就回车 —— 等于把用户刚说的
    //   那句话弄丢一次，比不自动发更糟。
    const ms = Math.max(300, parseInt(e.target.value || '800', 10) || 800);
    e.target.value = ms;
    api('/api/config', { send_after_voice_delay_ms: ms });
  });
  $('#set-autostart').addEventListener('change', e => api('/api/autostart', { enabled: e.target.checked }));

  // ── 鼠标模式（v1.0.31）────────────────────────────────────────────────────
  // 全部**热生效**：桥的主循环每轮读一次 config.json（mtime 缓存），
  // 改完下一轮就按新值走，不用重连（重连一次遥控器要哑几秒）。
  $('#mouse-enabled').addEventListener('change', e =>
    api('/api/config', { mouse_mode_enabled: e.target.checked }));

  const mslider = (sel, key, fmt) => {
    $(sel).addEventListener('input', e => {
      touch();
      $(sel + '-val').textContent = fmt(e.target.value);
      api('/api/config', { [key]: +e.target.value });
    });
  };
  // ⚠ 取值域必须在前端也守一道：服务端白名单只做类型转换、**不夹取值域**
  //   （见 `_patch_config`）。起步速度 < 1 会慢到"按了跟没按一样"；
  //   上限低于起步时引擎会自己抬到起步（`MouseMover.configure`），
  //   但界面上两个滑块看起来自相矛盾，不如在这里就挡住。
  mslider('#mouse-speed',     'mouse_speed',       v => Math.max(1, +v).toFixed(1));
  mslider('#mouse-speed-max', 'mouse_speed_max',   v => String(Math.round(Math.max(2, +v))));
  mslider('#mouse-accel',     'mouse_accel_ms',    v => String(Math.round(Math.max(0, +v))));
  mslider('#mouse-idle',      'mouse_idle_exit_s', v => String(Math.round(Math.max(0, +v))));

  $('#btn-reload-dev').addEventListener('click', async () => {
    // ?devices=1 绕过服务端的声卡列表缓存，强制重新枚举
    const r = await api('/api/state?devices=1');
    state = r;
    $('#set-output').dataset.sig = '';
    $('#set-sys-mic').dataset.sig = '';
    renderSettings();
    toast('设备列表已刷新');
  });
  $('#btn-reconnect').addEventListener('click', async () => {
    await api('/api/reconnect', {});
    toast('已请求重连，约 3 秒后恢复');
  });
  $('#btn-open-log').addEventListener('click', () => api('/api/open', { what: 'log' }));
  $('#btn-open-conf').addEventListener('click', () => api('/api/open', { what: 'config' }));
  $('#btn-repo').addEventListener('click', () => api('/api/open', { what: 'repo' }));

  $('#btn-clear-log').addEventListener('click', async () => {
    await api('/api/log/clear', {});
    lastLogSize = -1; refreshLog(true);
  });
  $('#log-follow').addEventListener('change', () => refreshLog(true));
  $('#btn-rec-cancel').addEventListener('click', async () => {
    clearInterval(recTimer);
    $('#rec-mask').classList.remove('is-open');
    await api('/api/record/cancel', {});
  });
}

// ── 启动 ──────────────────────────────────────────────────────────────────────
(function boot() {
  initTabs();
  initEvents();
  poll().then(() => {
    pollTimer = setInterval(poll, SLOW_MS);
    fastTimer();                       // 波形走快档
    setInterval(() => { if ($('.page[data-page="log"]').classList.contains('is-active')) refreshLog(false); }, 1000);
  });
  // 关页面时告诉服务端可以收工（托盘图标还在，不影响）
  window.addEventListener('beforeunload', () => navigator.sendBeacon?.('/api/bye'));
})();
