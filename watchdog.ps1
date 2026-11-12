# Keeps the local 2011 wiki answering on :8090. Run every few minutes by the
# "RS wiki watchdog" scheduled task (register-watchdog.ps1). Each run: if the
# API answers, nothing; if Docker Desktop is not running, start it; then bring
# the stack up, and if the web container still does not answer, recreate it.
# It never restarts a running Docker Desktop (that would drop rs-postgres);
# a wedged engine is logged for a person to restart.

$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot
$log = Join-Path $PSScriptRoot 'watchdog.log'
$api = 'http://localhost:8090/api.php?action=query&meta=siteinfo&format=json'

function Log($msg) {
    $line = "$(Get-Date -Format s) $msg"
    Add-Content -Path $log -Value $line
    # Keep the log short.
    $lines = Get-Content $log -Tail 500
    if ($lines.Count -ge 500) { Set-Content -Path $log -Value $lines }
}

function WikiUp {
    try {
        (Invoke-WebRequest -Uri $api -TimeoutSec 10 -UseBasicParsing).StatusCode -eq 200
    } catch { $false }
}

# Runs docker with a time limit, since a wedged engine hangs every call.
function Docker([string[]]$argv, [int]$seconds = 120) {
    $p = Start-Process -FilePath docker -ArgumentList $argv -NoNewWindow -PassThru `
        -RedirectStandardOutput "$env:TEMP\wiki-watchdog.out" -RedirectStandardError "$env:TEMP\wiki-watchdog.err"
    if (-not $p.WaitForExit($seconds * 1000)) {
        try { $p.Kill() } catch {}
        return $null
    }
    return $p.ExitCode
}

if (WikiUp) { exit 0 }
Log 'wiki not answering'

if (-not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
    Log 'Docker Desktop not running: starting it'
    Docker @('desktop', 'start') 300 | Out-Null
}

if ($null -eq (Docker @('info') 60)) {
    Log 'docker engine not answering (wedged?): restart Docker Desktop by hand'
    exit 1
}

Docker @('compose', 'up', '-d') 180 | Out-Null
for ($i = 0; $i -lt 12; $i++) { if (WikiUp) { Log 'up after compose up'; exit 0 }; Start-Sleep 5 }

Log 'still down: recreating the web container'
$rc = Docker @('compose', 'up', '-d', '--force-recreate', 'mediawiki') 180
if ($null -eq $rc) { Log 'recreate hung: the engine is wedged, restart Docker Desktop by hand'; exit 1 }
for ($i = 0; $i -lt 12; $i++) { if (WikiUp) { Log 'up after recreate'; exit 0 }; Start-Sleep 5 }
Log 'still down after recreate'
exit 1
