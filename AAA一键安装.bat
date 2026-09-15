@echo off
chcp 65001 >nul
title Campus Net Auto Login - Setup
cd /d "%~dp0"
echo ============================================================
echo   Campus Net Auto Login - Setup
echo ============================================================
echo.
echo   What this does:
echo     - saves your campus account on THIS PC only
echo     - auto login on boot, and auto reconnect within 1 minute
echo     - never touches other networks (home wifi / hotspot)
echo     - never uploads anything
echo.
echo   What you need:
echo     nothing to install - runtime is bundled
echo.
echo   The wizard will explain details and ask for your
echo   campus account and password.
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" (
  echo [!] Setup exited with code %RC%. Please read the messages above.
  echo     For help, open the .txt files in this folder.
)
pause
