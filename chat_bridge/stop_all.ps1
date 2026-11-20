# 停止 start_all.ps1 拉起的进程（只停命令行里带本项目脚本的 python）
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
try {
  $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop
} catch {
  Write-Warning "无法枚举进程（$($_.Exception.Message)）：本脚本没有停止任何服务。请在普通 PowerShell 窗口里运行。"
  Write-Host '完成（未做任何操作）。'
  exit 0
}
$procs |
  Where-Object { $_.CommandLine -match 'dl_api\.py|web_ui\.py|sync_memory\.py|sync_from_douyin\.py|social_time\.py|watch_guard\.py|profile_builder\.py|affection\.py|style_trainer\.py|backup_memory\.py|sleep_cycle\.py' } |
  ForEach-Object {
    Write-Host ("停止 " + $_.ProcessId + "  " + $_.CommandLine)
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
# 清掉 PID 记录，免得下次 start_all 拿旧号码误判“还在跑”
$pidDir = Join-Path $root 'logs\pids'
if (Test-Path $pidDir) { Get-ChildItem $pidDir -Filter '*.pid' | Remove-Item -Force -ErrorAction SilentlyContinue }
Write-Host '完成。'
