#!/usr/bin/env bash
# collector_linux.sh — Linux 采集入口（替代 Windows 的 collect_batch.bat / collector_silent.vbs）
#
# 用法: collector_linux.sh offer|night
#   offer = 7 个快档 topic
#           jczq_offer jclq_offer jczq_result jclq_result jc_issue jc_issue_result lottery_draw
#   night = 8 个 topic（offer 7 + jc_odds_history）
#
# 环境变量：
#   COLLECTOR_WORK_ROOT  放置 .ssh/ 的工作区根目录，默认 /home/agentuser/Work
#
# 两个必须显式处理、否则必踩的坑：
#   1) OpenSSH 展开 `~` 走 getpwuid()，**不认 $HOME**。受限沙箱里真实家目录不可写、
#      ~/.ssh 不存在，所以 ssh 配置与私钥必须用绝对路径传下去（collector.py 读
#      COLLECTOR_SSH_CONFIG / COLLECTOR_IDENTITY）。只 export HOME 是不够的。
#   2) umask 必须是 022。沙箱默认 077 会产出 0600 的文件，tar 原样带到 oracle 后
#      远端 league-staging 的 ingest 读不到（历史批次一律 0644/0664）。
set -uo pipefail

WORK_ROOT="${COLLECTOR_WORK_ROOT:-/home/agentuser/Work}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CN_DIR="$(dirname "$HERE")"

export HOME="$WORK_ROOT"
export COLLECTOR_SSH_CONFIG="$WORK_ROOT/.ssh/config"
export COLLECTOR_IDENTITY="$WORK_ROOT/.ssh/collector-league"
umask 022

case "${1:-}" in
  offer) TOPICS="jczq_offer jclq_offer jczq_result jclq_result jc_issue jc_issue_result lottery_draw" ;;
  night) TOPICS="jczq_offer jclq_offer jczq_result jclq_result jc_issue jc_issue_result lottery_draw jc_odds_history" ;;
  *) echo "usage: $0 offer|night" >&2; exit 2 ;;
esac

cd "$CN_DIR" || exit 1
exec /usr/bin/python3 collector.py --push-batch $TOPICS --mode-log "$1"
