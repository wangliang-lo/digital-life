import path from 'node:path';
const root = 'C:/Users/Lenovo/Desktop/aihuman/data/workspace';
const mod = await import('file:///' + path.join(root, 'packages/plugin-api/catalog.mjs').replace(/\\/g, '/'));
const catalog = mod.pluginCatalog(root, []);
for (const item of catalog.plugins) {
  console.log({
    id: item.id,
    enabled: item.enabled,
    permissions: item.permissions,
    chats: item.chats,
    invalid: item.invalid,
    reason: item.reason,
    settings: Object.fromEntries(Object.entries(item.settings ?? {}).filter(([k]) => ['apiProtocol','endpoint','model','audioFormat','requestMode','responsePath','timeoutMs'].includes(k))),
  });
}
