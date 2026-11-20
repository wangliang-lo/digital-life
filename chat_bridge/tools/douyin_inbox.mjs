// 读抖音私信列表：谁发了消息、几条没读。
// 用法：node tools/douyin_inbox.mjs [--watch 300]
// 结果写到 data/douyin_inbox.json，供 dl_api 每轮注入给她。
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const CDP = process.env.DOUYIN_CDP || 'http://127.0.0.1:9227';
const OUT = 'C:/aihuman/chat_bridge/data/douyin_inbox.json';
const watchIndex = process.argv.indexOf('--watch');
const watchSeconds = watchIndex > 0 ? Number(process.argv[watchIndex + 1]) || 300 : 0;

async function scrape() {
  const browser = await chromium.connectOverCDP(CDP);
  const context = browser.contexts()[0];
  let page = context.pages().find((p) => p.url().includes('douyin.com/chat'));
  if (!page) {
    page = await context.newPage();
    await page.goto('https://www.douyin.com/chat', { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForTimeout(3500);
  }
  const items = await page.evaluate(() => {
    const seen = new Set();
    const rows = [];
    const nodes = document.querySelectorAll('[class*="conversationConversationItem"]');
    for (const node of nodes) {
      const text = (node.innerText || '').replace(/\s+/g, ' ').trim();
      if (!text) continue;
      const titleNode = node.querySelector('[class*="ConversationItemtitle"]');
      const badge = node.querySelector('[class*="UnReadCount"], [class*="digitsNumber"]');
      let name = (titleNode?.innerText || '').replace(/\s+/g, ' ').trim();
      // 列表项里名字后面常跟着"连续聊天天数 + 时间"，去掉
      name = name.replace(/\s*\d+\s*(刚刚|昨天|\d{1,2}:\d{2}|周.\d*|\d{1,2}\/\d{1,2})?.*$/, '').trim() || name;
      const key = name + '|' + text.slice(0, 40);
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({
        name,
        unread: Number((badge?.innerText || '0').replace(/\D/g, '')) || 0,
        preview: text.slice(0, 120),
      });
    }
    return rows;
  });
  const unread = items.filter((item) => item.unread > 0);
  const payload = {
    at: new Date().toISOString(),
    total: items.length,
    unreadCount: unread.length,
    unread,
    items: items.slice(0, 12),
  };
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify(payload, null, 2), 'utf8');
  return payload;
}

async function once() {
  try {
    const data = await scrape();
    console.log(`抖音私信：${data.total} 个会话，${data.unreadCount} 个有未读`);
    for (const item of data.unread.slice(0, 5)) console.log(`  ${item.name}（${item.unread}）: ${item.preview.slice(0, 50)}`);
    return true;
  } catch (error) {
    console.log('读取失败：', error.message);
    return false;
  }
}

if (watchSeconds > 0) {
  for (;;) {
    await once();
    await new Promise((resolve) => setTimeout(resolve, watchSeconds * 1000));
  }
} else {
  await once();
  process.exit(0);
}
