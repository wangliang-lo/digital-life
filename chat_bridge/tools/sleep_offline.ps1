# 夜间睡眠：关闭专用抖音浏览器里所有抖音页面，并写入 data\asleep.json 离线标记。
$env:NODE_PATH = "$env:USERPROFILE\.dsh\profiles\desktop\node_modules"
node "$PSScriptRoot\sleep_offline.cjs"
