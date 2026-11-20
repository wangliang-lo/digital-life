// 夜间睡眠：关掉抖音浏览器里的所有抖音页面，并写入离线标记。
//   NODE_PATH=%USERPROFILE%\.dsh\profiles\desktop\node_modules node tools\sleep_offline.cjs
// 只碰 9227 这个专用调试实例，不碰用户自己的其他浏览器窗口。
const fs = require('node:fs');
const path = require('node:path');
function loadPlaywright() {
  try {
    return require('playwright');
  } catch (_) {
    const home = process.env.USERPROFILE || process.env.HOME || '';
    // 不依赖 NODE_PATH：直接指向 DSH profile 下的 node_modules
    return require(path.join(home, '.dsh', 'profiles', 'desktop', 'node_modules', 'playwright'));
  }
}

const { chromium } = loadPlaywright();

const MARKER = path.join(__dirname, '..', 'data', 'asleep.json');
const SLEEP_STATE = path.join(__dirname, '..', 'data', 'sleep_state.json');

function nextWakeUp(now) {
  const wake = new Date(now);
  wake.setHours(7, 0, 0, 0);
  if (wake <= now) wake.setDate(wake.getDate() + 1);
  return wake;
}

function writeMarker(payload) {
  fs.mkdirSync(path.dirname(MARKER), { recursive: true });
  fs.writeFileSync(MARKER, JSON.stringify(payload, null, 2), 'utf8');
}

(async () => {
  const now = new Date();
  let chosen = null;
  try {
    const state = JSON.parse(fs.readFileSync(SLEEP_STATE, 'utf8'));
    if (state && state.asleep && state.until) chosen = state;
  } catch (_) { /* ignore */ }
  const marker = {
    asleep: true,
    since: chosen?.since || now.toISOString(),
    until: chosen?.until || nextWakeUp(now).toISOString(),
    reason: chosen?.reason || 'night_sleep_22:00',
    douyin_pages_closed: 0,
    browser: 'unknown',
  };

  try {
    const browser = await chromium.connectOverCDP(process.env.DOUYIN_CDP || 'http://127.0.0.1:9227', { timeout: 10000 });
    marker.browser = 'running';
    const pages = browser.contexts().flatMap((c) => c.pages());
    let closed = 0;
    let remaining = 0;
    for (const page of pages) {
      const url = page.url();
      if (url.includes('douyin.com') || url.includes('iesdouyin.com')) {
        await page.close().catch(() => {});
        closed += 1;
      }
    }
    const left = browser.contexts().flatMap((c) => c.pages())
      .filter((p) => p.url().includes('douyin.com') || p.url().includes('iesdouyin.com'));
    remaining = left.length;
    marker.douyin_pages_closed = closed;
    marker.douyin_pages_remaining = remaining;
    // 整个浏览器一起关，避免后台标签继续跑
    const session = await browser.newBrowserCDPSession().catch(() => null);
    if (session) await session.send('Browser.close').catch(() => {});
    marker.browser = 'closed';
  } catch (error) {
    // 浏览器本来就关了：正常情况，仍写离线标记
    marker.browser = 'not_running';
    marker.note = error.message;
  }

  marker.updated_at = new Date().toISOString();
  writeMarker(marker);
  console.log(JSON.stringify({ ok: true, ...marker }));
})().catch((error) => {
  try {
    writeMarker({ asleep: true, error: error.message, updated_at: new Date().toISOString() });
  } catch (_) { /* 忽略 */ }
  console.log(JSON.stringify({ ok: false, error: error.message }));
  process.exit(1);
});
