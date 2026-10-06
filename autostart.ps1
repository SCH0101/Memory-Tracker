# Memory Chart - auto-start server + ngrok on port 80
# Registered as a Windows Scheduled Task to run at logon.
# Idempotent: skips if already running.

$ErrorActionPreference = "SilentlyContinue"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 80
$ngrokUrl = "jcapmemorytracker.ngrok.app"
# Absolute path - scheduled tasks can resolve `python` to the broken
# WindowsApps store stub depending on PATH ordering.
$python = "C:\Users\mchso\AppData\Local\Programs\Python\Python313\python.exe"
$logDir = Join-Path $projectDir "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir "autostart.log"

function _Log($msg) {
    Add-Content -Path $log -Value "[$(Get-Date -Format o)] $msg" -Encoding utf8
}

if (-not (Test-Path $python)) {
    _Log "autostart: python missing at $python - exiting"
    return
}

# 1. Only start server if port 80 is not already in use
try {
    $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop
    if ($busy) {
        _Log "autostart: port $port already in use (PID $(($busy | Select-Object -First 1).OwningProcess)), skipping server"
    }
} catch {
    # Port free - start the server
    _Log "autostart: launching server on port $port"
    Set-Location $projectDir
    Start-Process -FilePath $python -ArgumentList "start.py" -WindowStyle Hidden
    _Log "autostart: server process started"

    # Wait for server to be ready
    $retries = 0
    while ($retries -lt 10) {
        Start-Sleep -Seconds 2
        try {
            $r = Invoke-WebRequest -Uri "http://localhost:$port/health" -UseBasicParsing -TimeoutSec 3
            _Log "autostart: server healthy after $($retries * 2)s"
            break
        } catch {
            $retries++
        }
    }
    if ($retries -ge 10) { _Log "autostart: WARNING server not responding after 20s" }
}

# 2. Only start ngrok if not already running for this URL
$ngrokRunning = Get-CimInstance Win32_Process -Filter "Name='ngrok.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match $ngrokUrl }

if ($ngrokRunning) {
    _Log "autostart: ngrok tunnel already running for $ngrokUrl"
} else {
    _Log "autostart: starting ngrok tunnel $ngrokUrl -> port $port"
    Start-Process -FilePath "ngrok" -ArgumentList "http","--url=$ngrokUrl","$port" -WindowStyle Hidden
    _Log "autostart: ngrok process started"
}
