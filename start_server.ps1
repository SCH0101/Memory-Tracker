# Starts the Memory Chart server (port 80). Used by the MemoryChartServer scheduled task.
$root = "C:\Users\mchso\OneDrive - Jebsen & Co Ltd\Desktop\Agg"
$log = Join-Path $root "memory_chart\data\server.log"
Set-Location $root

# Skip if already running
$existing = Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Add-Content $log "$(Get-Date -Format s) start_server.ps1: port 80 already in use - skipping"
    exit 0
}

Add-Content $log "$(Get-Date -Format s) start_server.ps1: starting uvicorn"
& python -m uvicorn memory_chart.backend.api.server:app --host 0.0.0.0 --port 80 *>> $log
