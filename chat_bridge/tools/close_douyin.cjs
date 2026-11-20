// 关闭专用抖音 Edge（只关 9227 这个调试实例，不碰用户其他 Edge 窗口）。
//   NODE_PATH=%USERPROFILE%\.dsh\profiles\desktop\node_modules node tools\close_douyin.cjs
const { chromium } = require('playwright');

(async () => {
  try {
    const browser = await chromium.connectOverCDP(process.env.DOUYIN_CDP || 'http://127.0.0.1:9227', { timeout: 10000 });
    const session = await browser.newBrowserCDPSession();
    await session.send('Browser.close').catch(() => {});
    console.log(JSON.stringify({ ok: true, action: 'closed_douyin' }));
  } catch (error) {
    console.log(JSON.stringify({ ok: false, error: error.message }));
    process.exitCode = 1;
  }
})();
