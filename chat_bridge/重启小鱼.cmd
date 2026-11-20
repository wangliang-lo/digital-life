@echo off
rem Xiaoyu restart launcher. Keep ASCII only: cmd.exe reads this file as ANSI.
cd /d "%~dp0"
echo Restarting Xiaoyu (dl_api + web_ui)...
echo A UAC window will pop up. Click Yes.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','%~dp0restart_all.ps1'"
if errorlevel 1 (
  echo.
  echo FAILED to start the admin window.
  echo Right-click this file and choose "Run as administrator".
)
echo.
echo If no window appeared, right-click this .cmd and choose "Run as administrator".
pause
