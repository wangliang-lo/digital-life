// 按兴趣主题决定点赞/收藏/关注（不再掷骰子）。
//
//   node tools/douyin_auto.cjs --dry-run          # 只打印判断，不动手
//   node tools/douyin_auto.cjs --interval 6       # 后台循环
//
// 兴趣来源（按优先级合并）：
//   1. ~/.dsh/douyin-browser/memory/persona/state.json 里的 interests（她自己的观看结论）
//   2. data/interest_topics.json（可手编，权重会随互动自增）
//
// 判断规则：
//   - 标题/话题里命中「喜欢」主题，权重相加；
//   - 分数 ≥2 点赞，≥4 收藏，≥6 才考虑转发（转发走插件自己的卡片链路，不在这里做）；
//   - 同一作者被她点赞 ≥2 次，才关注；
//   - 命中「不喜欢」的主题则直接不动。
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const CDP = process.env.DOUYIN_CDP || 'http://127.0.0.1:9227';
const STATE_FILE = path.join(__dirname, '..', 'data', 'douyin_actions_state.json');
const INTERESTS_FILE = path.join(__dirname, '..', 'data', 'interest_topics.json');
const PERSONA_STATE = path.join(process.env.USERPROFILE || process.env.HOME, '.dsh', 'douyin-browser', 'memory', 'persona', 'state.json');
const MIN_DWELL_MS = Number(process.env.DOUYIN_MIN_DWELL_MS || 12000);
const MAX_LIKES = Number(process.env.DOUYIN_MAX_LIKES || 20);
const MAX_COLLECTS = Number(process.env.DOUYIN_MAX_COLLECTS || 10);
const MAX_FOLLOWS = Number(process.env.DOUYIN_MAX_FOLLOWS || 3);

const SEL = {
  digg: '[data-e2e="video-player-digg"]',
  collect: '[data-e2e="video-player-collect"]',
  follow: '[data-e2e="feed-follow-icon"]',
};

function loadJson(file, fallback) { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; } }
function saveJson(file, value) { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, JSON.stringify(value, null, 2)); }
const today = () => new Date().toISOString().slice(0, 10);

function loadInterests() {
  const map = new Map();
  const persona = loadJson(PERSONA_STATE, {});
  for (const item of persona.interests || []) {
    if (item && item.topic && item.signal === 'like') map.set(item.topic, Math.max(map.get(item.topic) || 0, 1.5));
  }
  const local = loadJson(INTERESTS_FILE, { topics: [] });
  for (const item of local.topics || []) {
    if (item && item.topic) map.set(item.topic, Number(item.weight) || 1);
  }
  return map;
}

function saveInterests(map) {
  saveJson(INTERESTS_FILE, { topics: [...map.entries()].map(([topic, weight]) => ({ topic, weight: Number(weight.toFixed(2)) })) });
}

function scoreTopics(text, interests) {
  let score = 0;
  const hits = [];
  for (const [topic, weight] of interests) {
    if (text.includes(topic)) { score += weight; hits.push(topic); }
  }
  return { score, hits };
}

async function getVideoInfo(p) {
  return p.evaluate(() => {
    const m = /(?:video\/|modal_id=)(\d+)/.exec(location.href);
    const title = (document.querySelector('[data-e2e="video-desc"]')?.innerText ||
      document.querySelector('h1')?.innerText || document.title || '').trim().slice(0, 200);
    const author = (document.querySelector('[data-e2e="video-author-name"], [class*="authorName"]')?.innerText || '').trim().slice(0, 40);
    return { videoId: m ? m[1] : null, title, author };
  });
}

async function clickIfNotActive(p, selector, label) {
  const loc = p.locator(selector).first();
  if (await loc.count() === 0) return { ok: false, error: `no ${label}` };
  const pressed = await loc.evaluate((el) => el.getAttribute('aria-pressed') === 'true' || /active|selected|liked|collected/i.test(String(el.className || ''))).catch(() => false);
  if (pressed) return { ok: true, skipped: true };
  await loc.click();
  await p.waitForTimeout(700);
  return { ok: true, clicked: true };
}

async function tick(state, seen, interests, dryRun) {
  const browser = await chromium.connectOverCDP(CDP);
  const p = browser.contexts().flatMap((c) => c.pages()).find((x) => x.url().includes('/video/') || x.url().includes('modal_id='));
  if (!p) return null;
  const info = await getVideoInfo(p);
  if (!info.videoId) return null;

  if (seen.videoId !== info.videoId) { seen.videoId = info.videoId; seen.since = Date.now(); return null; }
  if (!seen.since) seen.since = Date.now();
  if (Date.now() - seen.since < MIN_DWELL_MS) return null;
  if (seen.acted && seen.acted[info.videoId]) return null;

  const { score, hits } = scoreTopics(info.title, interests);
  const decision = { videoId: info.videoId, title: info.title.slice(0, 60), author: info.author, score: Number(score.toFixed(1)), hits };
  if (score < 2) return { ...decision, action: 'none' };

  const day = (state.days = state.days || {}, state.days[today()] = state.days[today()] || {});
  const result = { ...decision, action: 'like' };
  if (!dryRun && (day.likes || 0) < MAX_LIKES) {
    const r = await clickIfNotActive(p, SEL.digg, 'like');
    if (r.clicked) {
      day.likes = (day.likes || 0) + 1;
      state.authors = state.authors || {};
      state.authors[info.author] = (state.authors[info.author] || 0) + 1;
      for (const hit of hits) interests.set(hit, Math.min(4, (interests.get(hit) || 1) + 0.3));
      saveJson(INTERESTS_FILE, { topics: [...interests.entries()].map(([topic, weight]) => ({ topic, weight: Number(weight.toFixed(2)) })) });
    }
  }

  if (score >= 4 && !dryRun && (day.collects || 0) < MAX_COLLECTS) {
    const r = await clickIfNotActive(p, SEL.collect, 'collect');
    if (r.clicked) { day.collects = (day.collects || 0) + 1; result.action = 'like+collect'; }
  }

  if (!dryRun && (day.follows || 0) < MAX_FOLLOWS && info.author && (state.authors?.[info.author] || 0) >= 2) {
    const loc = p.locator(SEL.follow).first();
    if (await loc.count()) {
      const text = (await loc.innerText().catch(() => '')).trim();
      if (!/已关注|互相关注/.test(text)) {
        await loc.click();
        await p.waitForTimeout(700);
        day.follows = (day.follows || 0) + 1;
        result.action += '+follow';
      }
    }
  }

  seen.acted = seen.acted || {};
  seen.acted[info.videoId] = result.action;
  if (!dryRun) saveJson(STATE_FILE, state);
  return result;
}

async function main() {
  const args = process.argv.slice(2);
  const dryRun = args.includes('--dry-run');
  const once = args.includes('--once');
  const idx = args.indexOf('--interval');
  const interval = (idx >= 0 ? Number(args[idx + 1]) : 6) * 1000;
  const state = loadJson(STATE_FILE, {});
  const interests = loadInterests();
  const seen = { videoId: null, since: Date.now(), acted: {} };
  let remaining = dryRun ? 2 : (once ? 1 : Number.POSITIVE_INFINITY);
  while (remaining-- > 0) {
    try {
      const out = await tick(state, seen, interests, dryRun);
      if (out) console.log(JSON.stringify(out));
    } catch (e) { console.error('tick:', e.message); }
    if (remaining > 0) await new Promise((r) => setTimeout(r, interval));
  }
  process.exit(0);
}

main();
