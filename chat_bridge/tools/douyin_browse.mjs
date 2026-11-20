// 用 9227 上的专用浏览器替她"刷一会儿抖音/搜个视频"。
// 自带 playwright（从 DSH 的 node_modules 解析），不依赖插件内部模块。
//
//   node tools/douyin_browse.mjs --mode open
//   node tools/douyin_browse.mjs --mode search --q "示例词"
import { chromium } from 'playwright';

const CDP = process.env.DOUYIN_CDP || 'http://127.0.0.1:9227';

function arg(name, fallback = '') {
  const index = process.argv.indexOf(`--${name}`);
  return index > 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const mode = arg('mode', 'search');
const query = arg('q', '');
const limit = Number(arg('limit', '5')) || 5;

const out = (payload) => {
  process.stdout.write(JSON.stringify(payload));
  process.exit(payload.ok ? 0 : 1);
};

let browser;
try {
  browser = await chromium.connectOverCDP(CDP);
} catch (error) {
  out({ ok: false, error: `连不上浏览器（${CDP}）：${error.message}` });
}

const context = browser.contexts()[0];
let page = context.pages().find((item) => item.url().includes('douyin.com'));

try {
  if (mode === 'open') {
    if (!page) page = await context.newPage();
    await page.goto('https://www.douyin.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForTimeout(2500);
    out({ ok: true, url: page.url(), title: await page.title() });
  }

  if (!query) out({ ok: false, error: '没给搜索词' });
  page = await context.newPage();
  const url = `https://www.douyin.com/search/${encodeURIComponent(query)}?type=video`;
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(4000);

  const items = await page.evaluate((max) => {
    const cards = Array.from(document.querySelectorAll('a[href*="/video/"]'));
    const seen = new Set();
    const rows = [];
    for (const card of cards) {
      const href = card.getAttribute('href') || '';
      const text = (card.innerText || '').replace(/\s+/g, ' ').trim();
      if (!text || seen.has(href)) continue;
      seen.add(href);
      rows.push({ text: text.slice(0, 160), url: href.startsWith('http') ? href : 'https://www.douyin.com' + href });
      if (rows.length >= max) break;
    }
    return rows;
  }, limit);

  out({ ok: true, query, count: items.length, items });
} catch (error) {
  out({ ok: false, error: `${error.name}: ${error.message}` });
}
