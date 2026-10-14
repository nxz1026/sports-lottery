# sports-lottery pre-push hook 安装脚本
# 设置 git core.hooksPath 让 git push 走仓内 stress_runs/hooks/ 的版本。
#
# 用法（在仓库根）：
#   pwsh stress_runs/install-pre-push.ps1
#
# 卸载：
#   pwsh stress_runs/install-pre-push.ps1 -Uninstall
#
# 跳过 hook：
#   $env:SPORTS_LOTTERY_SKIP_PUSH_HOOK = "1"
#   git push ...
#
# 设计：仓库 .git/hooks/ 不入版本库；用 git config core.hooksPath 把仓内
# stress_runs/hooks/ 接管。重复运行幂等。

[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$repoRoot = & git rev-parse --show-toplevel 2>$null
if (-not $repoRoot) {
    Write-Error "不在 git 仓库中。请在 sports-lottery 仓库根目录运行。"
    exit 1
}
Set-Location $repoRoot

if ($Uninstall) {
    git config --unset core.hooksPath 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "core.hooksPath 未设置，无需卸载。"
        exit 0
    }
    Write-Host "✅ 已卸载 pre-push hook。git push 不再自动跑 5x 压测。"
    exit 0
}

# 装挂前 sanity 检查：仓内 hook 文件存在
$hookSource = Join-Path $repoRoot "stress_runs/hooks/pre-push"
if (-not (Test-Path $hookSource)) {
    Write-Error "未找到 hook 源：$hookSource"
    exit 1
}

# 设置 core.hooksPath（相对路径）
$relativePath = "stress_runs/hooks"
git config core.hooksPath $relativePath
if ($LASTEXITCODE -ne 0) {
    Write-Error "git config core.hooksPath 失败"
    exit 1
}

# 确认装挂
$effectivePath = & git config --get core.hooksPath
Write-Host ""
Write-Host "✅ pre-push hook 已装挂（core.hooksPath=$effectivePath）。"
Write-Host ""
Write-Host "从此 git push 之前会自动跑 5x pytest 压测："
Write-Host "  - 5/5 全过 → 放行 push"
Write-Host "  - 任一次 fail → 阻断 push（exit 1）"
Write-Host ""
Write-Host "跳过（CI / 紧急 push）："
Write-Host '  $env:SPORTS_LOTTERY_SKIP_PUSH_HOOK = "1"; git push'
Write-Host ""
Write-Host "测试文件范围：retrain + ml_feedback + observability + predict_log_error 共 12 个文件。"
Write-Host "完整 pytest 5x 仍可用 stress_runs/run5x.ps1（手动版）。"