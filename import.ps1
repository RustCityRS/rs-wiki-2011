$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host "=== RuneScape Wiki Import ===" -ForegroundColor Cyan

# Check containers are running
$status = docker compose ps --format json 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker containers not running. Start with: docker compose -f tools/wiki/docker-compose.yml up -d" -ForegroundColor Red
    exit 1
}

# Wait for DB to be healthy
Write-Host "`n[1/7] Waiting for database..." -ForegroundColor Yellow
$maxWait = 60
for ($i = 0; $i -lt $maxWait; $i++) {
    $health = docker compose exec db healthcheck.sh --connect --innodb_initialized 2>&1
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 2
}

# Run MediaWiki database setup/update
Write-Host "`n[2/7] Initializing MediaWiki database..." -ForegroundColor Yellow
docker compose exec mediawiki php maintenance/run.php update --quick 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Trying legacy maintenance path..." -ForegroundColor Gray
    docker compose exec mediawiki php maintenance/update.php --quick 2>&1
}

# Import XML dump
if (Test-Path "data/pages.xml") {
    Write-Host "`n[3/7] Importing page dump (this takes 30-60 min)..." -ForegroundColor Yellow
    docker compose exec mediawiki php maintenance/run.php importDump --no-updates /import/pages.xml 2>&1
    if ($LASTEXITCODE -ne 0) {
        docker compose exec mediawiki php maintenance/importDump.php --no-updates /import/pages.xml 2>&1
    }
} else {
    Write-Host "`n[3/7] No pages.xml found, skipping page import" -ForegroundColor Gray
}

# Import images
if (Test-Path "data/images" -PathType Container) {
    $imageCount = (Get-ChildItem "data/images" -File | Measure-Object).Count
    if ($imageCount -gt 0) {
        Write-Host "`n[4/7] Importing $imageCount images..." -ForegroundColor Yellow
        docker compose exec mediawiki php maintenance/run.php importImages /import/images --search-recursively --overwrite 2>&1
        if ($LASTEXITCODE -ne 0) {
            docker compose exec mediawiki php maintenance/importImages.php /import/images --search-recursively --overwrite 2>&1
        }
    } else {
        Write-Host "`n[4/7] No images found, skipping" -ForegroundColor Gray
    }
} else {
    Write-Host "`n[4/7] No images directory, skipping" -ForegroundColor Gray
}

# Rebuild indexes
Write-Host "`n[5/7] Rebuilding link tables..." -ForegroundColor Yellow
docker compose exec mediawiki php maintenance/run.php rebuildall 2>&1
if ($LASTEXITCODE -ne 0) {
    docker compose exec mediawiki php maintenance/rebuildall.php 2>&1
}

Write-Host "`n[6/7] Rebuilding search index..." -ForegroundColor Yellow

# MediaWiki 1.43 on MariaDB 10.11 creates `searchindex` with a FULLTEXT index on
# si_title and NONE on si_text. That breaks search two ways at once: queries
# against the text column have no index, and the repair path cannot run either,
# because `rebuildtextindex` opens with
#     ALTER TABLE searchindex DROP INDEX si_title, DROP INDEX si_text
# which dies with "Error 1091: Can't DROP INDEX si_text". Worse, that same
# failure is what `rebuildall` hits first, so it aborts before rebuilding the
# link tables and still exits 0 -- a wiki that looks imported with empty
# What-links-here and empty categories.
#
# Adding the index is safe at any time: searchindex is derived entirely from
# page content, which is why rebuildtextindex's own next step is to clear it.
$hasSiText = docker compose exec -T db mariadb -u wiki -p"$($env:MW_DB_PASSWORD ?? 'wiki_pw')" rswiki -N -B -e `
    "SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema='rswiki' AND table_name='searchindex' AND index_name='si_text';" 2>$null
if ("$hasSiText".Trim() -eq '0') {
    Write-Host "  searchindex is missing its si_text fulltext index; adding it" -ForegroundColor Gray
    docker compose exec -T db mariadb -u wiki -p"$($env:MW_DB_PASSWORD ?? 'wiki_pw')" rswiki `
        -e "ALTER TABLE searchindex ADD FULLTEXT INDEX si_text (si_text);" 2>&1
}

docker compose exec mediawiki php maintenance/run.php rebuildtextindex 2>&1
if ($LASTEXITCODE -ne 0) {
    docker compose exec mediawiki php maintenance/rebuildtextindex.php 2>&1
}

Write-Host "`n[7/7] Updating site statistics..." -ForegroundColor Yellow
docker compose exec mediawiki php maintenance/run.php initSiteStats --update 2>&1
if ($LASTEXITCODE -ne 0) {
    docker compose exec mediawiki php maintenance/initSiteStats.php --update 2>&1
}

# Run pending jobs
Write-Host "`nRunning pending jobs..." -ForegroundColor Yellow
docker compose exec mediawiki php maintenance/run.php runJobs --maxjobs 10000 2>&1
if ($LASTEXITCODE -ne 0) {
    docker compose exec mediawiki php maintenance/runJobs.php --maxjobs 10000 2>&1
}

# Verify
Write-Host "`n=== Verifying ===" -ForegroundColor Cyan
try {
    $response = Invoke-RestMethod -Uri "http://localhost:8090/api.php?action=query&meta=siteinfo&siprop=statistics&format=json" -TimeoutSec 5
    $stats = $response.query.statistics
    Write-Host "  Pages: $($stats.pages)" -ForegroundColor Green
    Write-Host "  Articles: $($stats.articles)" -ForegroundColor Green
    Write-Host "  Images: $($stats.images)" -ForegroundColor Green
    Write-Host "`nWiki ready at http://localhost:8090" -ForegroundColor Green
} catch {
    Write-Host "  Could not verify wiki API" -ForegroundColor Red
}
