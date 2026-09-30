#!/usr/bin/env bash
# supervise.sh — 看护 scheduler.py：崩了就重启（沙箱里没有 systemd 的 Restart=）。
#
# 启动（脱离父会话，关掉终端也不受影响）：
#   setsid nohup ./supervise.sh </dev/null >/dev/null 2>&1 &
#
# 停止：
#   pkill -f supervise.sh; pkill -f scheduler.py
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_ROOT="${COLLECTOR_WORK_ROOT:-/home/agentuser/Work}"
export COLLECTOR_WORK_ROOT="$WORK_ROOT"
export HOME="$WORK_ROOT"
umask 022

LOG="$HERE/../logs/supervisor.log"
mkdir -p "$(dirname "$LOG")"

while :; do
  echo "$(date -u +%FT%TZ) 拉起 scheduler.py" >>"$LOG"
  /usr/bin/python3 "$HERE/scheduler.py" >>"$LOG" 2>&1
  rc=$?
  if [ "$rc" = "0" ]; then
    echo "$(date -u +%FT%TZ) scheduler 正常退出，不再重启" >>"$LOG"
    break
  fi
  echo "$(date -u +%FT%TZ) scheduler 异常退出 rc=$rc，5s 后重启" >>"$LOG"
  sleep 5
done
