# 早晨醒来：清掉夜间离线标记，让她重新上线（不启动抖音，观看/私信按日程再开）。
$root = 'PROJECT_ROOT\chat_bridge'
if ($PSScriptRoot) { $root = Split-Path -Parent $PSScriptRoot }
$marker = Join-Path $root 'data\asleep.json'
if (Test-Path -LiteralPath $marker) {
    Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
    Write-Output '{"ok": true, "asleep": false, "action": "woke_up"}'
} else {
    Write-Output '{"ok": true, "asleep": false, "action": "already_awake"}'
}
