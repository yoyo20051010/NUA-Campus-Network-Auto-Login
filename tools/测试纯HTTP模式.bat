@echo off
chcp 65001 >nul
title Campus Net - HTTP Mode Test
cd /d "%~dp0"
echo ============================================================
echo   Pure HTTP login test
echo   - disables the wired NIC for 5 minutes
echo   - then re-enables it and logs in WITHOUT a browser
echo   - if that fails, the browser version rescues the network
echo   ------------------------------------------------------------
echo   You will lose network for about 5-6 minutes.
echo   Click YES on the UAC prompt to continue.
echo ============================================================
echo.
pause
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_nic.ps1" -Seconds 300 -AutoLogin -Engine http
echo.
pause
