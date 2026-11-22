@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set CHROME_PORT=9222
set LLAMA_PORT=8080
set TRIGGER_PORT=8765
set MODEL=

echo ============================================
echo  USB Agent launcher (daemon mode)
echo ============================================

REM --- 1. llama-server: start ONLY if /health does not respond ---
REM    Previously run.bat killed and restarted llama-server every time.
REM    Now on repeat launches (e.g. you ran run.bat by mistake while the
REM    daemon is already up) the server is not touched, profile not cleaned.
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%LLAMA_PORT%/health' -TimeoutSec 2).StatusCode | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    if exist "models\*.gguf" (
        for %%f in (models\*.gguf) do (
            if not defined MODEL set "MODEL=%%f"
        )
    )
    if defined MODEL (
        if exist "llama\llama-server.exe" (
            echo [INFO] Starting llama-server with model !MODEL!
            start "" /B llama\llama-server.exe -m "!MODEL!" --port %LLAMA_PORT% --host 127.0.0.1 --cont-batching
            echo [INFO] Waiting 6 sec for server to start...
            timeout /t 6 /nobreak >nul
        ) else (
            echo [WARN] llama\llama-server.exe not found.
            echo        Download llama-bXXXX-bin-win-cpu-x64.zip from
            echo        https://github.com/ggml-org/llama.cpp/releases
            echo        and extract its contents into the llama\ folder next to this script.
            echo        Need the whole folder ^(llama-server.exe + all .dll files^).
        )
    ) else (
        echo [WARN] No .gguf file found in models\ folder.
        echo        Download a model ^(e.g. Qwen2.5-3B-Instruct Q4_K_M^) from HuggingFace
        echo        and put the .gguf into models\. See README.md.
    )
) else (
    echo [INFO] llama-server already running on port %LLAMA_PORT% - skip start.
)

REM --- 2. Chrome: start ONLY if /json/version does not respond ---
REM    Without this check every hotkey press (if it points to run.bat)
REM    would kill all Chrome windows. Now Chrome survives restarts.
REM    Cold start (Chrome not on debug port): the #1 reason the window
REM    doesn't appear on cold start is a STALE chrome.exe still holding
REM    the chrome_profile lock - a new chrome.exe with the same
REM    --user-data-dir just forwards to the dead instance and exits
REM    without opening a window. So: kill stale chrome.exe bound to OUR
REM    port/profile only (personal Chrome is untouched), nuke the profile
REM    so Chrome starts truly fresh, relaunch with first-run dialogs off,
REM    and VERIFY it actually came up on the debug port.
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%CHROME_PORT%/json/version' -TimeoutSec 2).StatusCode | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Cold start: killing stale Chrome on port %CHROME_PORT% / profile chrome_profile...
    powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and ($_.CommandLine -match 'remote-debugging-port=%CHROME_PORT%' -or $_.CommandLine -match 'chrome_profile') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    timeout /t 2 /nobreak >nul
    if exist "chrome_profile" (
        echo [INFO] Removing old Chrome profile ^(cold start^)...
        rd /s /q "chrome_profile" 2>nul
    )
    set "CHROME="
    if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
    if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
    if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
    if defined CHROME (
        echo [INFO] Starting Chrome with --remote-debugging-port=%CHROME_PORT%
        start "" "!CHROME!" --remote-debugging-port=%CHROME_PORT% --user-data-dir="%~dp0chrome_profile" --no-first-run --no-default-browser-check
        echo [INFO] Waiting 5 sec for Chrome to start...
        timeout /t 5 /nobreak >nul
        powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%CHROME_PORT%/json/version' -TimeoutSec 5).StatusCode | Out-Null; echo '[INFO] Chrome is up on port %CHROME_PORT%.' } catch { echo '[WARN] Chrome did NOT respond on port %CHROME_PORT% within 5 sec - window may not have appeared.'; echo '       Manual launch: chrome --remote-debugging-port=%CHROME_PORT% --user-data-dir=chrome_profile --no-first-run' }"
    ) else (
        echo [WARN] Chrome not found. Start it manually:
        echo        chrome --remote-debugging-port=%CHROME_PORT% --user-data-dir=chrome_profile
    )
) else (
    echo [INFO] Chrome already running on debug port %CHROME_PORT% - skip start.
)

REM --- 3. If daemon is already running - do NOT start a second one, just trigger it ---
REM    This protects against double launch: you pressed a hotkey that points
REM    to run.bat, but the daemon is already up. Without this check you would
REM    get a second daemon conflicting on port 8765.
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%TRIGGER_PORT%/health' -TimeoutSec 2).StatusCode | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    echo [INFO] Daemon already running on port %TRIGGER_PORT% - sending trigger.
    powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%TRIGGER_PORT%/trigger' -Method POST -TimeoutSec 10 | Out-Null; echo '[INFO] Trigger sent.' } catch { echo '[WARN] Trigger failed: ' + $_.Exception.Message }"
    goto :eof
)

REM --- 4. Python agent (daemon mode) ---
where python >nul 2>&1
if errorlevel 1 (
    echo [FATAL] Python not found in PATH. Install Python 3.8+.
    pause
    exit /b 1
)

if not exist "agent.log" (
    echo [INFO] First run - make sure websocket-client is installed:
    echo        pip install -r requirements.txt
)

REM --- 4b. Built-in global hotkey (no external deps) ---
REM    The daemon registers Ctrl+Alt+Q via Win32 RegisterHotKey (ctypes,
REM    Python stdlib). Layout-independent: same physical key fires under
REM    any input language (registers by virtual-key code, not character).
REM    No pynput, no AutoHotkey, no desktop shortcut needed.

echo [INFO] Starting agent in daemon mode...
echo [INFO] Built-in hotkey: Ctrl+Alt+Q (Win32 RegisterHotKey, no install needed)
echo [INFO]   Switch to the test tab in Chrome, press Ctrl+Alt+Q (works under any layout).
echo [INFO]   Change hotkey: config.json -^> agent.hotkey, or CLI --hotkey "<ctrl>+<shift>+f12"
echo [INFO] HTTP trigger also works: trigger.bat (or POST /trigger)
echo [INFO] Press Ctrl+C to stop the daemon.
echo.
python main.py --daemon
pause
