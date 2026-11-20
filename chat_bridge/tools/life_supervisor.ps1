# Keep her life loop alive. ASCII ONLY (Windows PowerShell reads .ps1 as ANSI).
# If life_loop.py exits for any reason, start it again 5 seconds later.
$ErrorActionPreference = 'Continue'
$root = 'PROJECT_ROOT\chat_bridge'
$python = 'C:\Users\Lenovo\anaconda3\python.exe'
$log = Join-Path $root 'logs\life_loop.out.log'
$err = Join-Path $root 'logs\life_loop.err.log'

Set-Location $root
Write-Output ("[" + (Get-Date -Format 'HH:mm:ss') + "] life_supervisor started") | Out-File -FilePath $log -Append -Encoding utf8

while ($true) {
    $stamp = Get-Date -Format 'HH:mm:ss'
    Write-Output ("[" + $stamp + "] start life_loop") | Out-File -FilePath $log -Append -Encoding utf8
    & $python -u (Join-Path $root 'tools\life_loop.py') --watch 20 2>> $err
    $code = $LASTEXITCODE
    Write-Output ("[" + (Get-Date -Format 'HH:mm:ss') + "] life_loop exited code=" + $code + ", restart in 5s") | Out-File -FilePath $log -Append -Encoding utf8
    Start-Sleep -Seconds 5
}
