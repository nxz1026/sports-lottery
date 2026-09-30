#!/usr/bin/env bash
# bootstrap.sh — 一键把采集机跑起来：清旧调度 → 验证一批 → 拉起常驻调度 → 自检。
#
# 用法（在采集机工作区里执行）：
#   bash /home/agentuser/Work/src/collector-cn/scripts/bootstrap.sh
#
# 设计意图：远端 AI 会话容易自作主张（实测曾绕过入口脚本直接调 collector.py，
# 结果只采了 1 个 topic、且没启动调度）。所以把全部动作收进这一个脚本，远端只需
# 原样执行它、原样回报输出。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export COLLECTOR_WORK_ROOT="${COLLECTOR_WORK_ROOT:-/home/agentuser/Work}"
export HOME="$COLLECTOR_WORK_ROOT"
umask 022

echo "=== 0. 清掉可能存在的旧调度进程 ==="
pkill -f 'supervise\.sh' 2>/dev/null && echo "已停旧 supervise.sh" || echo "(无旧 supervise.sh)"
pkill -f 'scheduler\.py' 2>/dev/null && echo "已停旧 scheduler.py" || echo "(无旧 scheduler.py)"
sleep 1

echo
echo "=== 1. 验证一批：offer 全 7 topic（采集 + 推送）==="
"$HERE/collector_linux.sh" offer
echo "collect rc=$?"

echo
echo "=== 2. 拉起常驻调度（setsid 脱离本会话，关终端不影响）==="
setsid nohup "$HERE/supervise.sh" </dev/null >/dev/null 2>&1 &
echo "launcher pid=$!"
sleep 2

echo
echo "=== 3. 等 25s 后自检 ==="
sleep 25
"$HERE/status.sh"
