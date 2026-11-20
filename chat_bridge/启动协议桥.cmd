@echo off
rem Start the Xiaoyu protocol bridge on 127.0.0.1:8003 as an administator
rem background process (no window). ASCII only.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -Verb RunAs -FilePath 'C:\Users\Lenovo\anaconda3\python.exe' -ArgumentList 'PROJECT_ROOT\chat_bridge\tools\anthropic_bridge.py','--port','8003','--api','http://127.0.0.1:8000' -WindowStyle Hidden"
if errorlevel 1 echo FAILED - right-click and choose Run as administrator.
echo.
echo UAC - click Yes. The bridge runs hidden on port 8003.
timeout /t 3 /nobreak >nul
powershell -NoProfile -Command "try { $r = Invoke-RestMethod http://127.0.0.1:8003/ -TimeoutSec 3; Write-Host ('bridge OK: ' + $r.service) } catch { Write-Host 'bridge NOT responding on 8003' }"
pause
