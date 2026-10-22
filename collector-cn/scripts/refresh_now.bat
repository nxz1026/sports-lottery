@echo off
rem ============================================================================
rem refresh_now.bat - one-click on-demand refresh (manual mode, 2026-10-22)
rem
rem Pipeline (the whole loop in one shot):
rem   [1/4] collect + push  (collector.py --push-batch, tar stream to Oracle)
rem   [2/4] remote ingest   (bash ~/bin/jc-ingest-run.sh  -> fact.jc_*)
rem   [3/4] remote recommend (scripts/decision/recommend.py -> analysis.ev_report)
rem   [4/4] remote settle    (scripts/decision/settle.py     -> analysis.rec_settle)
rem
rem Usage:  refresh_now.bat [fast|full] [nosettle]
rem   default  = full  (8 topics incl. jc_odds_history) + settle
rem   fast     = 7 topics (skip jc_odds_history; saves ~60s, but the odds-move
rem             view will not get a new history point)
rem   nosettle = skip step [4/4]
rem
rem Why manual (measured 2026-10-22, 7 days, 117 adjacent-batch comparisons):
rem   54% of batches were byte-identical duplicates; 07-09h had 36 batches with
rem   only 1 productive (2.8%). The collector host is a work PC, not 24/7.
rem
rem NOTE: keep this file ASCII-only (cmd reads .bat in the OEM codepage).
rem       Chinese docs live in collector-cn\README.md.
rem
rem IMPORTANT: step [3/4] refuses to write a recommendation when the pricing
rem   odds snapshot is older than 6h (the freshness gate in recommend.py).
rem   That is by design: if step [1/4] or [2/4] silently failed, you get an
rem   explicit refusal instead of a recommendation priced with stale odds.
rem ============================================================================
setlocal EnableDelayedExpansion
cd /d E:\2026Workplace\Code\collector-cn
if not exist logs mkdir logs
set PY=C:\Python314\python.exe
set LOG=logs\refresh_now.log

set MODE=full
set DO_SETTLE=1
if /i "%~1"=="fast" set MODE=fast
if /i "%~1"=="nosettle" set DO_SETTLE=0
if /i "%~2"=="nosettle" set DO_SETTLE=0

echo ============================================================
echo  refresh_now  mode=%MODE%  settle=%DO_SETTLE%
echo  log: %LOG%
echo ============================================================
echo [1/4] collect + push ... (this takes 1-3 min)
if /i "%MODE%"=="fast" goto step1fast
%PY% collector.py --push-batch jczq_offer jclq_offer jczq_result jclq_result jc_issue jc_issue_result lottery_draw jc_odds_history --mode-log refresh >>%LOG% 2>&1
if errorlevel 1 goto fail_collect
goto step2

:step1fast
%PY% collector.py --push-batch jczq_offer jclq_offer jczq_result jclq_result jc_issue jc_issue_result lottery_draw --mode-log refresh >>%LOG% 2>&1
if errorlevel 1 goto fail_collect

:step2
echo [2/4] remote ingest (bash ~/bin/jc-ingest-run.sh) ...
ssh oracle "bash ~/bin/jc-ingest-run.sh" >>%LOG% 2>&1
if errorlevel 1 goto fail_ingest

:step3
echo [3/4] remote recommend (scripts/decision/recommend.py) ...
ssh oracle "cd /home/ubuntu/DSH/sports-lottery && PYTHONPATH=scripts /home/ubuntu/.venvs/league/bin/python scripts/decision/recommend.py" >>%LOG% 2>&1
if errorlevel 1 goto fail_recommend

:step4
if "%DO_SETTLE%"=="0" goto done
echo [4/4] remote settle (scripts/decision/settle.py) ...
ssh oracle "cd /home/ubuntu/DSH/sports-lottery && PYTHONPATH=scripts /home/ubuntu/.venvs/league/bin/python scripts/decision/settle.py" >>%LOG% 2>&1
if errorlevel 1 goto fail_settle
goto done

:fail_collect
echo [1/4] FAILED - collect/push error. See %LOG%
echo.
pause
exit /b 1

:fail_ingest
echo [2/4] FAILED - remote ingest error. See %LOG%
echo.
pause
exit /b 1

:fail_recommend
echo [3/4] recommend refused or failed. This is expected if the pricing odds
echo        snapshot is older than 6h (freshness gate). See %LOG% for the reason.
echo.
pause
exit /b 3

:fail_settle
echo [4/4] settle failed. See %LOG%
echo.
pause
exit /b 1

:done
echo.
echo ============================================================
echo  DONE. tail of %LOG%:
echo ============================================================
powershell -NoProfile -Command "Get-Content -Tail 25 -Encoding utf8 '%LOG%'"
echo.
echo Open the dashboard to look at the data.
echo.
pause
exit /b 0
