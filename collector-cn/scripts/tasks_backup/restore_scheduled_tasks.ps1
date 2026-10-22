# restore_scheduled_tasks.ps1 — 恢复 5 个采集计划任务（2026-10-22 切手动模式前备份）
#
# 用法（在**管理员** PowerShell 里跑）：
#   powershell -ExecutionPolicy Bypass -File E:\2026Workplace\Code\collector-cn\scripts\tasks_backup\restore_scheduled_tasks.ps1
#
# 备份 XML 与本脚本同目录（20261022-manual-mode\）⇒ 恢复出来的任务定义与停用前**逐字一致**
# （含 Daily+Boot+Logon 触发器、StartWhenAvailable、指向 collector_silent.vbs 的 action）。
#
# 切手动模式的原因（2026-10-22 实测）：
#   采集机是工作电脑，不是 24h 开机；近 7 天实测 117 次相邻批比较里 **54% 的批次完全空转**
#   （采回来和上一批一模一样），07–09 时共 36 批只有 1 批有产出（2.8%）。
#   ⇒ 改手动：一键脚本跑完「采集 → 推送 → 远端装载 → 出荐单」，看数据时再跑。
#
# ⚠️ 恢复前请先确认 scripts\refresh_now.bat 能跑通（手动模式是主路径）。
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $dir '20261022-manual-mode'
if (-not (Test-Path $src)) {
  Write-Error "找不到备份目录：$src"
  exit 1
}
$names = @('collector_0930', 'collector_1530', 'collector_2130', 'collector_2330', 'collector_offer_10m')
foreach ($n in $names) {
  $xml = Join-Path $src "$n.xml"
  if (-not (Test-Path $xml)) { Write-Error "缺 XML：$xml"; exit 1 }
  Register-ScheduledTask -TaskName $n -Xml (Get-Content -Raw -Encoding UTF8 $xml) -Force | Out-Null
  Enable-ScheduledTask -TaskName $n | Out-Null
  Write-Host "已恢复并启用：$n"
}
Write-Host ''
Write-Host '5 个采集计划任务已恢复（Daily+Boot+Logon 触发器，含 StartWhenAvailable 漏档补跑）。'
Write-Host '恢复后请先跑一次 scripts\collect_batch.bat night 验证采集链路。'
