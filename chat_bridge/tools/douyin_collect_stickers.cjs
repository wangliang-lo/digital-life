// 在抖音私信里收集对方发来的表情/贴纸，存到 data/sticker_inbox/
//   node tools/douyin_collect_stickers.cjs
//   node tools/douyin_collect_stickers.cjs --max-minutes 180
// 当对方在私信里发「结束」时自动停止。
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { chromium } = require('playwright');

const CDP = process.env.DOUYIN_CDP || 'http://127.0.0.1:9227';
const INBOX = path.join(__dirname, '..', 'data', 'sticker_inbox');
const STOP_TEXT = process.env.DOUYIN_STOP_TEXT || '结束';
const COLLECT_NAME = process.env.DOUYIN_COLLECT_NAME || '';
const INTERVAL = Number(process.env.DOUYIN_COLLECT_INTERVAL || 2500);
const MAX_MINUTES = Number(process.env.DOUYIN_COLLECT_MINUTES || 240);

fs.mkdirSync(INBOX, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function save(data) {
  const hash = crypto.createHash('sha1').update(data).digest('hex').slice(0, 16);
  const file = path.join(INBOX, hash + '.png');
  if (fs.existsSync(file)) return null;
  fs.writeFileSync(file, data);
  return file;
}

async function ensureConversation(page, name) {
  if (!name) return true;
  const row = page.getByText(name, { exact: true }).first();
  if (await row.count() === 0) return false;
  await row.click({ force: true }).catch(() => {});
  await sleep(1200);
  return true;
}

async function collectOnce(page, name) {
  await ensureConversation(page, name);
  // 给每条消息里的表情元素打个临时标记
  const marks = await page.evaluate(() => {
    const out = [];
    const boxes = [...document.querySelectorAll('[class*="MessageItemEmoji"], [data-e2e="message-emoji"], img[data-e2e="inline-emoji"]')];
    boxes.forEach((el, index) => {
      el.setAttribute('data-dl-collect', String(index));
      const r = el.getBoundingClientRect();
      out.push({ index, visible: r.width > 10 && r.height > 10, y: Math.round(r.y) });
    });
    return out;
  });

  const saved = [];
  for (const mark of marks) {
    const loc = page.locator(`[data-dl-collect="${mark.index}"]`).first();
    try {
      await loc.scrollIntoViewIfNeeded({ timeout: 1500 });
      const shot = await loc.screenshot({ timeout: 4000 });
      const file = save(shot);
      if (file) saved.push(path.basename(file));
    } catch {
      /* 被遮挡或不可见就跳过，下一轮再来 */
    }
  }
  return saved;
}

async function checkStop(page) {
  return page.evaluate((stopText) => {
    const rows = [...document.querySelectorAll('[class*="messageMessageBoxmessageBox"]')].slice(-6);
    return rows.some((row) => {
      const text = (row.innerText || '').trim();
      return text.includes(stopText) && !row.querySelector('[class*="MessageItemEmoji"]');
    });
  }, STOP_TEXT);
}

(async () => {
  const started = Date.now();
  const browser = await chromium.connectOverCDP(CDP);
  const page = browser.contexts().flatMap((c) => c.pages()).find((p) => p.url().includes('douyin.com'));
  if (!page) { console.log('没有抖音页面'); process.exit(2); }

  console.log(JSON.stringify({ ok: true, startedAt: new Date().toISOString(), stopText: STOP_TEXT }));
  while (Date.now() - started < MAX_MINUTES * 60_000) {
    try {
      const saved = await collectOnce(page, COLLECT_NAME);
      if (saved.length) console.log(JSON.stringify({ at: new Date().toISOString(), saved }));
      if (await checkStop(page)) {
        console.log(JSON.stringify({ at: new Date().toISOString(), stopped: '对方说结束' }));
        break;
      }
    } catch (e) {
      console.error('collect:', e.message);
    }
    await sleep(INTERVAL);
  }
  console.log(JSON.stringify({ ok: true, finishedAt: new Date().toISOString(), files: fs.readdirSync(INBOX).length }));
  process.exit(0);
})().catch((e) => { console.error(JSON.stringify({ ok: false, error: e.message })); process.exit(1); });
