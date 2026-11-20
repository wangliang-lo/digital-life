# Stop dl_api / web_ui, then start everything again, then run health check.
# Called by 重启小鱼.cmd with admin rights. Writes logs\restart_*.log.
# NOTE: keep this file pure ASCII so Windows PowerShell never mis-parses it.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir "restart_$stamp.log"
try { Start-Transcript -Path $logPath -Force | Out-Null } catch { }
Write-Host "log: $logPath"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Write-Host ("admin: {0}" -f $isAdmin)

Write-Host "== stop =="
$pidDir = Join-Path $root "logs\pids"
$stopped = 0
$targets = @()
try {
    $targets = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -match 'dl_api\.py|web_ui\.py|pending_replay\.py|solver_channel\.py|tts_server\.py' })
} catch {
    Write-Warning ("cannot enumerate processes: {0}" -f $_.Exception.Message)
}
foreach ($p in $targets) {
    Write-Host ("kill {0} {1}" -f $p.ProcessId, $p.CommandLine)
    & taskkill /F /PID $p.ProcessId 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $stopped++ }
}
foreach ($name in @("dl_api", "web_ui", "pending_replay", "solver_channel", "tts_server")) {
    $file = Join-Path $pidDir "$name.pid"
    if (Test-Path $file) {
        $old = (Get-Content $file -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($old) {
            Write-Host ("pid file {0} -> {1}" -f $name, $old)
            & taskkill /F /PID ([int]$old) 2>&1 | Out-Null
        }
        Remove-Item $file -Force -ErrorAction SilentlyContinue
    }
}
Write-Host ("killed: {0}" -f $stopped)

Start-Sleep -Seconds 3
$listen = netstat -ano | Select-String ":8000 .*LISTENING"
if ($listen) {
    Write-Host "WARN: port 8000 still listening, stop did not work" -ForegroundColor Yellow
    Write-Host "$listen"
} else {
    Write-Host "port 8000 free" -ForegroundColor Green
}

Write-Host ""
Write-Host "== start =="
& (Join-Path $root "start_all.ps1")
Start-Sleep -Seconds 8

Write-Host ""
Write-Host "== health =="
$py = "C:\Users\Lenovo\anaconda3\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py (Join-Path $root "tools\health.py")

Write-Host ""
Write-Host "If port 8000 was still listening above, run this window's command again or reboot." -ForegroundColor DarkGray
Write-Host "Close this window when done." -ForegroundColor Green
try { Stop-Transcript | Out-Null } catch { }
