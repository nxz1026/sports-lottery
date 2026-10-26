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

// ⚠️ P2-A3：冒烟进程固定 TZ=UTC。
// 目的：让「用**浏览器本地时区**渲染时刻」的实现与「显式按 **BJT（Asia/Shanghai）**渲染」
// 的实现在本进程里**产出不同** —— 否则机器本身就在 UTC+8，两种写法输出一样，
// 断言就成了空断言（怎么改都绿）。
process.env.TZ = 'UTC';

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
// ⚠️ P2-A3/A5：P2-A 组断言需要**第二套沙箱**（冻结时钟）。
//    两套沙箱**必须各有一份 DOM 桩** —— 共用一份的话，A 套的 loader 会把 B 套刚
//    渲染出来的面板内容冲掉，断言会读到别的沙箱的结果（⇒ 空断言/假通过）。
function makeDom() {
  const elements = new Map();

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

  function el(id) {
    if (!elements.has(id)) elements.set(id, makeEl(id));
    return elements.get(id);
  }

  const documentStub = {
    querySelector(sel) {
      const hit = /#([A-Za-z0-9_-]+)/.exec(String(sel));
      return el(hit ? hit[1] : 'anonymous');
    },
    // ⚠️ P1-G：``markUnlogged()`` 靠 ``querySelectorAll(".empty.loading")``
    //    找首屏那批假 loading 占位符 ⇒ 桩必须**真的**按 class 过滤，
    //    否则该断言会变成空断言（永远 0 命中 ⇒ 永远「通过」）。
    querySelectorAll(sel) {
      const s = String(sel).trim();
      const all = Array.from(elements.values());
      if (s.charAt(0) === '.') {
        const need = s.split('.').filter(Boolean);
        if (!need.length) return [];
        return all.filter((e) => need.every((c) => e.classList.contains(c)));
      }
      const idm = /^#([A-Za-z0-9_-]+)$/.exec(s);
      if (idm) {
        const hit = elements.get(idm[1]);
        return hit ? [hit] : [];
      }
      return [];
    },
    getElementById(id) { return el(id); },
    createElement() { return makeEl(''); },
    addEventListener() {}, removeEventListener() {},
    body: makeEl('body'),
    documentElement: makeEl('html'),
  };

  // ── P1-G：首屏「假 loading」占位符种子（模拟真实 DOM 里那几个占位 div）────────
  // 真实页面：#matchGrid / #champBody / #aiSummary 里各有一个
  //   `<div class="empty loading">正在加载…</div>`。
  // 未登录时 loadToday() 等**根本不会被调用**（探测直接跳登录页）⇒
  // 这些占位符会永远停在「正在加载」⇒ 看起来像一直在加载。
  // 这里把占位符**建成子元素**并塞进容器里，和真实 DOM 结构一致。
  function seedLoadingPlaceholder(parentId, childId, text) {
    const parent = el(parentId);
    const child = makeEl(childId);
    child.classList.add('empty');
    child.classList.add('loading');
    child.textContent = text;
    parent.children.push(child);
    elements.set(childId, child);
  }

  return { documentStub, elements, el, seedLoadingPlaceholder };
}

const dom = makeDom();
const elements = dom.elements;
const el = dom.el;
const documentStub = dom.documentStub;
dom.seedLoadingPlaceholder('matchGrid', 'matchGridLoading', '正在加载今日预测…');
dom.seedLoadingPlaceholder('champBody', 'champBodyLoading', '正在加载冠军概率…');
dom.seedLoadingPlaceholder('aiSummary', 'aiSummaryLoading', '正在读取 AI 状态…');

// ── P1-G：首屏「假 loading」占位符种子（模拟真实 DOM 里那几个占位 div）────────
// 真实页面：#matchGrid / #champBody / #aiSummary 里各有一个
//   `<div class="empty loading">正在加载…</div>`。
//   未登录时 loadToday() 等**根本不会被调用**（探测直接跳登录页）⇒
//   这些占位符会永远停在「正在加载」⇒ 看起来像一直在加载。
// 这里把占位符**建成子元素**并塞进容器里，和真实 DOM 结构一致。
// （占位符种子已在上面 makeDom() 内部完成 —— 见 makeDom().seedLoadingPlaceholder）


// ── fetch 桩：按路径返回 canned payload ───────────────────────────────────
const CLV_PAYLOAD = {
  rows: [{ date: '2026-10-21', league_key: 'epl', n_matches: 20, mean_clv: 0.0312 }],
  summary: { n_matches: 20, n_days: 1, mean_clv: 0.0312 },
};
// ⚠️ P2-N（2026-10-23）：不达标荐单的**逐条标注**文案（生产里由后端给，
//   这里作为桩数据用；同样的字符串要能在沙箱里被断言取到 ⇒ 注入沙箱全局）。
const P2N_BELOW_LABEL = '不满足现行门槛（EV ≥ 12.9%）：本条来自更早的荐单批次，不代表现行门槛下的推荐。';
const RECOMMEND_PAYLOAD = {
  picks: [{
    home_cn: '阿森纳', away_cn: '切尔西', league_key: 'epl', option_code: 'h',
    match_date: '2026-10-21', sp: 1.85, p_model: 0.62, ev: 0.0735, kelly: 0.08,
    // ⚠️ P2-N（2026-10-23）：EV 0.0735 **低于现行门槛** 12.9% ⇒ 必须被逐条标注。
    //   旧实现把它照旧当荐单展示 ⇒ 与面板上写的「门槛 EV ≥ 12.9%」自相矛盾（说假话）。
    meets_threshold: false, threshold_label: P2N_BELOW_LABEL,
  }, {
    // 达标的那条：绝不许带「不满足现行门槛」的标注（否则标注失去意义）。
    home_cn: '热刺', away_cn: '阿森纳', league_key: 'epl', option_code: 'a',
    match_date: '2026-10-21', sp: 3.1, p_model: 0.45, ev: 0.395, kelly: 0.12,
    meets_threshold: true, threshold_label: '',
  }],
  picks_n_below_threshold: 1,
  picks_threshold_note: '⚠️ 本面板有 1 条荐单不满足现行门槛（EV ≥ 12.9%）：它们是更早批次留下的行（同一比赛日多次跑批时旧行不会被清除），不代表现行门槛下的推荐。',
  record: {
    n_settled: 13, n_hit: 2, n_miss: 11, staked: 26, roi: -0.2208,
    insufficient: false, by_day: [{ date: '2026-10-20', n: 5, hits: 1, roi: -0.3 }],
    detail: [{ match_date: '2026-10-20', league_key: 'epl', option_code: 'h', sp_decide: 1.9, sp_settle: 1.95, hit: false, pnl: -10 }],
    // ⚠️ P2-N：战绩跨了两套门槛 ⇒ 命中率/ROI 是混合口径 ⇒ 必须写出来。
    n_below_threshold: 2,
    threshold_note: '战绩口径：已结算的 13 条荐单里有 2 条不满足现行门槛（EV ≥ 12.9%）—— 来自更早的荐单批次 ⇒ 命中率/ROI 是混合口径，不能直接当作现行门槛的表现。',
  },
  pricing_note: '定价用竞彩官方 SP。',
  calendar_note: '日历口径：BJT。',
  offer_freshness: {
    threshold_hours: 6, latest_snap: '2026-10-22 14:48:07+00', age_hours: 0.54,
    stale: false, arrival_ts: '2026-10-22 14:50:05+00', arrival_age_hours: 0.47,
    arrival_stale: false, degraded: false,
  },
};
const ACCURACY_PAYLOAD = {
  leagues: { epl: { '7d': { reconciled: 20, direction_accuracy: 0.55, score_accuracy: 0.2, over_under_accuracy: 0.5 }, '30d': {} } },
};
const ACCURACY_PENDING = { epl: 3 };

// ── P2-A：临时赛事表（A9：表头色值不许再写死）+ 数据源状态（A5：当日额度口径）──
// 临时赛事（亚运等）只出现在 /daily-image 的 temp_rows 里；旧实现给表头写死
// 浅黄底 #ffe082 ⇒ 暗色模式下一块刺眼的浅黄。
const TEMP_LEAGUES_PAYLOAD = {
  temp_rows: [{
    league_cn: '亚运', match_num: '001', home_cn: '甲队', away_cn: '乙队',
    goal_line: '0', kickoff_bj: '2026-10-23 18:00', had: { h: '2.10', d: '3.20', a: '3.40' },
  }],
};
// /sources/status 的配额：后端 jobs.quota_usage() 已按 **BJT 自然日** 归一（day 键是 BJT 日期）。
const SOURCES_PAYLOAD = {
  configured: ['api_football'],
  enabled: { api_football: true },
  default_source: 'api_football',
  jobs: {
    quota: { day: '2026-10-24', used: 3, limit: 80 },
    active: { status: 'idle' },
    recent: { by_status: { done: 2, failed: 0 }, items: [] },
    next_runs: {},
  },
};

// ── P1：降级态桩（2026-10-23）─────────────────────────────────────────────
// 事故：数据库读失败时，页面把「连不上库」印成「你还没跑脚本」/「已结算 0 注」/
// 「暂无 CLV 数据…」/「24 小时无异动」⇒ **四种情况都在说谎**。
// ⇒ 降级态必须有独立分支，且必须能被**运行时**验到（不是字符串匹配）。
const CLV_DEGRADED = {
  rows: [],
  summary: { n_days: 0, n_matches: 0, mean_clv: null, insufficient: true, degraded: true },
  degraded: true,
};
const RECOMMEND_DEGRADED = {
  picks: [], picks_degraded: true, n_picks: 0,
  record: { n_settled: 0, n_hit: 0, n_miss: 0, staked: 0, returned: 0, pnl: 0,
            roi: null, insufficient: true, degraded: true },
  offer_freshness: { threshold_hours: 6, latest_snap: null, age_hours: null,
                     stale: null, arrival_ts: null, arrival_age_hours: null,
                     arrival_stale: null, degraded: true },
  pricing_note: '', calendar_note: '',
};
const ODDS_MOVE_DEGRADED = { sport: 'football', count: 0, items: [], degraded: true };
const ODDS_MOVE_OPS_DEGRADED = { window_hours: 24, n_events: 0, n_pushed: 0, n_matches: 0,
                                 max_move_pct: null, avg_move_pct: null, last_curr_ts: null,
                                 threshold_pct: null, push_configured: false, degraded: true };

// 桩模式：'normal' | 'degraded'
let stubMode = 'normal';

function payloadFor(pathname) {
  const p = String(pathname);
  if (stubMode === 'degraded') {
    if (p.indexOf('/ops/odds-move') >= 0) return ODDS_MOVE_OPS_DEGRADED;
    if (p.indexOf('/odds-move') >= 0) return ODDS_MOVE_DEGRADED;
    if (p.indexOf('/clv') >= 0) return CLV_DEGRADED;
    if (p.indexOf('/recommend') >= 0) return RECOMMEND_DEGRADED;
    if (p.indexOf('/daily-image') >= 0) return TEMP_LEAGUES_PAYLOAD;
    if (p.indexOf('/sources/status') >= 0) return SOURCES_PAYLOAD;
    if (p.indexOf('/accuracy/breakdown') >= 0) return ACCURACY_PAYLOAD;
    if (p.indexOf('/accuracy') >= 0) return ACCURACY_PENDING;
    return {};
  }
  if (p.indexOf('/clv') >= 0) return CLV_PAYLOAD;
  if (p.indexOf('/recommend') >= 0) return RECOMMEND_PAYLOAD;
  if (p.indexOf('/daily-image') >= 0) return TEMP_LEAGUES_PAYLOAD;
  if (p.indexOf('/sources/status') >= 0) return SOURCES_PAYLOAD;
  if (p.indexOf('/accuracy/breakdown') >= 0) return ACCURACY_PAYLOAD;
  if (p.indexOf('/accuracy') >= 0) return ACCURACY_PENDING;
  return {};
}

const fetchStub = (url) => {
  const u = String(url);
  // ⚠️ P1-G：未登录态 —— /me 返回 401（真实后端 require_auth 的行为）。
  if (stubMode === 'unlogged' && u.indexOf('/me') >= 0) {
    return Promise.resolve({
      status: 401, ok: false,
      json: () => Promise.resolve({}),
      text: () => Promise.resolve(''),
    });
  }
  // ⚠️ P2-K：网络故障态 —— 除 /me 外全部**请求失败**（模拟断网 / 服务不可达）。
  //    用来验证「首屏取不到数据时顶部不许写『最近同步』」。
  if (stubMode === 'failed' && u.indexOf('/me') < 0) {
    return Promise.reject(new Error('网络错误（桩）'));
  }
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

// ── 沙箱工厂 ──────────────────────────────────────────────────────────────
// P2-A3/A5 需要**第二套**沙箱：把 Date 冻结在「BJT 与 UTC 的**日期**不同」的那一刻，
// 才能证明页面写的是 BJT 口径，而不是「碰巧跑在 UTC+8 的机器上看起来对」。
// 两套沙箱各配各的 DOM 桩（makeDom()）—— 共用一份的话，B 套的 loader 会把 A 套
// 刚渲染出来的面板冲掉 ⇒ 断言读到别的沙箱的结果（空断言/假通过）。
function buildSandbox(extra, domStub) {
  const sb = Object.assign({
    console,
    fetch: fetchStub,
    document: domStub.documentStub,
    localStorage: localStorageStub,
    navigator: { userAgent: 'node-smoke' },
    location: { href: 'http://127.0.0.1/jc/', pathname: '/jc/', reload() {} },
    setTimeout, clearTimeout, setInterval, clearInterval,
    URLSearchParams,
    // ⚠️ P2-K：`api()` 的超时靠 AbortController ⇒ 沙箱必须提供它，
    //    否则「请求永远挂着 ⇒ 必须超时失败」这条根本测不了。
    AbortController,
    IntersectionObserver: class { constructor() {} observe() {} disconnect() {} unobserve() {} },
  }, extra || {});
  sb.window = sb;
  sb.globalThis = sb;
  sb.__setStubMode = (m) => { stubMode = m; };
  vm.createContext(sb);
  return sb;
}

const sandbox = buildSandbox({ P2N_BELOW_LABEL }, dom);

let loadError = null;
try {
  vm.runInContext(code, sandbox, { filename: 'dashboard-inline.js' });
} catch (e) {
  loadError = e;
}

// ── 冻结时钟的第二套沙箱（P2-A3 / P2-A5 / P2-A6 的时刻类断言）──────────────
// 冻结在 2026-10-23T16:30:05Z：
//   UTC  = 2026-10-23 16:30:05 / 日期 2026-10-23
//   BJT  = 2026-10-24 00:30:05 / 日期 2026-10-24
// ⇒ 「按 UTC 写」的代码会印出 10-23 / 16:30；「按 BJT 写」的代码印 10-24 / 00:30。
const FIXED_NOW_MS = Date.UTC(2026, 9, 23, 16, 30, 5);
class FrozenDate extends Date {
  constructor(...args) {
    if (args.length === 0) super(FIXED_NOW_MS);
    else super(...args);
  }
  static now() { return FIXED_NOW_MS; }
}
const domFixed = makeDom();
domFixed.seedLoadingPlaceholder('matchGrid', 'matchGridLoading', '正在加载今日预测…');
domFixed.seedLoadingPlaceholder('champBody', 'champBodyLoading', '正在加载冠军概率…');
domFixed.seedLoadingPlaceholder('aiSummary', 'aiSummaryLoading', '正在读取 AI 状态…');
const sandboxFixed = buildSandbox({ Date: FrozenDate }, domFixed);
let fixedLoadError = null;
try {
  vm.runInContext(code, sandboxFixed, { filename: 'dashboard-inline-frozen.js' });
} catch (e) {
  fixedLoadError = e;
}

// ── P2-A10：static/jc.html 的 API 基址推导（运行时）───────────────────────
// 事故：jc.html 把 API 写死成根路径 "/api/jc/..." ⇒ 生产（本页挂在
//   /jc/static/jc.html）请求打到**另一个服务**的根路径，拿到的是它的 401
//   ⇒ 页面**永远**显示「请先登录」（实测 2026-10-23：根 /api/jc/fixtures → 401
//   text/plain「unauthorized」来自门户；/jc/api/jc/fixtures → 401 JSON 来自本应用）。
// 这里真跑一遍 jc.html 的内联脚本，把它实际 fetch 的 URL 抓出来对。
const jcHtmlPath = path.resolve(repoRoot, argValue('--jc-html', 'static/jc.html'));
const jcHtml = fs.readFileSync(jcHtmlPath, 'utf8');
const jcBlocks = [];
const jcRe = /<script(?![^>]*\ssrc=)[^>]*>([\s\S]*?)<\/script>/g;
let jcM;
while ((jcM = jcRe.exec(jcHtml)) !== null) jcBlocks.push(jcM[1]);
const jcCode = jcBlocks[jcBlocks.length - 1] || '';

// 真跑 jc.html 的内联脚本，返回它实际请求的 URL 列表。
function runJcPage(pathname) {
  const urls = [];
  const els = new Map();
  const jcDocument = {
    getElementById(id) {
      if (!els.has(id)) els.set(id, { id, textContent: '', innerHTML: '', style: { display: '' } });
      return els.get(id);
    },
  };
  const jcSandbox = {
    console,
    Promise,
    document: jcDocument,
    fetch: (url) => {
      urls.push(String(url));
      return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve({ rows: [], sql_note: '' }) });
    },
    window: { location: { pathname } },
  };
  jcSandbox.globalThis = jcSandbox;
  vm.createContext(jcSandbox);
  try {
    vm.runInContext(jcCode, jcSandbox, { filename: 'jc-inline.js' });
  } catch (e) {
    urls.push('THREW:' + (e && e.message ? e.message : String(e)));
  }
  return { urls, doc: jcDocument };
}

const PANELS = [
  { id: 'clvPanel', must: 'CLV' },
  // ⚠️ 2026-10-23：推荐单面板必须印「数据截至」——页面只在加载时拉一次 API，
  //    用户盯着旧面板会以为「没采到」（实测：库里明明有 1.6 小时前的新鲜快照，
  //    页面却显示「暂无定价赔率快照（未采集）」⇒ 静态页新、API 旧的混合态）。
  { id: 'recommendPanel', must: '今日推荐单', must2: '数据截至' },
  { id: 'recordPanel', must: '推荐单战绩' },
];

// ── P0：模型概率口径（2026-10-23 三路审计发现的口径塌方）─────────────────
// 事故：同一场比赛（斯图加特 vs 门兴格拉德巴赫）页面上出现 4 个互相矛盾的「赢面」：
//   probs            = {h:0.6466, d:0.1405, a:0.2129}  ← 决策层（推荐单）用的
//   ml_proba         = [0.9778, 0.0081, 0.0141]         ← 卡片概率条 / edge / EV 用的
//   confidence_score = 0.793（=(p−0.25)×2 的把握度评分）  ← 卡片标成「这条的赢面」
// ⇒ 卡片让买主胜、推荐单让买客胜（实测主胜 EV +0.33 vs −0.159）。
// 这里用**运行时**断言（不是字符串匹配）：函数真的被调用一次，看它到底算出什么。
const P0_CHECKS = [
  ['p0_modelProbs_noMlFallback', 'modelProbs() 不许兜底 ml_proba（分类器原始输出）'],
  ['p0_modelProbs_trueSource', 'modelProbs() 必须认后端已校验的 model_probs'],
  ['p0_confBlock', '「这条的赢面」= 模型概率对应方向那一项（不是把握度评分）'],
  ['p0_marketLabel_source', '卡片必须标明赔率来源（ESPN vs 竞彩官方 SP）'],
  ['p0_marketLabel_noEdge', 'edge 缺失时必须明说「与庄家对比暂不可用」，不许静默消失'],
  ['p0_marketLabel_negEv', 'EV 为负时必须说「亏」，不许说「多赚」'],
  ['p0_detail_sameSource', '详情面板「模型算出的赢面」必须与卡片同源'],
  ['p0_detailTech_label', '技术面板必须把 ml_proba 标成「未融合 · 仅供排障」'],
];

// ── P1：降级态（读失败 ≠ 没有数据）────────────────────────────────────────
const P1_CHECKS = [
  ['p1_clv_degraded', 'CLV 读失败时必须印「读取失败」，不许印「暂无 CLV 数据」'],
  ['p1_recommend_degraded', '推荐单读失败时必须印「读取失败」，不许印「你还没跑脚本」'],
  ['p1_record_degraded', '战绩读失败时必须印「读取失败」，不许印「已结算 0 注」'],
  ['p1_odds_move_degraded', '异动读失败时必须印「读取失败」，不许印「24 小时无异动」'],
];

// ── P1-G：未登录首屏（假 loading ⇒ 跳登录页）───────────────────────────────
// 事故：探测到 401 时只改一句 liveText 就 return ⇒ 首屏那批
//   「正在加载今日预测…」占位符**永远停在 loading**（像在一直加载），
//   而且**不跳登录页**（与 api() 的 401 处理不一致）⇒ 用户面前是个转不出来的页面。
const P1G_CHECKS = [
  ['p1g_redirect_to_login', '未登录时必须跳登录页（不许停在看板首屏）'],
  ['p1g_no_fake_loading', '未登录时首屏占位符必须变成「未登录」，不许停在「正在加载…」'],
  ['p1g_loading_class_removed', '未登录时占位符必须摘掉 loading 动画类'],
];

// ── P2-A：前端口径（把「没有」说成「0」/ 把 UTC 说成 BJT / 暴露内部路径 …）────
// 2026-10-23 三路审计的 24 条 P2 里属于前端的两组。全部是**运行时**断言：
// 真调用函数、真渲染面板，看它到底算出什么 —— 不是字符串匹配。
// 每个 key 都必须能「把修复回退 ⇒ 必 FAIL」（否则就是空断言）。
const P2A_CHECKS = [
  // A1：pct() 把「没有值」印成「0.0%」
  ['p2a_pct_null_is_dash', 'pct(null/undefined/空串) 必须印「—」，不许印「0.0%」'],
  ['p2a_pct_real_number_ok', 'pct(真实数字) 必须照常格式化（别把 0 也当缺失）'],
  // A2：篮球缺 win_prob ⇒ 编造「客胜 100.0%」
  ['p2a_basketball_missing_winprob_unknown', '篮球缺 win_prob 必须印「还没算出赢面」，不许编「客胜 100.0%」'],
  ['p2a_basketball_winprob_ok', '篮球有 win_prob 时必须照常印主/客胜率'],
  // A4：定价快照时间戳按 UTC 打印
  ['p2a_snapshot_stamp_is_bjt', '定价快照时间戳必须按 BJT 印（不许印「UTC」）'],
  // A5：「当日额度」口径 + 前端不许拿 UTC 切片当「今天」
  ['p2a_quota_label_is_bjt_day', '「当日额度」必须写明按 BJT 自然日'],
  ['p2a_history_date_is_bjt', '「今天」= BJT 自然日，不许用 UTC 日期切片（toISOString().slice(0,10)）'],
  // A6：60 秒只刷推荐单却更新全页「最近同步」
  ['p2a_live_partial_refresh_label', '只刷了推荐单 ⇒ 顶部不许写「最近同步」'],
  ['p2a_live_full_sync_label', '首屏全量刷完 ⇒ 顶部写「最近同步」'],
  ['p2a_live_clock_is_bjt', '顶部时间戳必须按 BJT 渲染'],
  // A3：「数据截至」用浏览器本地时区
  ['p2a_stamp_is_bjt', '「数据截至」必须按 BJT 渲染（不是浏览器本地时区）'],
  // A7：AI 分析面板原始英文 + 内部 API 路径
  ['p2a_ai_analyze_no_internal_path', 'AI 分析空态不许出现 POST /api/v1/... 或原始英文'],
  // A8：信心榜空态被清空
  ['p2a_ranklist_empty_state', '信心榜空态必须有文案，不许留空'],
  // A9：临时赛事表硬编码 #ffe082
  ['p2a_temp_leagues_no_hardcoded_color', '临时赛事表不许再出现硬编码色值（#ffe082/#f5e0a1）'],
  ['p2a_jc_api_base_from_url', 'static/jc.html 的 API 基址必须按当前 URL 推导（不许写死根路径）'],
  // P2-J：用户可见面板里不许漏出 Markdown 标记（前端不渲染 ⇒ 只会原样显示）
  ['p2j_normal_panels_no_markdown_marker', '正常态面板渲染结果里不许出现 Markdown 标记（**）'],
  ['p2j_degraded_panels_no_markdown_marker', '降级态面板渲染结果里不许出现 Markdown 标记（**）'],
  // P2-K：请求不许永远挂着；首屏取不到数据时顶部不许假装「最近同步」
  ['p2k_hanging_request_times_out', '请求永远挂着时必须超时失败（不许 promise 永不 settle）'],
  ['p2k_live_not_claimed_synced_when_partial_failure', '首屏有数据没取到时，顶部不许写「最近同步」'],
  // P2-M：响应体卡住也必须超时；未知视图名不许把主区清空
  ['p2m_body_never_completes_times_out', '响应体永远不来时必须超时失败（不许只有头没有体就卡死）'],
  ['p2m_switchview_unknown_name_keeps_current_view', '未知视图名不许把当前视图隐藏（点了「每日一图」不许主区全空）'],
  // P2-N：不满足现行门槛的荐单必须逐条标注；混合口径必须写出来
  ['p2n_below_threshold_pick_is_labeled', '不满足现行门槛的荐单必须逐条标注（达标的不许带标注）'],
  ['p2n_threshold_notes_are_rendered', '面板级门槛说明与战绩「混合口径」说明必须印出来'],
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
      // ⚠️ 必须**先**抓正常态的面板 HTML：下面的降级态会**覆盖**同一批面板
      //    （同一批 DOM 元素）⇒ 抓晚了就只剩降级态，反向对照（还原事故现场 ⇒
      //    必须 FAIL）会被自己的降级态掩盖 ⇒ 冒烟变成「永远绿」。
      out.__html = ${ids}.reduce((a, id) => {
        const e = document.getElementById(id);
        a[id] = e ? String(e.innerHTML || '') : '';
        return a;
      }, {});
      // ── P2-J（2026-10-23）：用户可见面板里不许出现 Markdown 标记 ────────
      // 事故（生产浏览器验收时当场抓到）：前端**不解析 Markdown**（全仓没有
      // marked/showdown）⇒ 面板里写星号粗体只会把星号原样印给用户。
      // ⇒ 正常态渲染出来的面板里不许有星号对（两个星号连写）。
      out.p2j_normal_panels_no_markdown_marker = Object.keys(out.__html).every((id) => {
        return out.__html[id].indexOf('**') < 0;
      });
      // ── P2-N（2026-10-23）：荐单门槛口径标注 ────────────────────────────
      // 事故：推荐端阈值 2026-10-23 提到 EV ≥ 12.9%，但生成端只做 upsert、
      //   **从不清理**当日早先批次留下的行 ⇒ 库里混着旧批次的行 ⇒
      //   页面照旧把它们当「荐单」展示 ⇒ 与面板上写的门槛**自相矛盾**（说假话）。
      // ⇒ 不达标的必须**逐条标注**，达标的不许带标注（否则标注失去意义）。
      out.p2n_below_threshold_pick_is_labeled = (() => {
        const html = String((out.__html || {}).recommendPanel || '');
        if (!html) return false;
        // 桩里 2 条荐单：1 条不达标（带标注）、1 条达标（不许带标注）
        return html.split(P2N_BELOW_LABEL).length - 1 === 1;
      })();
      // 面板级说明 + 战绩口径说明必须都印出来（混合口径不许静默）
      out.p2n_threshold_notes_are_rendered = (() => {
        const rec = String((out.__html || {}).recommendPanel || '');
        const rc = String((out.__html || {}).recordPanel || '');
        return rec.indexOf('本面板有 1 条荐单不满足现行门槛') >= 0
          && rc.indexOf('战绩口径：') >= 0
          && rc.indexOf('混合口径') >= 0;
      })();
      // ── P0 口径断言（运行时调用真实函数）──────────────────────────────
      out.p0_modelProbs_noMlFallback = modelProbs({ ml_proba: [0.9778, 0.0081, 0.0141] }) === null;
      out.p0_modelProbs_trueSource = (() => {
        const q = modelProbs({ model_probs: { home: 0.6466, draw: 0.1405, away: 0.2129 } });
        return !!q && Math.abs(q[0] - 0.6466) < 1e-9 && Math.abs(q[1] - 0.1405) < 1e-9;
      })();
      out.p0_confBlock = (() => {
        const rec = {
          direction: 'home', home: '斯图加特', away: '门兴格拉德巴赫',
          confidence_score: 0.793,
          model_probs: { home: 0.6466, draw: 0.1405, away: 0.2129 },
        };
        const h = confBlock(rec);
        // 必须印出模型概率 64.7%，且**不许**印把握度评分 79.3%
        return h.indexOf('64.7%') >= 0 && h.indexOf('79.3%') < 0 && h.indexOf('模型概率') >= 0;
      })();
      out.p0_marketLabel_source = marketLabel({
        direction: 'home', home: 'A', away: 'B',
        model_probs: { home: 0.6466, draw: 0.1405, away: 0.2129 },
        market: { source: 'espn', decimal_odds: { home: 1.36 } },
      }).indexOf('ESPN') >= 0;
      out.p0_marketLabel_noEdge = marketLabel({
        direction: 'home', home: 'A', away: 'B',
        model_probs: { home: 0.6466, draw: 0.1405, away: 0.2129 },
        market: { source: 'espn', decimal_odds: { home: 1.36 } },
      }).indexOf('与庄家对比暂不可用') >= 0;
      out.p0_marketLabel_negEv = marketLabel({
        direction: 'home', home: 'A', away: 'B',
        model_probs: { home: 0.6466, draw: 0.1405, away: 0.2129 },
        market: { source: 'espn', decimal_odds: { home: 1.36 }, edge_prob: { home: 0.21 }, ev_per_unit: { home: -0.159 } },
      }).indexOf('长期亏') >= 0;
      out.p0_detail_sameSource = (() => {
        const q = dtProbs({ ml_proba: [0.9778, 0.0081, 0.0141], model_probs: { home: 0.6466, draw: 0.1405, away: 0.2129 } });
        return !!q && Math.abs(q[0] - 0.6466) < 1e-9 && Math.abs(q[1] - 0.1405) < 1e-9;
      })();
      out.p0_detailTech_label = (() => {
        const h = detailTech({ ml_proba: [0.9778, 0.0081, 0.0141] });
        return h.indexOf('未融合') >= 0 && h.indexOf('仅供排障') >= 0;
      })();
      // ── P2-K：首屏不许停在「连接中…」───────────────────────────────
      // ① 请求**永远挂着** ⇒ api() 必须**超时失败**（不许 promise 永不 settle）。
      //    ⚠️ 必须打 api()（不是 fetchTo）—— 否则「api 绕开超时」这种回退抓不到。
      //    ⚠️ 必须再套一个 2 秒硬闸：回退后 api() 会永远挂着 ⇒ 断言要能**返回 false**
      //    而不是把整个冒烟挂死。
      out.p2k_hanging_request_times_out = await (async () => {
        const _f = fetch;
        try {
          // 真实的 fetch 会响应 abort ⇒ 桩也必须响应，否则测不到超时路径。
          fetch = (u, init) => new Promise((_res, rej) => {
            if (init && init.signal) {
              init.signal.addEventListener('abort', () => rej(new Error('aborted')));
            }
          });
          const verdict = await Promise.race([
            api('/hang-forever', 60).then(() => 'RESOLVED', (e) => 'REJECTED:' + String((e && e.message) || e)),
            new Promise((r) => setTimeout(() => r('HUNG'), 2000)),
          ]);
          return verdict.indexOf('REJECTED:请求超时') >= 0;
        } finally { fetch = _f; }
      })();
      // ② 首屏有面板取不到数据 ⇒ 顶部**不许**写「最近同步」（那是假时间戳）。
      //    ⚠️ 必须**恢复正常态**：'failed' 模式会把各面板重渲染成失败文案，
      //    不还原就会污染后面 P2-A 断言的输入（实测踩过：p2a_snapshot_stamp_is_bjt 假 FAIL）。
      out.p2k_live_not_claimed_synced_when_partial_failure = await (async () => {
        __setStubMode('failed');
        let ok = false;
        try {
          await _probeAuthAndBoot();
          const t = String(document.getElementById('liveText').textContent || '');
          ok = t.indexOf('未取到') >= 0 && t.indexOf('最近同步') < 0;
        } finally {
          __setStubMode('normal');
          await _probeAuthAndBoot();
        }
        return ok;
      })();
      // ── P2-M（2026-10-23）：响应体卡住 + 未知视图名（两个真缺口）────────────
      // ① **响应头到了、响应体永远不来** ⇒ api() 必须超时失败。
      //    ⚠️ P2-K 的兜底只保护到 fetch() **返回响应头**为止（finally 里
      //    就把定时器清了）⇒ 旧实现里 await r.json() **永不 settle**
      //    ⇒ 首屏仍会**永久**停在「连接中…」（P2-K 没覆盖到这一段）。
      //    ⚠️ 桩必须**响应 abort**（真实 fetch 就是这样），否则测不到超时路径。
      out.p2m_body_never_completes_times_out = await (async () => {
        const _f = fetch;
        try {
          fetch = (u, init) => Promise.resolve({
            status: 200, ok: true,
            json: () => Promise.resolve({}),
            // 体永远不来（但响应 abort）
            text: () => new Promise((_res, rej) => {
              if (init && init.signal) {
                init.signal.addEventListener('abort', () => rej(new Error('aborted')));
              }
            }),
          });
          const verdict = await Promise.race([
            api('/body-hang', 60).then(() => 'RESOLVED', (e) => 'REJECTED:' + String((e && e.message) || e)),
            new Promise((r) => setTimeout(() => r('HUNG'), 2000)),
          ]);
          return verdict.indexOf('REJECTED:请求超时') >= 0;
        } finally { fetch = _f; }
      })();
      // ② **未知视图名**（点「每日一图」「分享」时 b.dataset.view 就是 undefined）
      //    ⇒ **不许**把当前视图的 active 去掉（旧实现 ⇒ 主区整块空白：
      //    用户在「今日推荐」页点一下「每日一图」就把整个看板点没了）。
      out.p2m_switchview_unknown_name_keeps_current_view = (() => {
        const v = document.getElementById('view-today');
        v.classList.add('view'); v.classList.add('active');
        const t = document.getElementById('pageTitle');
        t.textContent = '今日推荐';
        try { switchView(undefined); } catch (_) {}
        return v.classList.contains('active')
          && String(t.textContent || '').indexOf('数据仪表盘') < 0;
      })();
      // ── P2-A 口径断言（运行时调用真实函数 / 真渲染面板）───────────────
      // A1：pct() —— Number(null)===0 ⇒ 旧实现把「没有值」印成「0.0%」
      out.p2a_pct_null_is_dash = (() => {
        return pct(null) === '—' && pct(undefined) === '—' && pct('') === '—' && pct('  ') === '—';
      })();
      // 反向对照：真实数字（含 0）必须照常格式化 —— 否则就是把「一律当缺失」当修复
      out.p2a_pct_real_number_ok = (() => {
        return pct(0.6466) === '64.7%' && pct(0) === '0.0%' && pct(1) === '100.0%' && pct(12.34) === '12.3%';
      })();
      // A2：篮球缺 win_prob —— 旧实现 Number(null)===0 且 Number.isFinite(0)===true
      //    ⇒ 印成「主胜 0.0% / 客胜 100.0%」。
      //    ⚠️ 必须覆盖 **null**（API 的实际形状：字段在、值为 null）与空串 ——
      //    字段「整个缺失」（undefined）时旧实现 Number(undefined)=NaN，本来就没事，
      //    只测 undefined 的话这条断言会变成空断言。
      out.p2a_basketball_missing_winprob_unknown = (() => {
        const base = { sport: 'basketball', league: 'nba', home: '湖人', away: '勇士' };
        const cases = [base, Object.assign({}, base, { win_prob: null }), Object.assign({}, base, { win_prob: '' })];
        return cases.every((c) => {
          const h = probBlock(c, '—');
          return h.indexOf('还没算出赢面') >= 0 && h.indexOf('100.0%') < 0 && h.indexOf('客胜') < 0;
        });
      })();
      // 反向对照：有 win_prob 时必须照常印出主/客胜率
      out.p2a_basketball_winprob_ok = (() => {
        const h = probBlock({ sport: 'basketball', league: 'nba', home: '湖人', away: '勇士', win_prob: 0.62 }, '—');
        return h.indexOf('主胜 62.0%') >= 0 && h.indexOf('客胜 38.0%') >= 0;
      })();
      // A4：定价快照时间戳（后端给 UTC '2026-10-22 14:48:07+00'）必须换算成 BJT 再印
      out.p2a_snapshot_stamp_is_bjt = (() => {
        const h = String(document.getElementById('recommendPanel').innerHTML || '');
        return h.indexOf('2026-10-22 22:48:07') >= 0 && h.indexOf('UTC') < 0 && h.indexOf('BJT') >= 0;
      })();
      // A5：「当日额度」必须写明按 BJT 自然日
      out.p2a_quota_label_is_bjt_day = await (async () => {
        await loadSources();
        const h = String(document.getElementById('sourcesBody').innerHTML || '');
        return h.indexOf('今日额度（BJT 自然日）') >= 0 && h.indexOf('按 BJT 自然日') >= 0;
      })();
      // A6：只刷了推荐单的定时器 ⇒ 顶部不许写「最近同步」
      out.p2a_live_partial_refresh_label = await (async () => {
        await tickRecommend();
        const t = String(document.getElementById('liveText').textContent || '');
        return t.indexOf('推荐单已刷新') >= 0 && t.indexOf('最近同步') < 0;
      })();
      // A6：首屏全量刷完 ⇒ 顶部写「最近同步」
      out.p2a_live_full_sync_label = (() => {
        setLiveFullSync();
        const t = String(document.getElementById('liveText').textContent || '');
        return t.indexOf('最近同步') >= 0;
      })();
      // A7：AI 分析空态不许出现内部 API 路径 / 原始英文
      out.p2a_ai_analyze_no_internal_path = (() => {
        loadAiAnalyzeRender({ available: false, date: '2026-10-24', note: 'no analysis yet（先 POST /api/v1/jobs/ai-analyze）' });
        const ids = ['aiBanker', 'aiPerMatch', 'aiLottery'];
        return ids.every((id) => {
          const t = String(document.getElementById(id).textContent || '');
          return t.length > 0
            && t.indexOf('POST /api') < 0
            && t.indexOf('/api/v1') < 0
            && t.indexOf('no analysis yet') < 0
            && /[一-龥]/.test(t);
        });
      })();
      // A8：信心榜空态（今天全已完赛 ⇒ upcomingCards 为空）必须留文案，不许整块空白
      out.p2a_ranklist_empty_state = (() => {
        const oldAll = state.all, oldRange = state.timeRange;
        state.timeRange = 'all';
        state.all = [{ id: 'p1', league: 'epl', home: '甲队', away: '乙队', score: '2-1', stars: '3★' }];
        renderMatches();
        const h = String(document.getElementById('rankList').innerHTML || '');
        state.all = oldAll; state.timeRange = oldRange;
        return h.length > 0 && h.indexOf('暂无') >= 0;
      })();
      // A9：临时赛事表不许再写死色值（#ffe082 / #f5e0a1）
      out.p2a_temp_leagues_no_hardcoded_color = await (async () => {
        await loadTempLeagues();
        const h = String(document.getElementById('tempLeaguesBody').innerHTML || '');
        return h.length > 0 && h.indexOf('#ffe082') < 0 && h.indexOf('#f5e0a1') < 0 && h.indexOf('var(--') >= 0;
      })();
      // ── P1 降级态：把桩切到「读失败」再跑一遍面板 ────────────────────
      __setStubMode('degraded');
      await run(loadClv);
      await run(loadRecommend);
      await run(loadJcOddsMove);
      out.__degradedHtml = ['clvPanel', 'recommendPanel', 'recordPanel', 'jcMoveBody']
        .reduce((a, id) => {
          const e = document.getElementById(id);
          a[id] = e ? String(e.innerHTML || '') : '';
          return a;
        }, {});
      __setStubMode('normal');
      const dh = out.__degradedHtml;
      // ⚠️ 反向判据必须用**只有正常空态才有**的短语：
      //    降级文案本身会引用「不是「你还没跑脚本」」这类**被否定的旧文案**，
      //    直接查那些短语会误判（第一版就踩了这个坑）。
      out.p1_clv_degraded = dh.clvPanel.indexOf('读取失败') >= 0
        && dh.clvPanel.indexOf('暂无 CLV 数据') < 0;
      out.p1_recommend_degraded = dh.recommendPanel.indexOf('读取失败') >= 0
        && dh.recommendPanel.indexOf('请先跑一次国内机') < 0
        && dh.recommendPanel.indexOf('暂无推荐单。') < 0;
      out.p1_record_degraded = dh.recordPanel.indexOf('读取失败') >= 0
        && dh.recordPanel.indexOf('<label>已结算</label>') < 0;
      out.p1_odds_move_degraded = dh.jcMoveBody.indexOf('读取失败') >= 0
        && dh.jcMoveBody.indexOf('无异动事件') < 0;
      // ── P2-J：降级态文案也不许有 Markdown 标记 ────────────────────────
      //    「⚠️ 读取失败（**不是「没有数据」**）」这种写法**曾经真的在页面上**。
      out.p2j_degraded_panels_no_markdown_marker = Object.keys(dh).every((id) => {
        return dh[id].indexOf('**') < 0;
      });
      // ── P1-G：未登录首屏（假 loading ⇒ 跳登录页）────────────────────
      // 把桩切到「未登录」：/me 返回 401 ⇒ 页面必须跳登录页，
      // 且首屏那批占位符必须从「正在加载…」变成「未登录」。
      __setStubMode('unlogged');
      await _probeAuthAndBoot();
      out.p1g_redirect_to_login = String((location && location.href) || '')
        .indexOf('/jc/login') >= 0;
      out.p1g_no_fake_loading = (() => {
        const e = document.getElementById('matchGridLoading');
        const t = String((e && e.textContent) || '');
        return t.indexOf('正在加载') < 0 && t.indexOf('未登录') >= 0;
      })();
      out.p1g_loading_class_removed = (() => {
        const e = document.getElementById('matchGridLoading');
        return !!(e && e.classList && !e.classList.contains('loading'));
      })();
      __setStubMode('normal');
      return JSON.stringify(out);
    })()
  `;
  const raw = await vm.runInContext(probe, sandbox, { filename: 'dashboard-probe.js' });
  const res = JSON.parse(raw);

  // ── P2-A10：static/jc.html 的 API 基址必须按当前 URL 推导 ────────────────
  // 生产路径 /jc/static/jc.html ⇒ 必须打 /jc/api/jc/...
  // 本机路径 /static/jc.html（8077 根路径）⇒ 必须打 /api/jc/...
  const jcProd = runJcPage('/jc/static/jc.html');
  const jcLocal = runJcPage('/static/jc.html');
  res.p2a_jc_api_base_from_url =
    jcProd.urls.length === 4
    && jcProd.urls.every((u) => u.indexOf('/jc/api/jc/') === 0)
    && jcProd.urls.every((u) => u.indexOf('"/api/jc/') < 0)
    && jcLocal.urls.length === 4
    && jcLocal.urls.every((u) => u.indexOf('/api/jc/') === 0);

  let failed = false;

  // ── 冻结时钟沙箱的探针（P2-A3 / P2-A5 / P2-A6 的时刻类断言）──────────────
  // 冻结在 2026-10-23T16:30:05Z ⇒ UTC 10-23 16:30 / BJT 10-24 00:30。
  // 「用浏览器本地时区」或「用 UTC 日期切片」的写法在这里必然露馅。
  let resFixed = {};
  if (!fixedLoadError) {
    const probeFixed = `
      (async () => {
        const out = {};
        // A5：「今天」= BJT 自然日 2026-10-24（UTC 切片会给出 2026-10-23）
        out.p2a_history_date_is_bjt = (() => {
          const v = String(document.getElementById('historyDate').value || '');
          return v === '2026-10-24' && bjtToday() === '2026-10-24';
        })();
        // A3：「数据截至」必须按 BJT 渲染（00:30:05），不许是本地/UTC 时区（16:30:05）
        out.p2a_stamp_is_bjt = await (async () => {
          await loadRecommend();
          const h = String(document.getElementById('recommendPanel').innerHTML || '');
          return h.indexOf('00:30:05') >= 0 && h.indexOf('16:30:05') < 0 && h.indexOf('BJT') >= 0;
        })();
        // A6：顶部时间戳必须按 BJT 渲染（00:30），不许是本地/UTC 时区（16:30）
        out.p2a_live_clock_is_bjt = (() => {
          setLiveFullSync();
          const t = String(document.getElementById('liveText').textContent || '');
          return t.indexOf('00:30') >= 0 && t.indexOf('16:30') < 0;
        })();
        return JSON.stringify(out);
      })()
    `;
    const rawFixed = await vm.runInContext(probeFixed, sandboxFixed, { filename: 'dashboard-probe-fixed.js' });
    resFixed = JSON.parse(rawFixed);
  } else {
    console.error('FAIL: 冻结时钟沙箱加载看板脚本即抛异常：'
      + (fixedLoadError && fixedLoadError.stack ? fixedLoadError.stack : fixedLoadError));
    failed = true;
  }

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
    if (p.must2 && htmlOut.indexOf(p.must2) < 0) {
      console.error('FAIL: 面板 #' + p.id + ' 里没有「' + p.must2 + '」⇒ 面板没印出「数据截至」时刻');
      console.error('      ' + htmlOut.slice(0, 300));
      failed = true;
      continue;
    }
    console.log('OK  : #' + p.id + ' 渲染出 ' + htmlOut.length + ' 字符，含「' + p.must + '」'
      + (p.must2 ? (' + 「' + p.must2 + '」') : ''));
  }
  // ── P0 口径断言结果 ────────────────────────────────────────────────────
  for (const [key, desc] of P0_CHECKS) {
    if (res[key] !== true) {
      console.error('FAIL: ' + desc + '（' + key + ' = ' + JSON.stringify(res[key]) + '）');
      failed = true;
    } else {
      console.log('OK  : ' + desc);
    }
  }
  // ── P1 降级态断言结果 ──────────────────────────────────────────────────
  for (const [key, desc] of P1_CHECKS) {
    if (res[key] !== true) {
      console.error('FAIL: ' + desc + '（' + key + ' = ' + JSON.stringify(res[key]) + '）');
      const dump = res.__degradedHtml || {};
      for (const id of Object.keys(dump)) {
        console.error('      ' + id + ': ' + String(dump[id]).slice(0, 300));
      }
      failed = true;
    } else {
      console.log('OK  : ' + desc);
    }
  }
  // ── P1-G 未登录首屏断言结果 ────────────────────────────────────────────
  for (const [key, desc] of P1G_CHECKS) {
    if (res[key] !== true) {
      console.error('FAIL: ' + desc + '（' + key + ' = ' + JSON.stringify(res[key]) + '）');
      console.error('      location.href = ' + JSON.stringify(sandbox.location.href));
      console.error('      matchGridLoading = ' +
        JSON.stringify(String(el('matchGridLoading').textContent || '')));
      failed = true;
    } else {
      console.log('OK  : ' + desc);
    }
  }
  // ── P2-A 前端口径断言结果 ───────────────────────────────────────────────
  // 断言值来自两个探针：常规沙箱（res）与冻结时钟沙箱（resFixed）。
  // ⚠️ 输出里带**稳定 key**（[p2a_xxx]）⇒ tests/web/test_p2_ui_calibers.py
  //    可以按 key 逐条断言，且反向对照（回退修复 ⇒ 必须 FAIL）也能按 key 定位。
  for (const [key, desc] of P2A_CHECKS) {
    const val = Object.prototype.hasOwnProperty.call(res, key) ? res[key] : resFixed[key];
    if (val !== true) {
      console.error('FAIL: [' + key + '] ' + desc + '（' + key + ' = ' + JSON.stringify(val) + '）');
      if (key === 'p2a_snapshot_stamp_is_bjt' || key === 'p2a_quota_label_is_bjt_day'
        || key === 'p2a_temp_leagues_no_hardcoded_color') {
        console.error('      recommendPanel = ' + String((res.__html || {}).recommendPanel || '').slice(0, 300));
      }
      if (key === 'p2a_stamp_is_bjt' || key === 'p2a_live_clock_is_bjt' || key === 'p2a_history_date_is_bjt') {
        console.error('      冻结时钟 = 2026-10-23T16:30:05Z ⇒ BJT 应为 2026-10-24 00:30:05');
      }
      failed = true;
    } else {
      console.log('OK  : [' + key + '] ' + desc);
    }
  }
  if (failed) process.exit(1);
  console.log('看板内联 JS 运行时冒烟：3 个面板全部渲染出真内容 + P0 口径断言全过 + P1 降级态断言全过 + P1-G 未登录首屏断言全过 + P2-A 前端口径断言全过');
  // ⚠️ 必须显式退出：看板现在有 setInterval（每 60 秒自动刷新推荐单面板），
  //    node 里的定时器会**挂住事件循环**⇒ 不 exit 的话冒烟脚本永远不结束。
  process.exit(0);
}

main().catch((e) => {
  console.error('FAIL: 冒烟脚本自身抛异常：' + (e && e.stack ? e.stack : e));
  process.exit(1);
});
