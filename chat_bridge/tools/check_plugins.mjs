// 用应用自己的 catalog 模块校验：插件目录和 plugins.json 认不认。
import path from 'node:path';
const root = 'C:/Users/Lenovo/Desktop/aihuman/data/workspace';
const mod = await import('file:///' + path.join(root, 'packages/plugin-api/catalog.mjs').replace(/\\/g, '/'));
try {
  const items = mod.scanPlugins(root);
  console.log('扫描到插件:', items.map((i) => ({ id: i.id, ok: !i.invalid, reason: i.reason })));
  const { config, revision } = mod.configuration(root);
  console.log('plugins.json 有效，revision:', String(revision).slice(0, 12));
  console.log('已配置:', Object.keys(config.plugins));
} catch (error) {
  console.log('校验失败:', error.message);
  process.exit(1);
}
