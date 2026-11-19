# chrome_cleanup.ps1
# Kill Chrome processes using the agent profile, then clean lock files.
# Called by run.bat (before launching Chrome) and stop.bat (after stopping).
# Solves the "cannot read/write profile directory" error caused by
# SingletonLock from a previous crashed or still-running Chrome instance.
#
# Why this is needed: Chrome puts an exclusive lock on the user-data-dir
# via SingletonLock/SingletonSocket/SingletonCookie/lockfile. The lock is
# held by the parent chrome.exe AND inherited by all child processes
# (renderer, GPU, network service, crashpad, etc.). The children do NOT
# carry the --user-data-dir argument, so a naive filter by commandline
# only kills the parent - the children survive and keep the lock. This
# script walks the process tree, kills the whole tree, waits for handles
# to release, then deletes lock files. If deletion still fails (profile
# corrupted), it removes the whole chrome_profile dir so Chrome can
# create a fresh one on next launch.

 $ErrorActionPreference = 'SilentlyContinue'

 $profileName = 'chrome_profile'
 $profilePath = Join-Path $PSScriptRoot $profileName

# First run - no profile dir yet, nothing to clean.
if (-not (Test-Path $profilePath)) { exit 0 }

# Recursive: collect a process and all its descendants from the snapshot.
function Get-ProcessTree {
    param($ProcessId, $AllProcesses)
    $result = @()
    $proc = $AllProcesses | Where-Object { $_.ProcessId -eq $ProcessId }
    if ($proc) { $result += $proc }
    $children = $AllProcesses | Where-Object { $_.ParentProcessId -eq $ProcessId }
    foreach ($child in $children) {
        $result += Get-ProcessTree -ProcessId $child.ProcessId -AllProcesses $AllProcesses
    }
    return $result
}

# Snapshot all chrome.exe processes once (avoid repeated CIM queries).
 $allChrome = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'"

# Roots: chrome.exe whose commandline mentions our profile name.
# These are the parent/browser processes that launched with --user-data-dir.
 $roots = $allChrome | Where-Object { $_.CommandLine -like "*$profileName*" }

# Walk each root's tree to collect all descendants (children hold lock handles).
 $toKill = @()
foreach ($root in $roots) {
    $toKill += Get-ProcessTree -ProcessId $root.ProcessId -AllProcesses $allChrome
}
# Dedupe by ProcessId (a process might be reachable via multiple paths).
 $toKill = $toKill | Sort-Object ProcessId -Unique

if ($toKill -and $toKill.Count -gt 0) {
    Write-Host "[INFO] Killing $($toKill.Count) Chrome process(es) in agent profile tree..."
    $toKill | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    # Wait for processes to actually exit and OS to release file handles.
    # 3 seconds is enough for Chrome's child self-termination on parent death.
    Start-Sleep -Seconds 3
}

# Graceful pass: delete just the lock files (preserves session, cookies, history).
 $lockFiles = @('SingletonLock','SingletonSocket','SingletonCookie','lockfile')
foreach ($f in $lockFiles) {
    $p = Join-Path $profilePath $f
    if (Test-Path $p) {
        Remove-Item -Path $p -Force -ErrorAction SilentlyContinue
    }
}

# Nuclear option: if SingletonLock still exists after kill + wait + delete,
# the profile is corrupted or a handle is stuck. Remove the whole profile
# dir so Chrome creates a fresh one on next launch. Loses session state
# but guarantees Chrome can start.
 $lockPath = Join-Path $profilePath 'SingletonLock'
if (Test-Path $lockPath) {
    Write-Host "[WARN] Lock could not be deleted - profile may be corrupted."
    Write-Host "[WARN] Removing $profilePath entirely for a fresh start."
    Remove-Item -Path $profilePath -Recurse -Force -ErrorAction SilentlyContinue
}

exit 0