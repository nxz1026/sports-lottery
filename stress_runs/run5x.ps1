$ErrorActionPreference = 'Continue'
Set-Location E:\2026Workplace\Code\sports-lottery
$logdir = ".\stress_runs"
if (-not (Test-Path $logdir)) { New-Item -ItemType Directory -Path $logdir | Out-Null }

$summaryLines = @()
$summaryLines += "5x pytest stress test @ HEAD=4074457"
$summaryLines += ("started: {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))

for ($i = 1; $i -le 5; $i++) {
    $summaryLines += ("--- run {0} ---" -f $i)
    $start = Get-Date
    $out = python -m pytest -q --no-header 2>&1 | Out-String
    $end = Get-Date
    $duration = ($end - $start).TotalSeconds
    $summaryLines += ("duration: {0:N1}s" -f $duration)
    $tail = ($out -split "`n" | Select-Object -Last 3) -join "`n"
    $summaryLines += $tail
    $summaryLines += ""
}

$summaryLines += ("finished: {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
$content = ($summaryLines -join "`n")
$content | Out-File -FilePath ".\stress_runs\5x_summary.txt" -Encoding utf8
Write-Output $content
