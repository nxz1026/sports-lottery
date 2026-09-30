#!/usr/bin/env bash
# status.sh — 采集机健康自检（沙箱里没有 systemd/cron，手动跑这个代替 systemctl status）
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CN_DIR="$(dirname "$HERE")"
LOGS="$CN_DIR/logs"

echo "=== 时间 ==="
date -u +'UTC   %F %T'
TZ=Asia/Shanghai date +'北京  %F %T'

echo
echo "=== 进程 ==="
ps -eo pid,etime,args 2>/dev/null | grep -E 'supervise\.sh|scheduler\.py|collector\.py' | grep -v grep || echo "(无)"

echo
echo "=== 心跳（scheduler 每 20s 刷一次，超过 2 分钟即视为挂了）==="
if [ -f "$LOGS/scheduler.heartbeat" ]; then
  cat "$LOGS/scheduler.heartbeat"
  hb=$(stat -c %Y "$LOGS/scheduler.heartbeat")
  now=$(date +%s)
  echo "距今 $(( now - hb ))s"
else
  echo "(无心跳文件，scheduler 从未启动)"
fi

echo
echo "=== 单例锁 ==="
if [ -f "$CN_DIR/.collector.lock" ]; then
  pid=$(cat "$CN_DIR/.collector.lock")
  if kill -0 "$pid" 2>/dev/null; then echo "有批次在跑 pid=$pid"; else echo "残留锁 pid=$pid（进程已死，下批会接管）"; fi
else
  echo "(空闲)"
fi

echo
echo "=== 各 topic 落盘文件数 ==="
for d in "$CN_DIR"/out/*/; do
  [ -d "$d" ] || continue
  printf '  %-16s %s\n' "$(basename "$d")" "$(ls -1 "$d" | wc -l)"
done

echo
echo "=== scheduler 最近 8 行 ==="
tail -8 "$LOGS/scheduler.log" 2>/dev/null || echo "(无)"

echo
echo "=== 采集最近 8 行 ==="
tail -8 "$LOGS/collector_offer.log" 2>/dev/null || echo "(无)"
