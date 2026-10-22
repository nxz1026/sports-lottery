# disable_collector_tasks.ps1 — 停用本机 5 个采集计划任务（需管理员）
# 由提权进程调用；结果写入 %TEMP%\disable_collector_tasks.result 供调用方核验。
$ErrorActionPreference = 'Continue'
$names = @('collector_0930', 'collector_1530', 'collector_2130', 'collector_2330', 'collector_offer_10m')
$out = @()
foreach ($n in $names) {
  try {
    Disable-ScheduledTask -TaskName $n -ErrorAction Stop | Out-Null
    $t = Get-ScheduledTask -TaskName $n
    $out += ("{0}|disabled={1}|state={2}" -f $n, (-not $t.Settings.Enabled), $t.State)
  } catch {
    $out += ("{0}|ERROR={1}" -f $n, $_.Exception.Message)
  }
}
$out | Set-Content -Path (Join-Path $env:TEMP 'disable_collector_tasks.result') -Encoding UTF8
