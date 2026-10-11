#!/usr/bin/env pwsh
# sync-oracle.ps1 — 把本地 origin/main 同步到 Oracle 并跑 smoke 测试
#
# 前置：
#   1. Oracle SSH 可达（ping 140.29.97.78 不超时）
#   2. ~/.ssh/config 有对应别名（或直接改下面的 $OracleHost）
#   3. Oracle 端用户有 sudo 权限可重启 league-web.service
#
# 用法：
#   pwsh scripts/sync-oracle.ps1
#   pwsh scripts/sync-oracle.ps1 -Host ubuntu@140.29.97.78
#   pwsh scripts/sync-oracle.ps1 -SkipTests  # 仅同步不跑测试

param(
    [string]$OracleHost = "ubuntu@140.29.97.78",
    [string]$OracleRepo = "/home/ubuntu/DSH/sports-lottery",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

Step "1/5 连通性检查"
$ping = Test-NetConnection -ComputerName ($OracleHost -replace ".*@","") -Port 22 -WarningAction SilentlyContinue
if (-not $ping.TcpTestSucceeded) {
    Write-Host "[FAIL] Oracle $OracleHost TCP 22 不可达，请检查：" -ForegroundColor Red
    Write-Host "  - VPN 是否连上（公司内网 / 云服务器控制台）"
    Write-Host "  - IP 是否变化（上次成功 IP: 140.29.97.78）"
    Write-Host "  - Oracle 端 systemctl status sshd"
    exit 1
}
Write-Host "[OK] TCP 22 open"

Step "2/5 本地 origin/main 确认"
$localHead = git rev-parse main
$originHead = git rev-parse origin/main
if ($localHead -ne $originHead) {
    Write-Host "[FAIL] 本地 main ($localHead) ≠ origin/main ($originHead) — 先 git push" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] 本地 = origin/main = $localHead"

Step "3/5 推送到 Oracle"
$branch = git rev-parse --abbrevShort main
ssh $OracleHost "cd $OracleRepo && git fetch origin && git checkout main && git reset --hard origin/$branch"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] Oracle git reset 失败" -ForegroundColor Red
    exit 1
}
$oracleHead = ssh $OracleHost "cd $OracleRepo && git rev-parse HEAD"
Write-Host "[OK] Oracle HEAD = $oracleHead"

Step "4/5 Oracle 端服务重启"
ssh $OracleHost "sudo systemctl restart league-web.service"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] 服务重启返回非 0（可能无 sudo 权限），继续..." -ForegroundColor Yellow
}
Start-Sleep -Seconds 2
$health = ssh $OracleHost "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8077/health"
if ($health -ne "200") {
    Write-Host "[FAIL] /health 返回 $health，检查 league-web.service 日志" -ForegroundColor Red
    ssh $OracleHost "sudo journalctl -u league-web.service --no-pager -n 25"
    exit 1
}
Write-Host "[OK] /health 200"

Step "5/5 Oracle 端 smoke 测试"
if ($SkipTests) {
    Write-Host "[SKIP] -SkipTests"
} else {
    ssh $OracleHost "cd $OracleRepo && python -m pytest tests/test_sources_router.py tests/test_m3_jobs.py -q 2>&1 | tail -5"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] Oracle 端测试失败，请检查上方输出" -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK] Oracle 端 sources_router + m3_jobs 测试通过"
}

Write-Host "`n=== 同步完成 ===" -ForegroundColor Green
Write-Host "本地 $($localHead) == Oracle $($oracleHead) == origin/main"
Write-Host "服务健康：/health 200"