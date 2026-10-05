$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$dataDir = Join-Path $root "data"
$log = Join-Path $dataDir "run.log"
$pidFile = Join-Path $dataDir "run.pid"
$totalFile = Join-Path $dataDir "all-topics.json"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Set-Content -Path $pidFile -Value $PID -Encoding utf8

$total = 0
if (Test-Path $totalFile) {
    $total = (Get-Content $totalFile -Raw | ConvertFrom-Json).Count
}

$prev = -1
$stagnant = 0

for ($pass = 1; $pass -le 50; $pass++) {
    $start = (Get-Date).ToString("s")
    Add-Content -Path $log -Value "=== pass $pass start $start (pid $PID) ===" -Encoding utf8

    & python -u archive.py --config config.yml --max-topics 0 --seed 20261004 2>&1 |
        Out-File -FilePath $log -Append -Encoding utf8

    $done = (Get-ChildItem -Path (Join-Path $dataDir "topics") -Directory | Measure-Object).Count
    $end = (Get-Date).ToString("s")
    Add-Content -Path $log -Value "=== pass $pass end $end done=$done total=$total ===" -Encoding utf8

    if ($total -gt 0 -and $done -ge $total) { break }
    if ($done -le $prev) { $stagnant++ } else { $stagnant = 0 }
    $prev = $done
    if ($stagnant -ge 2) {
        Add-Content -Path $log -Value "no progress for $stagnant passes, stopping" -Encoding utf8
        break
    }
    Start-Sleep -Seconds 15
}

Add-Content -Path $log -Value "=== runner finished ===" -Encoding utf8
Remove-Item -Path $pidFile -ErrorAction SilentlyContinue
