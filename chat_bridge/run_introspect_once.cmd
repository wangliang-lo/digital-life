@echo off
rem Run one introspection probe tick (Task Scheduler, every 30 minutes).
cd /d PROJECT_ROOT\chat_bridge
"C:\Users\Lenovo\anaconda3\python.exe" -u tools\introspect.py --once >> logs\introspect.out.log 2>&1
