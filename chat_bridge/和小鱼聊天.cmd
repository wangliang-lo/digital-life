@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "& '%~dp0start_all.ps1'; $u='http://127.0.0.1:8899/'; for($i=0;$i -lt 30;$i++){try{Invoke-WebRequest $u -UseBasicParsing -TimeoutSec 2 | Out-Null; break}catch{Start-Sleep -Seconds 1}}; Start-Process msedge -ArgumentList ('--app=' + $u)"
endlocal
