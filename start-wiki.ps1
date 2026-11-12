$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host "Starting RuneScape Wiki (2011 snapshot)..." -ForegroundColor Cyan

docker compose up -d

Write-Host "Waiting for wiki to become ready..."
$maxRetries = 30
for ($i = 0; $i -lt $maxRetries; $i++) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:8090/api.php?action=query&meta=siteinfo&format=json" -TimeoutSec 2 -ErrorAction Stop
        if ($response.StatusCode -eq 200) {
            Write-Host "`nWiki is ready!" -ForegroundColor Green
            Write-Host "  Browser: http://localhost:8090" -ForegroundColor Cyan
            Write-Host "  API:     http://localhost:8090/api.php" -ForegroundColor Cyan
            exit 0
        }
    } catch {
        # Not ready yet
    }
    Write-Host "." -NoNewline
    Start-Sleep -Seconds 2
}

Write-Host "`nWiki did not start within 60 seconds. Check: docker compose -f tools/wiki/docker-compose.yml logs" -ForegroundColor Red
exit 1
