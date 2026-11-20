# Replace every process listening on 8003 with the current bridge code.
# The old script only killed one owner from Get-NetTCPConnection; Windows allows
# several sockets to listen on the same port, so stale processes survived.
$ErrorActionPreference = "Continue"
$py = "C:\Users\Lenovo\anaconda3\python.exe"
$script = "PROJECT_ROOT\chat_bridge\tools\anthropic_bridge.py"
$port = 8003

function Get-PortOwners([int]$port) {
    $owners = @()
    foreach ($line in (netstat -ano | Select-String (":{0} .*LISTENING" -f $port))) {
        $text = $line.ToString().Trim()
        $fields = $text -split '\s+'
        $last = $fields[-1]
        if ($last -match '^\d+$') { $owners += [int]$last }
    }
    return $owners | Sort-Object -Unique
}

Write-Host "== stop everything on $port =="
for ($round = 1; $round -le 4; $round++) {
    $owners = Get-PortOwners $port
    if (-not $owners) { break }
    Write-Host ("  round {0}: pids {1}" -f $round, ($owners -join ", "))
    foreach ($owner in $owners) {
        & taskkill /F /PID $owner 2>&1 | Out-Null
    }
    Start-Sleep -Seconds 2
}
$left = Get-PortOwners $port
if ($left) {
    Write-Host ("  still holding: {0}" -f ($left -join ", ")) -ForegroundColor Red
} else {
    Write-Host "  port free" -ForegroundColor Green
}
Start-Sleep -Seconds 1

Write-Host "== start new bridge =="
Start-Process -FilePath $py -ArgumentList @($script, "--port", "$port", "--api", "http://127.0.0.1:8000") -WindowStyle Hidden
Start-Sleep -Seconds 4

Write-Host ("== owner now: {0} ==" -f ((Get-PortOwners $port) -join ", "))

Write-Host "== verify =="
try {
    $probe = Invoke-RestMethod -Uri "http://127.0.0.1:$port/" -TimeoutSec 3
    Write-Host ("  service: {0}" -f $probe.service) -ForegroundColor Green
} catch {
    Write-Host "  bridge not responding" -ForegroundColor Red
}

$body = '{"model":"xiaoyu","messages":[{"role":"user","content":"爸爸: 在吗"}],"stream":false}'
$ok = $false
for ($try = 1; $try -le 3; $try++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/v1/chat/completions" -Method POST -Body $body `
            -ContentType "application/json" -TimeoutSec 120 -UseBasicParsing
        if ($r.Content -match '"object":\s*"chat\.completion"') { $ok = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if ($ok) {
    Write-Host "  OpenAI protocol: OK" -ForegroundColor Green
} else {
    Write-Host "  OpenAI protocol: still wrong - send the screenshot to the agent" -ForegroundColor Red
}
Read-Host "press enter to close"
