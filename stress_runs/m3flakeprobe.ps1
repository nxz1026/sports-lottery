$ErrorActionPreference = 'Continue'
Set-Location E:\2026Workplace\Code\sports-lottery
$pass = 0; $fail = 0; $traces = @()
for ($i = 1; $i -le 10; $i++) {
    $out = python -m pytest tests/web/test_m3_jobs.py::test_auto_refresh_trigger_and_dedup -q 2>&1 | Out-String
    if ($out -match "1 passed") {
        $pass++
    } elseif ($out -match "1 failed") {
        $fail++
        $traces += "--- run $i FAILED ---"
        $traces += ($out -split "`n" | Select-Object -First 50)
    }
}
Write-Output "RESULT: pass=$pass fail=$fail of 10"
foreach ($t in $traces) { Write-Output $t }
