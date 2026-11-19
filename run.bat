@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set CHROME_PORT=9222
set LLAMA_PORT=8080
set MODEL=

echo ============================================
echo  USB Agent launcher
echo ============================================

REM --- 1. llama-server + GGUF model ---
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

REM --- 2. Chrome with remote debugging ---
REM Clean Chrome profile lock files from previous runs.
REM Without this, Chrome fails with "cannot read/write profile directory"
REM because SingletonLock is an exclusive lock held by parent chrome.exe
REM AND inherited by child processes (renderer, GPU, network). The helper
REM .ps1 walks the process tree, kills the whole tree, waits for handles
REM to release, deletes lock files, and if deletion still fails removes
REM the whole chrome_profile dir so Chrome recreates a fresh one.
if exist "chrome_profile" (
    echo [INFO] Cleaning Chrome profile...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0chrome_cleanup.ps1"
)

set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"

if defined CHROME (
    echo [INFO] Starting Chrome with --remote-debugging-port=%CHROME_PORT%
    start "" "%CHROME%" --remote-debugging-port=%CHROME_PORT% --user-data-dir="%~dp0chrome_profile"
    timeout /t 2 /nobreak >nul
) else (
    echo [WARN] Chrome not found. Start it manually:
    echo        chrome --remote-debugging-port=%CHROME_PORT%
)

REM --- 3. Python agent ---
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

echo [INFO] Starting agent... ^(Ctrl+C to stop^)
echo.
python main.py
pause