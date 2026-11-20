// 抖音互动动作：点赞 / 收藏 / 关注（通过 CDP 复用她正在用的那个 Edge）
//
//   node tools/douyin_actions.cjs status
//   node tools/douyin_actions.cjs like --dry-run
//   node tools/douyin_actions.cjs collect
//   node tools/douyin_actions.cjs follow
//   node tools/douyin_actions.cjs auto --interval 45 --like-chance 0.35 --follow-chance 0.05
//
// 需要环境变量 NODE_PATH 指向 DSH profile 的 node_modules（见 tools/run_actions.ps1）。
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const STATE_FILE = path.join(__dirname, '..', 'data', 'douyin_actions_state.json');
const CDP = process.env.DOUYIN_CDP || 'http://127.0.0.1:9227';
const MAX_LIKES_PER_DAY = Number(process.env.DOUYIN_MAX_LIKES || 30);
const MAX_COLLECTS_PER_DAY = Number(process.env.DOUYIN_MAX_COLLECTS || 15);
const MAX_FOLLOWS_PER_DAY = Number(process.env.DOUYIN_MAX_FOLLOWS || 5);

const SELECTORS = {
  digg: '[data-e2e="video-player-digg"]',
  collect: '[data-e2e="video-player-collect"]',
  follow: '[data-e2e="feed-follow-icon"]',
};

function loadState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch { return {}; }
}
function saveState(state) {
  fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}
function today() { return new Date().toISOString().slice(0, 10); }
function quota(state, key, max) {
  state.days = state.days || {};
  state.days[today()] = state.days[today()] || {};
  return (state.days[today()][key] || 0) < max;
}
function bump(state, key) {
  state.days[today()][key] = (state.days[today()][key] || 0) + 1;
  saveState(state);
}

async function page() {
  const browser = await chromium.connectOverCDP(CDP);
  const pages = browser.contexts().flatMap((c) => c.pages());
  const target = pages.find((p) => p.url().includes('/video/') || p.url().includes('modal_id='));
  return { browser, page: target || pages.find((p) => p.url().includes('douyin.com')) };
}

function isActive(el) {
  if (!el) return false;
  const cls = String(el.className || '');
  return el.getAttribute('aria-pressed') === 'true' || /active|selected|liked|collected/i.test(cls);
}

async function readState(p) {
  return p.evaluate((sel) => {
    const el = document.querySelector(sel);
    const pressed = el ? (el.getAttribute('aria-pressed') === 'true' || /active|selected|liked|collected/i.test(String(el.className || ''))) : null;
    const url = location.href;
    const m = /(?:video\/|modal_id=)(\d+)/.exec(url);
    return { videoId: m ? m[1] : null, present: Boolean(el), pressed };
  }, SELECTORS.digg);
}

async function clickAction(p, selector, label) {
  const el = p.locator(selector).first();
  if (await el.count() === 0) return { ok: false, error: `找不到${label}按钮` };
  if (isActive(await el.elementHandle())) {
    return { ok: true, skipped: true, reason: `已经${label}过了` };
  }
  await el.click();
  await p.waitForTimeout(800);
  return { ok: true, clicked: true };
}

async function follow(p) {
  const el = p.locator(SELECTORS.follow).first();
  if (await el.count() === 0) return { ok: false, error: '找不到关注按钮' };
  const text = (await el.innerText().catch(() => '')).trim();
  if (/已关注|互相关注/.test(text)) return { ok: true, skipped: true, reason: '已经关注了' };
  await el.click();
  await p.waitForTimeout(800);
  const after = (await el.innerText().catch(() => '')).trim();
  return { ok: true, clicked: true, after };
}

async function main() {
  const [cmd = 'status', ...rest] = process.argv.slice(2);
  const dryRun = rest.includes('--dry-run');
  const { browser, page: p } = await page();
  if (!p) { console.log(JSON.stringify({ ok: false, error: '没有找到抖音页面' })); process.exit(1); }

  const state = loadState();
  const before = await readState(p);
  let result = { ok: true, command: cmd, videoId: before.videoId };

  if (cmd === 'status') {
    result.digg = before.present ? { present: true, pressed: before.pressed } : { present: false };
    result.today = (state.days || {})[today()] || {};
    result.limits = { likes: MAX_LIKES_PER_DAY, collects: MAX_COLLECTS_PER_DAY, follows: MAX_FOLLOWS_PER_DAY };
  } else if (cmd === 'like' || cmd === 'collect' || cmd === 'follow') {
    if (dryRun) {
      result.dryRun = true;
      result.would = cmd;
    } else if (cmd === 'like' || cmd === 'collect') {
      const key = cmd === 'like' ? 'likes' : 'collects';
      const max = cmd === 'like' ? MAX_LIKES_PER_DAY : MAX_COLLECTS_PER_DAY;
      if (!quota(state, key, max)) result = { ok: false, error: `今天的${cmd}额度用完了` };
      else {
        result = { ...result, ...(await clickAction(p, cmd === 'like' ? SELECTORS.digg : SELECTORS.collect, cmd === 'like' ? '点赞' : '收藏')) };
        if (result.clicked) bump(state, key);
      }
    } else {
      if (!quota(state, 'follows', MAX_FOLLOWS_PER_DAY)) result = { ok: false, error: '今天的关注额度用完了' };
      else {
        result = { ...result, ...(await follow(p)) };
        if (result.clicked) bump(state, 'follows');
      }
    }
  } else if (cmd === 'auto') {
    result = { ok: false, error: 'auto 模式已移除，请用计划任务调用 like/collect/follow' };
  } else {
    result = { ok: false, error: `未知动作：${cmd}` };
  }

  console.log(JSON.stringify(result, null, 2));
  process.exit(result.ok ? 0 : 2);
}

main().catch((e) => { console.error(JSON.stringify({ ok: false, error: e.message })); process.exit(1); });
