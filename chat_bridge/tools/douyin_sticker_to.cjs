// 给指定好友发一个表情：优先「我的」收藏页，其次默认表情面板，最后上传本地图片。
// 作为模块：require('./douyin_sticker_to.cjs') → sendStickerTo(name)
// 命令行：node tools/douyin_sticker_to.cjs --name "爸爸"
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const CDP = process.env.DOUYIN_CDP || 'http://127.0.0.1:9227';
const LIBRARY = 'C:/aihuman/chat_bridge/stickers/files';

const normalize = (value) => String(value || '').replace(/\s+/g, '');

async function sendStickerTo(name) {
  const browser = await chromium.connectOverCDP(CDP);
  const page = browser.contexts().flatMap((c) => c.pages()).find((p) => p.url().includes('douyin.com'));
  if (!page) return { ok: false, error: '没有抖音页面' };

  const check = () => page.evaluate(() => {
    const input = document.querySelector('[contenteditable="true"], [data-e2e="msg-input"]');
    if (!input) return '';
    // 2026-11-04：以前读整个右栏（含消息列表），只要历史里出现过这个名字就算"切到了"，
    // 结果把表情发给了正开着的另一个会话（熟人B）。现在只读会话头部那个名字。
    const header = document.querySelector('[class*="RightPanelHeaderconvHeader"]')
      || document.querySelector('[class*="RightPanelHeader"]');
    if (header) return (header.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40);
    const wrapper = input.closest('.componentsRightPanelwrapper');
    return (wrapper?.innerText || '').trim().slice(0, 40).replace(/\n/g, '|');
  });

  if (await page.locator('.messageMsgInputinputAction').count() === 0) {
    // 2026-11-04：点导航上的"消息"经常点不动（SPA 路由），直接进 /chat 更稳。
    const entry = page.getByText('消息', { exact: true }).first();
    if (await entry.count()) {
      await entry.click({ force: true }).catch(() => {});
      await page.waitForTimeout(1500);
    }
    if (await page.locator('.messageMsgInputinputAction').count() === 0) {
      await page.goto('https://www.douyin.com/chat', { waitUntil: 'domcontentloaded', timeout: 45000 }).catch(() => {});
      await page.waitForTimeout(2500);
    }
  }

  let snapshot = await check();
  for (let attempt = 0; attempt < 4 && name && !normalize(snapshot).includes(normalize(name)); attempt += 1) {
    const rows = page.getByText(name);
    const count = await rows.count();
    if (count > 0) {
      await rows.nth(0).click({ force: true }).catch(() => {});
      await page.waitForTimeout(1200);
      snapshot = await check();
      if (normalize(snapshot).includes(normalize(name))) break;
    }
    await page.evaluate(() => {
      const candidates = [...document.querySelectorAll('div')].filter((el) => el.scrollHeight > el.clientHeight + 40 && el.clientHeight > 120);
      for (const el of candidates) el.scrollTop = Math.min(el.scrollHeight, el.scrollTop + 260);
    });
    await page.waitForTimeout(600);
    snapshot = await check();
  }
  const deadline = Date.now() + 25000;
  while (name && !normalize(snapshot).includes(normalize(name)) && Date.now() < deadline) {
    await page.waitForTimeout(1000);
    snapshot = await check();
  }
  if (name && !normalize(snapshot).includes(normalize(name))) {
    return { ok: false, error: '没切到 ' + name + ' 的会话（当前：' + snapshot.slice(0, 24) + '）' };
  }

  const openPanel = async () => {
    const actions = page.locator('.messageMsgInputinputAction');
    const n = await actions.count();
    if (!n) return false;
    await actions.nth(Math.min(1, n - 1)).click({ force: true });
    await page.waitForTimeout(1000);
    return true;
  };
  const collect = async () => {
    // 表情面板的每一项是 emojiEmojiItemimgBox，尺寸常小于 24px，不能按大小筛掉
    const panelItems = page.locator('[class*="emojiEmojiItem"]');
    const count = await panelItems.count();
    const list = [];
    for (let i = 0; i < count && list.length < 60; i += 1) {
      const box = await panelItems.nth(i).boundingBox().catch(() => null);
      if (box && box.width >= 10 && box.height >= 10 && box.y > 200) list.push(i);
    }
    if (list.length) return list;
    const imgs = page.locator('img');
    const total = await imgs.count();
    for (let i = 0; i < total && list.length < 40; i += 1) {
      const box = await imgs.nth(i).boundingBox().catch(() => null);
      if (box && box.width >= 14 && box.height >= 14 && box.y > 260) list.push(i);
    }
    return list;
  };

  if (!await openPanel()) return { ok: false, error: '找不到表情按钮' };

  // 先切收藏（爱心）页
  for (const selector of ['[class*="collect"]', '[class*="favorite"]', '[class*="heart"]', '[title*="收藏"]', '[aria-label*="收藏"]']) {
    const tab = page.locator(selector).first();
    if (await tab.count()) {
      const box = await tab.boundingBox().catch(() => null);
      if (box && box.y > 200) { await tab.click({ force: true }).catch(() => {}); await page.waitForTimeout(600); break; }
    }
  }
  const mine = page.getByText('我的', { exact: true }).first();
  if (await mine.count()) { await mine.click({ force: true }).catch(() => {}); await page.waitForTimeout(600); }

  let candidates = await collect();
  if (!candidates.length) {
    // 收藏页没东西：关掉再开，用默认表情
    await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(400);
    if (await openPanel()) candidates = await collect();
  }

  const countRows = () => page.evaluate(() => document.querySelectorAll('[class*="messageMessageBoxmessageBox"]').length);
  // 2026-11-04：以前从 stickers/files 里随机抓一张就上传——库里混着截图/视频画面，
  // 结果把"分享视频截图"当表情包发了。现在只拿有正经标签、不是截图的那批。
  const localFiles = (() => {
    try {
      const raw = fs.readFileSync('C:/aihuman/chat_bridge/stickers/index.json', 'utf8');
      const items = (JSON.parse(raw).stickers || []);
      return items
        .filter((item) => {
          const tags = (item.tags || []).map(String);
          const note = String(item.note || '');
          if (!tags.length || tags.every((t) => ['未分类', '示例', ''].includes(t))) return false;
          if (/截图|视频|画面|分享|链接/.test(note)) return false;
          return true;
        })
        .map((item) => String(item.file || '').replace(/^files\//, ''))
        .filter((name) => name && fs.existsSync(path.join(LIBRARY, name)));
    } catch {
      return [];
    }
  })();
  const fileInput = page.locator('.MsgInputFileUpload input[type="file"], input[type="file"]').first();
  if (localFiles.length && await fileInput.count()) {
    // 优先上传她自己收集的表情图片，并核对会话里真的多了一条
    const before = await countRows();
    const pickFile = localFiles[Math.floor(Math.random() * localFiles.length)];
    await fileInput.setInputFiles(path.join(LIBRARY, pickFile));
    await page.waitForTimeout(2200);
    const sendBtn = page.locator('[class*="sendBtn"], [data-e2e="send-btn"], button:has-text("发送")').first();
    if (await sendBtn.count()) await sendBtn.click({ force: true }).catch(() => {});
    await page.keyboard.press('Enter').catch(() => {});
    await page.waitForTimeout(2000);
    if (await countRows() > before) return { ok: true, via: 'file', file: pickFile, verified: true };
  }

  if (candidates.length) {
    const pick = candidates[Math.floor(Math.random() * candidates.length)];
    const countOutgoing = () => page.evaluate(() => [...document.querySelectorAll('[class*="MessageItemEmoji"]')].filter((el) => {
      let node = el;
      for (let i = 0; i < 6 && node; i += 1) {
        const cls = String(node.className || '');
        if (/isFromMe|FromMe/i.test(cls) || node.getAttribute('data-direction') === 'outgoing') return true;
        node = node.parentElement;
      }
      return false;
    }).length);
    const before = await countOutgoing();
    const panelItems = page.locator('[class*="emojiEmojiItem"]');
    if (await panelItems.count() > pick) {
      await panelItems.nth(pick).click({ force: true });
    } else {
      await page.locator('img').nth(pick).click({ force: true });
    }
    await page.waitForTimeout(1200);
    await page.keyboard.press('Enter').catch(() => {});
    await page.waitForTimeout(1500);
    if (await countOutgoing() > before) return { ok: true, via: 'panel', index: pick, verified: 'dom-count' };
    // 会话是虚拟列表，刚发出的消息可能还没渲染；面板关闭即视为已发出
    if (await page.locator('[class*="emojiEmojiItem"]').count() === 0) {
      return { ok: true, via: 'panel', index: pick, verified: 'panel-closed' };
    }
  }

  return { ok: false, error: '上传和面板都没能发出新消息' };
}

module.exports = { sendStickerTo };

if (require.main === module) {
  const idx = process.argv.indexOf('--name');
  const name = idx >= 0 && process.argv[idx + 1] ? process.argv[idx + 1] : '';
  sendStickerTo(name)
    .then((result) => { console.log(JSON.stringify(result)); process.exit(result.ok ? 0 : 1); })
    .catch((error) => { console.error(JSON.stringify({ ok: false, error: error.message })); process.exit(1); });
}
