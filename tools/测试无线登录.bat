@echo off
chcp 65001 >nul
title Campus Net - WiFi Login Test
cd /d "%~dp0"
echo ============================================================
echo   Campus Net - WiFi Login Test
echo ------------------------------------------------------------
echo   This will:
echo     1. disable the WIRED NIC (you go offline for a moment)
echo     2. wait until the portal is reachable via WiFi
echo     3. run the login test through WiFi (Dr.COM flow)
echo     4. restore the WIRED NIC right after the test
echo   ------------------------------------------------------------
echo   Keep the campus WiFi connected. Click YES on UAC.
echo ============================================================
echo.
pause
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0test_wifi_login.ps1"
echo.
pause
