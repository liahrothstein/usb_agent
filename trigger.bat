@echo off
REM trigger.bat - sends HTTP POST to the already running USB Agent daemon.
REM
REM This is the FALLBACK trigger. The preferred way is the built-in global
REM hotkey Ctrl+Alt+Q (registered by the daemon via Win32 RegisterHotKey -
REM no shortcut, no external program, layout-independent). Use this .bat
REM only if the hotkey combination is already taken by another program.
REM
REM The daemon must already be up (run.bat starts it once at session start).
REM Double-click this file, or run from PowerShell:
REM   Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8765/trigger' -Method POST

set TRIGGER_PORT=8765

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%TRIGGER_PORT%/trigger' -Method POST -TimeoutSec 10; Write-Host '[INFO] Trigger sent. Daemon response:' $r.Content } catch { if ($_.Exception.Response) { Write-Host '[WARN] Trigger failed:' $_.Exception.Message } else { Write-Host '[WARN] Daemon not running on port %TRIGGER_PORT%. Start run.bat first.' } }"
