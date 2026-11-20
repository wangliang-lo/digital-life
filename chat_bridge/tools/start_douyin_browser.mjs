// 启动抖音专用浏览器（Edge + CDP 9227），等它 ready 就打印端点。
// 用法：node _tmp/start_douyin_browser.mjs
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const ROOT = 'C:/Users/Lenovo/.dsh/profiles/desktop/node_modules/dsh-douyin-browser-local';
const locationFile = path.join(os.homedir(), '.dsh', 'douyin-browser', 'runtime-location.json');
let stateDir;
try {
  stateDir = JSON.parse(fs.readFileSync(locationFile, 'utf8')).stateDir;
} catch {
  // 插件从没写过 runtime-location.json：按它的默认位置来
  stateDir = path.join(os.homedir(), '.dsh', 'douyin-browser');
}
const PROFILE = path.join(stateDir, 'cli-profile');
const CONTROL = path.join(stateDir, 'control');
const PORT = 9227;

fs.mkdirSync(CONTROL, { recursive: true });
const stateFile = path.join(CONTROL, 'browser.json');
const logFile = path.join(CONTROL, 'browser-host.log');

const output = fs.openSync(logFile, 'a');
const child = spawn(process.execPath, [
  path.join(ROOT, 'browser-host.mjs'), PROFILE, CONTROL, String(PORT),
], { detached: true, windowsHide: true, stdio: ['ignore', output, output] });
child.unref();
console.log('host pid', child.pid);

const deadline = Date.now() + 30000;
while (Date.now() < deadline) {
  await new Promise((r) => setTimeout(r, 500));
  let state = null;
  try { state = JSON.parse(fs.readFileSync(stateFile, 'utf8')); } catch {}
  if (state && state.ready && state.port === PORT) {
    const res = await fetch(`http://127.0.0.1:${PORT}/json/version`).catch(() => null);
    if (res && res.ok) {
      console.log('CDP ready:', `http://127.0.0.1:${PORT}`);
      process.exit(0);
    }
  }
}
console.log('启动超时，看日志：', logFile);
process.exit(1);
