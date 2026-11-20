$py = 'C:\Users\Lenovo\anaconda3\python.exe'
$root = 'PROJECT_ROOT\chat_bridge'
$log = Join-Path $root 'logs\reading_cron.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
& $py (Join-Path $root 'tools\autonomous_reading.py') --once *>> $log
