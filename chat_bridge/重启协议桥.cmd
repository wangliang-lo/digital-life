@echo off
rem Replace the bridge on 8003 with the current code. ASCII only, self-elevates.
cd /d "%~dp0"
echo Restarting the Xiaoyu protocol bridge on 8003 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','%~dp0restart_bridge.ps1'"
echo.
echo A UAC window should pop up - click Yes.
pause
