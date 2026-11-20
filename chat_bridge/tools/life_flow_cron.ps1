$py = 'C:\Users\Lenovo\anaconda3\python.exe'
$root = 'PROJECT_ROOT\chat_bridge'
$log = Join-Path $root 'logs\life_flow.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
& $py (Join-Path $root 'tools\life_flow.py') --once *>> $log
