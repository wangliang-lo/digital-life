// 单条作品页放完后：有多个抖音标签页就关掉这一页，否则回到推荐流。
//   node tools/guard_nav.cjs
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.connectOverCDP(process.env.DOUYIN_CDP || 'http://127.0.0.1:9227');
  const pages = browser.contexts().flatMap((c) => c.pages()).filter((p) => p.url().includes('douyin.com'));
  const videoPages = [];
  for (const page of pages) {
    if (!page.url().includes('/video/')) continue;
    const ended = await page.evaluate(() => {
      const v = document.querySelector('video');
      if (!v) return false;
      const dur = Number(v.duration);
      if (!Number.isFinite(dur) || dur <= 0) return false;
      return v.paused && v.currentTime >= dur - 0.8;
    }).catch(() => false);
    if (ended) videoPages.push(page);
  }
  if (!videoPages.length) {
    console.log(JSON.stringify({ ok: true, action: 'none', pages: pages.length }));
    process.exit(0);
  }
  const target = videoPages[0];
  if (pages.length > 1) {
    await target.close().catch(() => {});
    console.log(JSON.stringify({ ok: true, action: 'closed', pages: pages.length }));
  } else {
    await target.goto('https://www.douyin.com/?recommend=1', { waitUntil: 'domcontentloaded' });
    await target.waitForTimeout(2000);
    console.log(JSON.stringify({ ok: true, action: 'reloaded_feed', url: target.url() }));
  }
  process.exit(0);
})().catch((error) => { console.log(JSON.stringify({ ok: false, error: error.message })); process.exit(1); });
