$ErrorActionPreference = 'Continue'
Set-Location E:\2026Workplace\Code\sports-lottery
$logdir = ".\stress_runs"
if (-not (Test-Path $logdir)) { New-Item -ItemType Directory -Path $logdir | Out-Null }

# R16 #2: 动态取 HEAD 短哈希（之前硬编码 4074457 是 R12 时写下的，HEAD 现已
# 1cb7e4d —— 每次跑仍写旧 commit 哈希到 5x_summary.txt，误导运维以为测试在
# 4074457 上跑过）。
$headShort = (& git rev-parse --short HEAD).Trim()

$summaryLines = @()
$summaryLines += ("5x pytest stress test @ HEAD={0}" -f $headShort)
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
