// 看板内联 JS 的**运行时**冒烟（Node + vm 桩）
//
// 为什么需要它：2026-10-21 生产事故「荐单暂不可用：num is not defined」是**运行时**才炸的。
//   · `node --check`（语法）查不出；
//   · `tools/check_docs_claims.py` 的 `tools/check_js_scope.py`（作用域）能查出「会炸」，
//     但查不出「面板到底渲染出什么」；
//   · 而**只有真跑一次 loadClv()/loadRecommend()，看面板里是不是真内容**，才算验收。
//     上一轮只验了 API（都 200）就以为没问题 ⇒ 漏掉了这个事故。
//
// 用法：node tools/dashboard_smoke.js [--html static/dashboard.html]
// 退出码：0 = 三个面板都渲染出真内容；1 = 有面板显示「暂不可用」或抛异常。
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const repoRoot = path.resolve(__dirname, '..');

function argValue(flag, fallback) {
  const i = process.argv.indexOf(flag);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const htmlPath = path.resolve(repoRoot, argValue('--html', 'static/dashboard.html'));
const html = fs.readFileSync(htmlPath, 'utf8');

// 抽出所有内联 script（无 src 的），取最后一个（看板只有一个）。
const blocks = [];
const re = /<script(?![^>]*\ssrc=)[^>]*>([\s\S]*?)<\/script>/g;
let m;
while ((m = re.exec(html)) !== null) blocks.push(m[1]);
if (!blocks.length) {
  console.error('FAIL: 页面里没有内联 script');
  process.exit(1);
}
const code = blocks[blocks.length - 1];

// ── DOM 桩 ────────────────────────────────────────────────────────────────
function makeEl(id) {
  const classes = new Set();
  return {
    id: id || '',
    innerHTML: '',
    textContent: '',
    value: '',
    hidden: false,
    checked: false,
    dataset: {},
    style: { removeProperty() {}, setProperty() {}, getPropertyValue() { return ''; } },
    children: [],
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, on) => { if (on === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); } else if (on) classes.add(c); else classes.delete(c); },
    },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { this.children.push(c); }, removeChild() {},
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, focus() {}, click() {}, remove() {},
    getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; },
  };
}

const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, makeEl(id));
  return elements.get(id);
}

const documentStub = {
  querySelector(sel) {
    const hit = /#([A-Za-z0-9_-]+)/.exec(String(sel));
    return el(hit ? hit[1] : 'anonymous');
  },
  querySelectorAll() { return []; },
  getElementById(id) { return el(id); },
  createElement() { return makeEl(''); },
  addEventListener() {}, removeEventListener() {},
  body: makeEl('body'),
  documentElement: makeEl('html'),
};

// ── fetch 桩：按路径返回 canned payload ───────────────────────────────────
const CLV_PAYLOAD = {
  rows: [{ date: '2026-10-21', league_key: 'epl', n_matches: 20, mean_clv: 0.0312 }],
  summary: { n_matches: 20, n_days: 1, mean_clv: 0.0312 },
};
const RECOMMEND_PAYLOAD = {
  picks: [{
    home_cn: '阿森纳', away_cn: '切尔西', league_key: 'epl', option_code: 'h',
    match_date: '2026-10-21', sp: 1.85, p_model: 0.62, ev: 0.0735, kelly: 0.08,
  }],
  record: {
    n_settled: 13, n_hit: 2, n_miss: 11, staked: 26, roi: -0.2208,
    insufficient: false, by_day: [{ date: '2026-10-20', n: 5, hits: 1, roi: -0.3 }],
    detail: [{ match_date: '2026-10-20', league_key: 'epl', option_code: 'h', sp_decide: 1.9, sp_settle: 1.95, hit: false, pnl: -10 }],
  },
  pricing_note: '定价用竞彩官方 SP。',
  calendar_note: '日历口径：BJT。',
};
const ACCURACY_PAYLOAD = {
  leagues: { epl: { '7d': { reconciled: 20, direction_accuracy: 0.55, score_accuracy: 0.2, over_under_accuracy: 0.5 }, '30d': {} } },
};
const ACCURACY_PENDING = { epl: 3 };

function payloadFor(pathname) {
  if (pathname.indexOf('/clv') >= 0) return CLV_PAYLOAD;
  if (pathname.indexOf('/recommend') >= 0) return RECOMMEND_PAYLOAD;
  if (pathname.indexOf('/accuracy/breakdown') >= 0) return ACCURACY_PAYLOAD;
  if (pathname.indexOf('/accuracy') >= 0) return ACCURACY_PENDING;
  return {};
}

const fetchStub = (url) => {
  const u = String(url);
  const body = payloadFor(u);
  return Promise.resolve({
    status: 200, ok: true,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  });
};

const localStorageStub = (() => {
  const store = new Map();
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
    clear: () => store.clear(),
  };
})();

const sandbox = {
  console,
  fetch: fetchStub,
  document: documentStub,
  localStorage: localStorageStub,
  navigator: { userAgent: 'node-smoke' },
  location: { href: 'http://127.0.0.1/dashboard/jc/', pathname: '/dashboard/jc/', reload() {} },
  setTimeout, clearTimeout, setInterval, clearInterval,
  URLSearchParams,
  IntersectionObserver: class { constructor() {} observe() {} disconnect() {} unobserve() {} },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

let loadError = null;
try {
  vm.runInContext(code, sandbox, { filename: 'dashboard-inline.js' });
} catch (e) {
  loadError = e;
}

const PANELS = [
  { id: 'clvPanel', must: 'CLV' },
  { id: 'recommendPanel', must: '今日推荐单' },
  { id: 'recordPanel', must: '推荐单战绩' },
];

async function main() {
  if (loadError) {
    console.error('FAIL: 看板内联脚本加载即抛异常：' + (loadError && loadError.stack ? loadError.stack : loadError));
    process.exit(1);
  }
  const ids = JSON.stringify(PANELS.map((p) => p.id));
  const probe = `
    (async () => {
      const out = {};
      const run = async (fn) => {
        try { await fn(); out[fn.name] = 'ok'; }
        catch (e) { out[fn.name] = 'THREW: ' + (e && e.message ? e.message : String(e)); }
      };
      await run(loadClv);
      await run(loadRecommend);
      await run(loadAccuracy);
      out.__html = ${ids}.reduce((a, id) => {
        const e = document.getElementById(id);
        a[id] = e ? String(e.innerHTML || '') : '';
        return a;
      }, {});
      return JSON.stringify(out);
    })()
  `;
  const raw = await vm.runInContext(probe, sandbox, { filename: 'dashboard-probe.js' });
  const res = JSON.parse(raw);

  let failed = false;
  for (const fn of ['loadClv', 'loadRecommend', 'loadAccuracy']) {
    if (res[fn] !== 'ok') {
      console.error('FAIL: ' + fn + '() 抛异常 → ' + res[fn]);
      failed = true;
    }
  }
  for (const p of PANELS) {
    const htmlOut = res.__html[p.id] || '';
    if (!htmlOut) {
      console.error('FAIL: 面板 #' + p.id + ' 渲染为空');
      failed = true;
      continue;
    }
    if (htmlOut.indexOf('暂不可用') >= 0 || htmlOut.indexOf('算不出来') >= 0) {
      console.error('FAIL: 面板 #' + p.id + ' 显示的是「暂不可用」⇒ 前端又出运行时错误了');
      console.error('      ' + htmlOut.slice(0, 300));
      failed = true;
      continue;
    }
    if (htmlOut.indexOf(p.must) < 0) {
      console.error('FAIL: 面板 #' + p.id + ' 里没有「' + p.must + '」⇒ 渲染的不是预期内容');
      console.error('      ' + htmlOut.slice(0, 300));
      failed = true;
      continue;
    }
    console.log('OK  : #' + p.id + ' 渲染出 ' + htmlOut.length + ' 字符，含「' + p.must + '」');
  }
  if (failed) process.exit(1);
  console.log('看板内联 JS 运行时冒烟：3 个面板全部渲染出真内容');
}

main().catch((e) => {
  console.error('FAIL: 冒烟脚本自身抛异常：' + (e && e.stack ? e.stack : e));
  process.exit(1);
});
