$py = 'C:\Users\Lenovo\anaconda3\python.exe'
$root = 'PROJECT_ROOT\chat_bridge'
$log = Join-Path $root 'logs\goal_engine.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
& $py (Join-Path $root 'tools\goal_engine.py') --once *>> $log
