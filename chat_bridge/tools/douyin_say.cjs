// 以她的身份，往指定抖音会话发一条文字。
//   node tools/douyin_say.cjs --name "熟人B" --text "在呢，刚没看手机"
//
// 2026-11-01 加固：以前只按名字找元素，页面一换就发错人（真的发错过）。
// 现在必须四道确认：会话标题对得上 → 输入框清空 → 输入内容回读一致 → 发出后能看到这条新消息。
// 任何一步不满足就报错退出，不发。
const { chromium } = require('playwright');

const CDP = process.env.DOUYIN_CDP || 'http://127.0.0.1:9227';

function arg(name, fallback) {
  const idx = process.argv.indexOf('--' + name);
  return idx >= 0 && process.argv[idx + 1] ? process.argv[idx + 1] : fallback;
}

// 2026-11-07：抖音输入框会在末尾塞一个零宽空格（U+200B），
// 原来只去普通空白，回读永远"不一致"→ 每次都在这里放弃、一条也发不出去。
const normalize = (value) => String(value || '').replace(/[\s\u200b\u200c\u200d\ufeff]+/g, '');

(async () => {
  const name = (arg('name', '') || '').trim();
  const text = arg('text', '');
  if (!text) { console.log(JSON.stringify({ ok: false, error: '缺少 --text' })); process.exit(2); }
  if (!name) { console.log(JSON.stringify({ ok: false, error: '必须给 --name，避免发错人' })); process.exit(2); }

  const browser = await chromium.connectOverCDP(CDP);
  const page = browser.contexts().flatMap((c) => c.pages()).find((p) => p.url().includes('douyin.com'));
  if (!page) { console.log(JSON.stringify({ ok: false, error: '没有抖音页面' })); process.exit(2); }

  const currentTitle = () => page.evaluate(() => {
    // 2026-11-07：抖音新版把私信面板的 data-e2e="im-dialog" 去掉了，
    // 原来只认它会永远读到空标题、于是一律判"不是这个人"而拒发。
    // 现在先认右栏标题（RightPanelHeadertitle 就是当前会话的人名），再退回旧选择器。
    const panel =
      document.querySelector('[class*="RightPanelHeadertitle"]') ||
      document.querySelector('[class*="RightPanelHeaderconvHeader"]') ||
      document.querySelector('[data-e2e="im-dialog"]');
    if (!panel) return '';
    const lines = (panel.innerText || '').trim().split('\n').map((line) => line.trim()).filter(Boolean);
    return lines.slice(0, 2).join('|');
  });

  // 0) 2026-11-07：她第一次要主动发私信时，页面可能停在推荐流或其他页，
  //    私信面板根本不存在（currentTitle 返回空 → 直接判"不是这个人"）。
  //    这里先确认页面在私信上，不在就先切过去，再往下走四道核对。
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const hasDialog = await page.evaluate(() => Boolean(document.querySelector('[data-e2e="im-dialog"]')));
    if (hasDialog) break;
    if (!page.url().includes('/chat')) {
      await page.goto('https://www.douyin.com/chat', { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    } else {
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    }
    await page.waitForTimeout(3500);
  }

  const titleMatches = async () => normalize(await currentTitle()).includes(normalize(name));

  // 1) 会话必须是这个人；不是就只在会话列表里点“名字”那一项，绝不点正文里的同名文字
  if (!(await titleMatches())) {
    // 2026-11-07：原来的选择器 `[class*="conversation"]` 太宽，会点到大容器甚至正文里的同名文字，
    // 点完面板还是空的。现在优先用抖音私信列表的真实类名，并且点完等面板出现再验标题。
    const selectors = [
      '[class*="conversationConversationItem"]',
      '[data-e2e="im-conversation-item"]',
      '.conversation-item',
    ];
    let clicked = false;
    for (const selector of selectors) {
      const row = page.locator(selector).filter({ hasText: name }).first();
      if (await row.count()) {
        await row.click({ force: true }).catch(() => {});
        clicked = true;
        break;
      }
    }
    if (clicked) {
      // 等私信面板真的出现（最多 8 秒），别用固定 1.5 秒赌
      await page.waitForSelector('[data-e2e="im-dialog"]', { timeout: 8000 }).catch(() => {});
      await page.waitForTimeout(500);
    }
  }
  if (!(await titleMatches())) {
    console.log(JSON.stringify({ ok: false, error: `当前会话不是 ${name}（面板：${(await currentTitle()).slice(0, 30)}），未发送`, aborted: true }));
    process.exit(3);
  }

  // 2) 清空输入框（草稿会粘在新消息前面）
  const input = page.locator('[data-e2e="msg-input"], [contenteditable="true"]').first();
  if ((await input.count()) === 0) { console.log(JSON.stringify({ ok: false, error: '找不到输入框' })); process.exit(2); }
  await input.click({ force: true });
  await page.waitForTimeout(200);
  await page.keyboard.press('Control+A');
  await page.keyboard.press('Backspace');
  await page.waitForTimeout(200);

  // 3) 逐字输入并回读校验，输错就放弃
  for (const ch of Array.from(text)) {
    await page.keyboard.insertText(ch);
    await page.waitForTimeout(50 + Math.floor(Math.random() * 40));
  }
  await page.waitForTimeout(300);
  const typed = await input.evaluate((el) => (el.innerText || el.value || '').trim());
  if (normalize(typed) !== normalize(text)) {
    console.log(JSON.stringify({ ok: false, error: `输入框内容不一致（${typed.slice(0, 20)}），未发送` }));
    process.exit(4);
  }

  // 4) 发送后确认这条消息真的出现在会话里
  await page.keyboard.press('Enter');
  let delivered = false;
  for (let attempt = 0; attempt < 12 && !delivered; attempt += 1) {
    await page.waitForTimeout(500);
    delivered = await page.evaluate((wanted) => {
      // 2026-11-07：消息气泡的类名不是 msg-item/message（那是旧版），
      // 按类名找永远找不到 → 明明发出去了却报"没看到这条"，还会诱导重发。
      // 现在改成：找文本完全等于这条消息的叶子节点。
      const norm = (v) => String(v || '').replace(/[\s\u200b\u200c\u200d\ufeff]+/g, '');
      const target = norm(wanted);
      if (!target) return false;
      const nodes = Array.from(document.querySelectorAll('div, span, p'));
      return nodes.some((el) => el.children.length === 0 && norm(el.innerText) === target);
    }, text).catch(() => false);
  }
  if (!delivered) {
    console.log(JSON.stringify({ ok: false, error: '发出去后没在会话里看到这条消息，请不要再重试，先人工确认', uncertain: true }));
    process.exit(5);
  }
  console.log(JSON.stringify({ ok: true, sent: text, to: name }));
  process.exit(0);
})().catch((e) => { console.error(JSON.stringify({ ok: false, error: e.message })); process.exit(1); });
