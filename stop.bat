@echo off
chcp 65001 >nul
echo Stopping llama-server, Python agent and Chrome with profile...
taskkill /F /IM llama-server.exe 2>nul
REM Kill Python agent (main.py) via PowerShell (wmic removed in Windows 11 24H2+).
REM Chrome cleanup (tree-kill + lock removal) is handled by chrome_cleanup.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*main.py*' } | Stop-Process -Force -ErrorAction SilentlyContinue" 2>nul
echo Cleaning Chrome profile...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0chrome_cleanup.ps1"
echo Done.
timeout /t 2 /nobreak >nul