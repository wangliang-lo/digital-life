# Start IMAgent with its LLM endpoint pointed at the local Xiaoyu bridge.
# DSH honors DEEPSEEK_BASE_URL (trusted launch environment) as the provider root,
# so no config file of the app is touched: launch it normally and you are back
# to the stock agent.
$ErrorActionPreference = "Continue"

$env:DEEPSEEK_BASE_URL = "http://127.0.0.1:8003"
$exe = "C:\Users\Lenovo\Desktop\aihuman\app\IMAgent.exe"

Write-Host "DEEPSEEK_BASE_URL=$env:DEEPSEEK_BASE_URL"
Write-Host "start $exe"

# make sure the bridge is up (start it in this elevated tree so it outlives the chat session)
$bridgeUp = $false
try {
    $probe = Invoke-RestMethod -Uri "http://127.0.0.1:8003/" -TimeoutSec 3
    if ($probe.service -eq "anthropic-bridge") { $bridgeUp = $true; Write-Host ("bridge: {0}" -f $probe.service) }
} catch { }
if (-not $bridgeUp) {
    Write-Host "starting bridge on 8003 ..."
    $py = "C:\Users\Lenovo\anaconda3\python.exe"
    if (-not (Test-Path $py)) { $py = "python" }
    Start-Process -FilePath $py `
        -ArgumentList @("PROJECT_ROOT\chat_bridge\tools\anthropic_bridge.py", "--port", "8003", "--api", "http://127.0.0.1:8000") `
        -WorkingDirectory "PROJECT_ROOT\chat_bridge" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

Get-Process -Name IMAgent -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host ("stop old {0}" -f $_.Id)
    try { $_.Kill() } catch { }
}
Start-Sleep -Seconds 2

Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe)
Start-Sleep -Seconds 12

$alive = @(Get-Process -Name IMAgent -ErrorAction SilentlyContinue)
Write-Host ("IMAgent processes: {0}" -f $alive.Count)
Write-Host "Send a IM message to test. Log: PROJECT_ROOT\chat_bridge\data\anthropic_bridge.jsonl"
Read-Host "press enter to close"
