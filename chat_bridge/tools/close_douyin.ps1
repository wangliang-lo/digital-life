# 关闭专用抖音 Edge，只影响 9227 调试实例。
$env:NODE_PATH = "$env:USERPROFILE\.dsh\profiles\desktop\node_modules"
node "$PSScriptRoot\close_douyin.cjs"
