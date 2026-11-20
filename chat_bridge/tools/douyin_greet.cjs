// 给许可名单里的每位好友发一个打招呼表情。
//   node tools/douyin_greet.cjs --dry-run     # 只列出会发给谁
//   node tools/douyin_greet.cjs               # 真的发（每位间隔 20-35 秒）
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const CDP = process.env.DOUYIN_CDP || 'http://127.0.0.1:9227';
const POLICY = path.join(process.env.USERPROFILE || process.env.HOME, '.dsh', 'profiles', 'desktop', 'node_modules', 'dsh-douyin-browser-local', 'state', 'chat-policy.json');
const STATE_FILE = path.join(__dirname, '..', 'data', 'greet-state.json');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function loadContacts() {
  try {
    const policy = JSON.parse(fs.readFileSync(POLICY, 'utf8'));
    return (policy.allowlist || []).filter((item) => item && item.name && !item.isGroup);
  } catch (e) {
    console.error('读取许可名单失败:', e.message);
    return [];
  }
}

async function openConversation(page, name) {
  const row = page.getByText(name, { exact: true }).first();
  if (await row.count() === 0) return false;
  await row.click({ force: true });
  await sleep(1600);
  return true;
}

async function sendSticker(page) {
  const actions = page.locator('.messageMsgInputinputAction');
  const count = await actions.count();
  if (!count) throw new Error('找不到输入框工具按钮');
  await actions.nth(Math.min(1, count - 1)).click({ force: true });
  await sleep(900);
  const imgs = page.locator('img');
  const total = await imgs.count();
  const candidates = [];
  for (let i = 0; i < total && candidates.length < 60; i += 1) {
    const box = await imgs.nth(i).boundingBox().catch(() => null);
    if (box && box.width >= 24 && box.height >= 24 && box.y > 260) candidates.push(i);
  }
  if (!candidates.length) throw new Error('表情面板没有可选表情');
  const pick = candidates[Math.floor(Math.random() * candidates.length)];
  await imgs.nth(pick).click({ force: true });
  await sleep(900);
  await page.keyboard.press('Enter').catch(() => {});
  await sleep(600);
  return pick;
}

(async () => {
  const dryRun = process.argv.includes('--dry-run');
  const contacts = loadContacts();
  if (!contacts.length) { console.log(JSON.stringify({ ok: false, error: '许可名单为空' })); process.exit(2); }

  const browser = await chromium.connectOverCDP(CDP);
  const page = browser.contexts().flatMap((c) => c.pages()).find((p) => p.url().includes('douyin.com'));
  if (!page) { console.log(JSON.stringify({ ok: false, error: '没有抖音页面' })); process.exit(2); }

  const results = [];
  for (const contact of contacts) {
    const opened = await openConversation(page, contact.name);
    if (!opened) { results.push({ name: contact.name, sent: false, error: '会话列表里没找到' }); continue; }
    if (dryRun) { results.push({ name: contact.name, sent: false, dryRun: true }); continue; }
    try {
      const index = await sendSticker(page);
      results.push({ name: contact.name, sent: true, index });
      console.log(JSON.stringify({ name: contact.name, sent: true }));
      await sleep(20000 + Math.random() * 15000);
    } catch (e) {
      results.push({ name: contact.name, sent: false, error: e.message });
    }
  }
  if (!dryRun) {
    try {
      const prev = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
      fs.writeFileSync(STATE_FILE, JSON.stringify({ ...prev, lastGreetAt: new Date().toISOString() }, null, 2));
    } catch { /* ignore */ }
  }
  console.log(JSON.stringify({ ok: true, dryRun, results }, null, 2));
  process.exit(0);
})().catch((e) => { console.error(JSON.stringify({ ok: false, error: e.message })); process.exit(1); });
