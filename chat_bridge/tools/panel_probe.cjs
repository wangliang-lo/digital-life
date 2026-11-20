// 记录表情面板出现时的 DOM 结构，用于校准选择器。
//   node tools/panel_probe.cjs
const fs = require('fs');
const { chromium } = require('playwright');

const OUT = 'C:/aihuman/chat_bridge/data/panel_probe.jsonl';

(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:9227');
  const page = browser.contexts().flatMap((c) => c.pages()).find((p) => p.url().includes('douyin.com'));
  if (!page) process.exit(2);
  let last = -1;
  for (let i = 0; i < 600; i += 1) {
    try {
      const info = await page.evaluate(() => {
        const visible = [...document.querySelectorAll('img')].filter((img) => {
          const r = img.getBoundingClientRect();
          return r.width > 15 && r.height > 15 && r.y > 200 && r.y < innerHeight;
        });
        const box = visible[0]?.closest('div[class]') || null;
        let root = box;
        for (let k = 0; k < 5 && root?.parentElement; k += 1) root = root.parentElement;
        return {
          count: visible.length,
          sample: visible.slice(0, 3).map((img) => ({ src: (img.currentSrc || '').slice(0, 60), y: Math.round(img.getBoundingClientRect().y) })),
          rootClass: root ? String(root.className).slice(0, 80) : null,
          cls: visible.slice(0, 3).map((img) => String(img.className || '').slice(0, 40)),
        };
      });
      if (info.count !== last && info.count > 6) {
        last = info.count;
        fs.appendFileSync(OUT, JSON.stringify({ at: new Date().toISOString(), ...info }) + '\n');
      }
    } catch { /* ignore */ }
    await new Promise((r) => setTimeout(r, 1000));
  }
  process.exit(0);
})();
