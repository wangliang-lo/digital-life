// 打开一条抖音作品链接，采 3 帧存成图片，输出时长和帧路径。
//   node tools/watch_video_url.cjs --url "https://www.douyin.com/video/xxx" --out PROJECT_ROOT\chat_bridge\data\share_frames
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

function arg(name, fallback = '') {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

(async () => {
  const url = arg('url');
  const outDir = arg('out', 'C:/aihuman/chat_bridge/data/share_frames');
  if (!/^https?:\/\/(www\.)?(douyin\.com|v\.douyin\.com)\//.test(url)) {
    console.log(JSON.stringify({ ok: false, error: '不是抖音链接' }));
    process.exit(0);
  }
  fs.mkdirSync(outDir, { recursive: true });
  const browser = await chromium.connectOverCDP(process.env.DOUYIN_CDP || 'http://127.0.0.1:9227');
  const context = browser.contexts()[0];
  const page = await context.newPage();
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForTimeout(6000);
    const duration = await page.evaluate(() => {
      const v = document.querySelector('video');
      return v && Number.isFinite(v.duration) ? v.duration : 0;
    });
    const targets = [0.1, 0.5, 0.9].map((r) => Math.max(1, Math.min(duration - 1, duration * r)));
    const frames = [];
    for (let i = 0; i < targets.length; i += 1) {
      await page.evaluate((t) => {
        const v = document.querySelector('video');
        if (v) { v.currentTime = t; v.pause(); }
      }, targets[i]);
      await page.waitForTimeout(1200);
      const file = path.join(outDir, `f${i}.jpg`);
      await page.locator('video').first().screenshot({ path: file, timeout: 15000 }).catch(() => null);
      if (fs.existsSync(file)) frames.push(file);
    }
    console.log(JSON.stringify({ ok: true, duration: Math.round(duration), frames, url }));
  } catch (error) {
    console.log(JSON.stringify({ ok: false, error: error.message.slice(0, 120), url }));
  } finally {
    await page.close().catch(() => {});
  }
  process.exit(0);
})();
