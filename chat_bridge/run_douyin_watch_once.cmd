@echo off
rem One douyin browser watchdog check (Task Scheduler, every 5 minutes).
cd /d PROJECT_ROOT\chat_bridge
"C:\Users\Lenovo\anaconda3\python.exe" -u tools\douyin_watchdog.py --once >> logs\douyin_watchdog.out.log 2>&1
