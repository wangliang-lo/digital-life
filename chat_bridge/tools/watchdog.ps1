# 她看护：每 60 秒检查一次服务，挂了就重新拉起。
# 由「启动」文件夹里的 她-看护.cmd 或桌面「小鱼看护」快捷方式启动，独立于 DSH。
# 2026-11-01 修：$root 曾取成 tools 目录，指向不存在的 start_all.ps1。
# 2026-11-01 补：7860 生图服务纳入探活。
# 2026-11-01 再修：以前用 `& $start` 直接调，start_all 的输出被后台进程继承会一直不返回，
#                  看护于是卡死在第一次拉起上、再也不探活。改成独立进程、不等待。
# 11239 替身已废弃（IM改走 DSH 插件里的 xiaoyu-bridge），不再探活。
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$start = Join-Path $root 'start_all.ps1'
$lock = Join-Path $root 'logs\watchdog.lock'

# 单实例：已经有看护在跑就提示并停住，别一闪就消失。
if (Test-Path $lock) {
  $old = Get-Content $lock -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($old -and (Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "已经有看护在跑（PID $old），这个窗口不用再开。" -ForegroundColor Yellow
    Write-Host "如果那个是临时会话里的，等它结束再双击本快捷方式即可。"
    Write-Host "本窗口会留在这里，直接关掉就行。"
    while ($true) { Start-Sleep -Seconds 3600 }
  }
}
Set-Content -Path $lock -Value $PID -Encoding ascii
Write-Host ""
Write-Host "小鱼看护已启动（PID $PID）。" -ForegroundColor Green
Write-Host "每 60 秒检查一次 8000 / 8899 / 7860，掉线自动拉起。"
Write-Host "这个窗口不要关。"
Write-Host ""

function Test-Up([string]$url) {
  try { Invoke-RestMethod $url -TimeoutSec 3 | Out-Null; return $true } catch { return $false }
}

# 单实例守卫：重复的进程会抢端口、白吃内存，只留一个
function Remove-Duplicate([string]$needle, [string]$port) {
  $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$needle*" })
  if ($procs.Count -le 1) { return }
  $keep = $null
  if ($port) {
    $conn = Get-NetTCPConnection -LocalPort ([int]$port) -State Listen -ErrorAction SilentlyContinue
    if ($conn) { $keep = $conn.OwningProcess }
  }
  if (-not $keep) { $keep = ($procs | Sort-Object CreationDate | Select-Object -First 1).ProcessId }
  foreach ($p in $procs) {
    if ($p.ProcessId -ne $keep) {
      try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        Write-Host ("[{0}] 结束重复的 {1} (PID {2})" -f (Get-Date -Format 'HH:mm:ss'), $needle, $p.ProcessId)
      } catch { }
    }
  }
}

while ($true) {
  Remove-Duplicate 'dl_api.py' '8000'
  Remove-Duplicate 'web_ui.py' '8899'
  Remove-Duplicate 'sleep_cycle.py' ''
  $api = Test-Up 'http://127.0.0.1:8000/status'
  $web = Test-Up 'http://127.0.0.1:8899/api/health'
  $draw = Test-Up 'http://127.0.0.1:7860/health'
  if (-not $api -or -not $web -or -not $draw) {
    Write-Host ("[{0}] 服务掉线 api={1} web={2} draw={3}，重新拉起" -f (Get-Date -Format 'HH:mm:ss'), $api, $web, $draw)
    # 独立进程、不等待：start_all 的后台子进程会继承输出句柄，直接 & 调用会卡住本循环。
    Start-Process -FilePath 'powershell.exe' `
      -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $start) `
      -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 30
  }
  Start-Sleep -Seconds 60
}
