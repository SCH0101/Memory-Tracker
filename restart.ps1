# Memory Chart — safe restart (only kills THIS project's processes)
# Usage: right-click → Run with PowerShell, or from terminal: powershell -File restart.ps1

$ErrorActionPreference = "SilentlyContinue"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 80
$ngrokUrl = "jcapmemorytracker.ngrok.app"

Write-Host "`n=== Memory Chart Restart ===" -ForegroundColor Cyan

# 1. Kill ONLY memory_chart python processes (match start.py in this directory)
$pyProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'memory_chart' -or ($_.CommandLine -match 'start\.py' -and $_.CommandLine -notmatch 'cv_screening|equity_swarm') }

foreach ($p in $pyProcs) {
    # Also kill child processes (uvicorn workers spawned by this parent)
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($p.ProcessId)" 2>$null
    foreach ($child in $children) {
        Write-Host "  Stopping child PID $($child.ProcessId)" -ForegroundColor Yellow
        Stop-Process -Id $child.ProcessId -Force 2>$null
    }
    Write-Host "  Stopping PID $($p.ProcessId): $($p.CommandLine.Substring(0, [Math]::Min(80, $p.CommandLine.Length)))..." -ForegroundColor Yellow
    Stop-Process -Id $p.ProcessId -Force 2>$null
}

if (-not $pyProcs) { Write-Host "  No memory_chart Python processes found" -ForegroundColor Gray }

# 2. Kill ONLY the ngrok tunnel for THIS project (match by URL)
$ngrokProcs = Get-CimInstance Win32_Process -Filter "Name='ngrok.exe'" |
    Where-Object { $_.CommandLine -match $ngrokUrl }

foreach ($p in $ngrokProcs) {
    Write-Host "  Stopping ngrok PID $($p.ProcessId) ($ngrokUrl)" -ForegroundColor Yellow
    Stop-Process -Id $p.ProcessId -Force 2>$null
}

Start-Sleep -Seconds 2

# 3. Restart Python server
Write-Host "`n  Starting server on port $port..." -ForegroundColor Green
Set-Location $projectDir
Start-Process -FilePath "python" -ArgumentList "start.py" -WindowStyle Hidden

Start-Sleep -Seconds 3

# 4. Verify server is up
try {
    $r = Invoke-WebRequest -Uri "http://localhost:$port/health" -UseBasicParsing -TimeoutSec 5
    Write-Host "  Server OK: $($r.Content)" -ForegroundColor Green
} catch {
    Write-Host "  WARNING: Server not responding on port $port!" -ForegroundColor Red
}

# 5. Restart ngrok tunnel
Write-Host "  Starting ngrok tunnel ($ngrokUrl -> port $port)..." -ForegroundColor Green
Start-Process -FilePath "ngrok" -ArgumentList "http","--url=$ngrokUrl","$port" -WindowStyle Hidden

Start-Sleep -Seconds 4

# 6. Verify ngrok
try {
    $r = Invoke-WebRequest -Uri "https://$ngrokUrl/health" -UseBasicParsing -TimeoutSec 10
    Write-Host "  Ngrok OK: https://$ngrokUrl" -ForegroundColor Green
} catch {
    Write-Host "  WARNING: Ngrok tunnel not responding!" -ForegroundColor Red
}

# 7. Show what's still running (other projects should be untouched)
Write-Host "`n=== All running services ===" -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='ngrok.exe'" |
    Select-Object ProcessId, Name, @{N='Cmd';E={$_.CommandLine.Substring(0, [Math]::Min(100, $_.CommandLine.Length))}} |
    Format-Table -AutoSize

Write-Host "Done! Other services were NOT touched.`n" -ForegroundColor Green
