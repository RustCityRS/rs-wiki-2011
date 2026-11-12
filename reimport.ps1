<#
.SYNOPSIS
    Re-import the local wiki after `scrape_pages.py` has retitled the snapshot.

.DESCRIPTION
    `import.ps1` builds the wiki from empty. This one updates a wiki that is
    already populated, after phase 3 of `scrape_pages.py` has rewritten
    `pages.xml` so each page carries the title it had at the 2011 cutoff.

    The delete step is the part that is not optional and not obvious.
    MediaWiki's `importDump` ADDS pages: importing the retitled dump over an
    existing import leaves both names in place, so ~19,500 pages end up
    duplicated under a 2011 title and a modern one. The pages whose titles
    changed have to be deleted first.

    Nothing is lost by that. Every deleted title is either re-created by the
    import under a page that kept it, or taken over by the page that held it in
    2011 -- the two directions look alarming together (465 titles are both
    "stale" and an import target) and are simply swaps: the page at `Backpack`
    comes back as `Inventory`, while `Ruined backpack (Zogre Flesh Eaters)`
    becomes `Backpack`. And all of it is re-importable from `pages.xml`.

    Images are deliberately untouched. They live in the `wiki-images` volume,
    the retitling renamed pages and not files, and re-running `importImages`
    over 5.8 GB would cost hours to change nothing.

.PARAMETER SkipDelete
    Import without deleting first. Only correct on a wiki that has never been
    imported into -- otherwise it is how you get the duplicates described above.

.EXAMPLE
    ./reimport.ps1
    ./reimport.ps1 -LogDir D:\logs
#>
[CmdletBinding()]
param(
    [string]$DataDir = (Join-Path $PSScriptRoot 'data'),
    [string]$LogDir = (Join-Path $PSScriptRoot 'data\reimport-logs'),
    [switch]$SkipDelete
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$db = Join-Path $DataDir 'staging.db'
$xml = Join-Path $DataDir 'pages.xml'
$staleList = Join-Path $DataDir 'stale_titles.txt'

foreach ($f in @($db, $xml)) {
    if (-not (Test-Path $f)) { throw "missing $f - run scrape_pages.py first" }
}
New-Item -ItemType Directory -Force $LogDir | Out-Null
$runLog = Join-Path $LogDir 'run.log'

Write-Host '=== Local wiki re-import ===' -ForegroundColor Cyan

# The whole point of this script is the 2011 titles. Without phase 3 the dump
# still carries modern ones, nothing would be stale, and this would import the
# very thing it is meant to replace -- so check rather than assume.
$retitled = (& py -c @"
import sqlite3, sys
con = sqlite3.connect(r'$db')
cols = {r[1] for r in con.execute('PRAGMA table_info(revisions)')}
if 'title_at_cutoff' not in cols:
    print(-1)
else:
    print(con.execute('SELECT COUNT(*) FROM revisions WHERE title_at_cutoff <> title').fetchone()[0])
"@) | Select-Object -Last 1

if ([int]$retitled -lt 0) {
    throw "staging.db has no title_at_cutoff column - run: py scrape_pages.py --retitle-only --captured-at <when the content was fetched>"
}
Write-Host "  $retitled pages carry a restored 2011 title" -ForegroundColor Gray

if (-not $SkipDelete) {
    if ([int]$retitled -eq 0) {
        throw "no page titles differ from the snapshot's - nothing to re-import. Use -SkipDelete if you meant to import into an empty wiki."
    }

    # Regenerated here rather than kept as a file: the list is derived from the
    # database, and a stale copy of it would delete the wrong pages.
    & py -c @"
import sqlite3
con = sqlite3.connect(r'$db')
rows = con.execute('SELECT title FROM revisions WHERE title_at_cutoff <> title ORDER BY title').fetchall()
with open(r'$staleList', 'w', encoding='utf-8', newline='\n') as f:
    for (t,) in rows:
        f.write(t + '\n')
print(f'wrote {len(rows)} stale titles')
"@

    Write-Host "`n[1/4] Deleting pages whose title changed..." -ForegroundColor Yellow
    "=== [1/4] deleteBatch === $(Get-Date -Format o)" | Tee-Object $runLog | Out-Null
    docker compose exec -T mediawiki php maintenance/run.php deleteBatch `
        --r 'Retitled to 2011 snapshot name' /import/stale_titles.txt *>&1 |
        Tee-Object (Join-Path $LogDir 'delete.log') |
        Select-String -Pattern 'Deleted!' -NotMatch |
        Tee-Object -Append $runLog
} else {
    Write-Host "`n[1/4] Delete skipped (-SkipDelete)" -ForegroundColor Gray
}

Write-Host "`n[2/4] Importing pages (30-60 min)..." -ForegroundColor Yellow
"=== [2/4] importDump === $(Get-Date -Format o)" | Tee-Object -Append $runLog | Out-Null
docker compose exec -T mediawiki php maintenance/run.php importDump --no-updates /import/pages.xml *>&1 |
    Tee-Object (Join-Path $LogDir 'import.log') | Select-Object -Last 20 | Tee-Object -Append $runLog

# `--no-updates` above skips link-table maintenance per page, which is what
# makes the import bearable; this puts the links back in one pass.
#
# `refreshLinks` rather than `rebuildall`, deliberately. `rebuildall` runs
# `rebuildtextindex` FIRST, and on this MariaDB the searchindex table has no
# si_title/si_text indexes to drop, so it dies with "Error 1091: Can't DROP
# INDEX si_text" -- and takes the link rebuild down with it, because it never
# gets that far. That failure is silent in the worst way: the script exits 0
# and the wiki looks imported while What-links-here and every category listing
# are empty.
Write-Host "`n[3/4] Rebuilding link tables..." -ForegroundColor Yellow
"=== [3/4] refreshLinks === $(Get-Date -Format o)" | Tee-Object -Append $runLog | Out-Null
docker compose exec -T mediawiki php maintenance/run.php refreshLinks *>&1 |
    Tee-Object (Join-Path $LogDir 'rebuild.log') | Select-Object -Last 10 | Tee-Object -Append $runLog
if ($LASTEXITCODE -ne 0) {
    Write-Host "  refreshLinks reported a failure - see rebuild.log" -ForegroundColor Red
    "  refreshLinks FAILED" | Tee-Object -Append $runLog
}

Write-Host "`n[4/4] Updating site statistics..." -ForegroundColor Yellow
"=== [4/4] initSiteStats === $(Get-Date -Format o)" | Tee-Object -Append $runLog | Out-Null
docker compose exec -T mediawiki php maintenance/run.php initSiteStats --update *>&1 |
    Tee-Object -Append $runLog

Write-Host "`n=== Verifying ===" -ForegroundColor Cyan
try {
    $stats = (Invoke-RestMethod -TimeoutSec 10 -Uri `
        'http://localhost:8090/api.php?action=query&meta=siteinfo&siprop=statistics&format=json').query.statistics
    Write-Host "  Pages: $($stats.pages)   Images: $($stats.images)" -ForegroundColor Green

    # A 2011 title that only exists if the retitling landed, and the modern one
    # it replaced. Checking both is what distinguishes "imported" from
    # "imported correctly".
    foreach ($pair in @(@('Death Plateau', 'Death Plateau (historical)'),
                        @('Varrock Tasks', 'Varrock achievements'))) {
        $q = (Invoke-RestMethod -TimeoutSec 10 -Uri `
            ("http://localhost:8090/api.php?action=query&format=json&titles=" +
             [uri]::EscapeDataString($pair[0]) + '|' + [uri]::EscapeDataString($pair[1])))
        foreach ($p in $q.query.pages.PSObject.Properties.Value) {
            $want = if ($p.title -eq $pair[0]) { 'present' } else { 'gone' }
            $got = if ($p.PSObject.Properties.Name -contains 'missing') { 'gone' } else { 'present' }
            $colour = if ($got -eq $want) { 'Green' } else { 'Red' }
            Write-Host "  $($p.title): $got (want $want)" -ForegroundColor $colour
        }
    }
    Write-Host "`nWiki ready at http://localhost:8090" -ForegroundColor Green
} catch {
    Write-Host "  Could not reach the wiki API: $_" -ForegroundColor Red
}
