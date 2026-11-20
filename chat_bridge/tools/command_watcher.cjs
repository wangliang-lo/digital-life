// 读取数字生命写下的指令文件，替她执行抖音动作（目前支持发表情）。
const fs = require('fs');
const { sendStickerTo } = require('./douyin_sticker_to.cjs');

const CMD = 'C:/aihuman/chat_bridge/data/douyin_command.json';
const RES = CMD.replace(/\.json$/, '.result.json');

setInterval(async () => {
  if (!fs.existsSync(CMD)) return;
  let cmd;
  try { cmd = JSON.parse(fs.readFileSync(CMD, 'utf8')); } catch { return; }
  try { fs.unlinkSync(CMD); } catch { /* ignore */ }
  let result = { ok: false, error: '未知指令' };
  try {
    if (cmd.action === 'sticker') result = await sendStickerTo(cmd.name || '');
    else result = { ok: false, error: '未知指令 ' + cmd.action };
  } catch (error) {
    result = { ok: false, error: error.message };
  }
  try { fs.writeFileSync(RES, JSON.stringify({ ...result, at: new Date().toISOString() })); } catch { /* ignore */ }
}, 2000);
