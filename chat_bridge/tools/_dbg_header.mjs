import { chromium } from 'playwright';
const browser = await chromium.connectOverCDP('http://127.0.0.1:9227');
const ctx = browser.contexts()[0];
const page = ctx.pages().find((p) => p.url().includes('/chat')) || await ctx.newPage();
if (!page.url().includes('/chat')) { await page.goto('https://www.douyin.com/chat', { waitUntil: 'domcontentloaded' }); await page.waitForTimeout(3000); }
const info = await page.evaluate(() => {
  const out = [];
  const wrap = document.querySelector('[class*="componentsRightPanelwrapper"]');
  if (wrap) {
    out.push({ tag: 'wrapper', cls: String(wrap.className).slice(0, 60), text: (wrap.innerText || '').replace(/\s+/g, ' ').slice(0, 60) });
    for (const child of [...wrap.children].slice(0, 3)) {
      out.push({ tag: 'child', cls: String(child.className).slice(0, 60), text: (child.innerText || '').replace(/\s+/g, ' ').slice(0, 60) });
      for (const g of [...child.children].slice(0, 2)) {
        out.push({ tag: 'grandchild', cls: String(g.className).slice(0, 60), text: (g.innerText || '').replace(/\s+/g, ' ').slice(0, 60) });
      }
    }
  }
  return out;
});
console.log(JSON.stringify(info, null, 2));
process.exit(0);
