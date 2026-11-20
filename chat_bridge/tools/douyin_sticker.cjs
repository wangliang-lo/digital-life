// 抖音私信发表情：点表情面板 → 选一张 → 发送
//
//   node tools/douyin_sticker.cjs --dry-run     # 只打开面板并报告有几张表情
//   node tools/douyin_sticker.cjs --index 0     # 发面板里的第 1 张
//   node tools/douyin_sticker.cjs --random      # 随机发一张
//
// 需要 NODE_PATH 指向 DSH profile 的 node_modules，且抖音浏览器开着（CDP 9227）。
const { chromium } = require('playwright');

const CDP = process.env.DOUYIN_CDP || 'http://127.0.0.1:9227';

async function getPage() {
  const browser = await chromium.connectOverCDP(CDP);
  const pages = browser.contexts().flatMap((c) => c.pages());
  return pages.find((p) => p.url().includes('douyin.com'));
}

async function openEmojiPanel(page) {
  const actions = page.locator('.messageMsgInputinputAction');
  const count = await actions.count();
  if (!count) return { ok: false, error: '没有找到输入框按钮（messageMsgInputinputAction）' };
  // 三个按钮通常是：上传图片 / 表情 / 发送
  await actions.nth(Math.min(1, count - 1)).click({ force: true });
  await page.waitForTimeout(900);
  const state = await page.evaluate(() => {
    const imgs = [...document.querySelectorAll('img')].filter((img) => {
      const r = img.getBoundingClientRect();
      return r.width > 20 && r.height > 20 && r.y > innerHeight * 0.35;
    });
    const panel = imgs.length ? imgs[0].closest('div[class]') : null;
    let root = panel;
    for (let i = 0; i < 4 && root && root.parentElement; i++) root = root.parentElement;
    return { imgCount: imgs.length, panelClass: panel ? String(panel.className).slice(0, 60) : null, rootClass: root ? String(root.className).slice(0, 60) : null };
  });
  return { ok: true, actionCount: count, ...state };
}

async function sendSticker(page, index) {
  const imgs = page.locator('img');
  const candidates = [];
  const total = await imgs.count();
  for (let i = 0; i < total && candidates.length < 60; i += 1) {
    const box = await imgs.nth(i).boundingBox().catch(() => null);
    if (box && box.width > 20 && box.height > 20 && box.y > 300) candidates.push({ i, box });
  }
  if (!candidates.length) return { ok: false, error: '面板里没有找到表情图片' };
  const pick = candidates[Number.isInteger(index) ? Math.min(index, candidates.length - 1) : Math.floor(Math.random() * candidates.length)];
  await imgs.nth(pick.i).click({ force: true });
  await page.waitForTimeout(1200);
  // 有些版本选完还要按发送/回车
  await page.keyboard.press('Enter').catch(() => {});
  await page.waitForTimeout(800);
  return { ok: true, picked: pick.i, total: candidates.length };
}

(async () => {
  const args = process.argv.slice(2);
  const dryRun = args.includes('--dry-run');
  const indexArg = args.indexOf('--index');
  const index = indexArg >= 0 ? Number(args[indexArg + 1]) : null;
  const fileArg = args.indexOf('--file');
  const file = fileArg >= 0 ? args[fileArg + 1] : null;

  const page = await getPage();
  if (!page) { console.log(JSON.stringify({ ok: false, error: '没有抖音页面' })); process.exit(1); }

  if (file) {
    const input = page.locator('.MsgInputFileUpload input[type="file"], input[type="file"]').first();
    if (await input.count() === 0) { console.log(JSON.stringify({ ok: false, error: '找不到上传控件' })); process.exit(2); }
    if (dryRun) { console.log(JSON.stringify({ ok: true, dryRun: true, file })); process.exit(0); }
    await input.setInputFiles(file);
    await page.waitForTimeout(1800);
    await page.keyboard.press('Enter').catch(() => {});
    await page.waitForTimeout(800);
    console.log(JSON.stringify({ ok: true, sent: 'file', file }));
    process.exit(0);
  }

  const opened = await openEmojiPanel(page);
  if (!opened.ok) { console.log(JSON.stringify(opened)); process.exit(2); }
  if (dryRun) {
    await page.keyboard.press('Escape').catch(() => {});
    console.log(JSON.stringify({ ok: true, dryRun: true, ...opened }));
    process.exit(0);
  }
  const sent = await sendSticker(page, index);
  console.log(JSON.stringify({ ok: sent.ok, opened, sent }, null, 2));
  process.exit(sent.ok ? 0 : 2);
})().catch((e) => { console.error(JSON.stringify({ ok: false, error: e.message })); process.exit(1); });
