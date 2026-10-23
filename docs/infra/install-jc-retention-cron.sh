#!/usr/bin/env bash
# 保留策略 cron 安装器（2026-10-23）
#
# ## 它做什么
#
# 把两件事装到 Oracle 生产机上，**幂等**（重复跑不会叠加 cron 条目）：
#
#   ① 把仓库里的保留策略脚本/SQL 拷到 ``~/bin/``（生产真正执行的那份）；
#   ② 往 crontab 里装两条每日任务（带标记块 ⇒ 重复安装是**替换**不是**追加**）。
#
# ## 为什么要有这个安装器（而不是手敲 crontab）
#
# cron 是「装了但没人看得见」的东西：仓库里没有它 ⇒ 换机器/重建环境就丢了 ⇒
# 保留策略悄悄失效 ⇒ ``ops.ingest_log`` 又会长回千万行。
# ⇒ 部署物进仓库（能 diff、能 review、能被守门断言检查），安装器只负责落地。
#
# ## 时间口径
#
# Oracle 时区是 **Etc/UTC**；cron 也按 UTC 解释 ⇒ 选 **UTC 18:00 / 18:30**
# = **北京 02:00 / 02:30**（夜间低峰，采集端是手动模式、这段没有推送）。
# ⚠️ 避开同一台机器上已有的其它 cron（UTC 01/02/03/04/11 点各有任务）。
#
# 用法：
#   bash docs/infra/install-jc-retention-cron.sh              # 默认仓库路径
#   bash docs/infra/install-jc-retention-cron.sh /path/to/repo
#   bash docs/infra/install-jc-retention-cron.sh --print       # 只打印，不改
set -uo pipefail

REPO=${1:-${JC_REPO:-/home/ubuntu/DSH/sports-lottery}}
BIN=${JC_BIN_DIR:-/home/ubuntu/bin}
LOGDIR=${JC_LOG_DIR:-/home/ubuntu/logs}
MARK_BEGIN="# >>> jc-retention 2026-10-23 >>>"
MARK_END="# <<< jc-retention 2026-10-23 <<<"

PRINT_ONLY=0
for a in "$@"; do
  [ "$a" = "--print" ] && PRINT_ONLY=1
done
[ -d "$REPO/docs/infra" ] || REPO=${JC_REPO:-/home/ubuntu/DSH/sports-lottery}

if [ ! -f "$REPO/docs/infra/jc-staging-retention.sh" ]; then
  echo "✗ 找不到 $REPO/docs/infra/jc-staging-retention.sh（先 cd 到仓库根再跑）" >&2
  exit 7
fi

ENTRIES=$(cat <<EOF
# 落港目录保留 30 天 + 坏文件隔离（UTC 18:00 = 北京 02:00）
0 18 * * * $BIN/jc-staging-retention.sh --apply >> $LOGDIR/jc-staging-retention.log 2>&1
# ops 台账保留 7 天（UTC 18:30 = 北京 02:30；普通 VACUUM，不阻塞装载）
30 18 * * * $BIN/jc-ops-retention.sh >> $LOGDIR/ops-retention.log 2>&1
EOF
)

if [ "$PRINT_ONLY" = "1" ]; then
  echo "--- 目标 crontab 块 ---"
  echo "$MARK_BEGIN"
  echo "$ENTRIES"
  echo "$MARK_END"
  exit 0
fi

mkdir -p "$BIN" "$LOGDIR"

# ① 部署物进 ~/bin
install -m 0755 "$REPO/docs/infra/jc-staging-retention.sh" "$BIN/jc-staging-retention.sh"
install -m 0755 "$REPO/docs/infra/jc-ops-retention.sh" "$BIN/jc-ops-retention.sh"
install -m 0644 "$REPO/docs/db/ops_retention.sql" "$BIN/ops_retention.sql"
echo "✓ 已部署到 $BIN：jc-staging-retention.sh / jc-ops-retention.sh / ops_retention.sql"

# ② 幂等装 cron：先删掉旧标记块，再追加新块
CUR=$(crontab -l 2>/dev/null || true)
CLEANED=$(printf '%s\n' "$CUR" | sed "/$MARK_BEGIN/,/$MARK_END/d")
{
  printf '%s\n' "$CLEANED"
  printf '%s\n' "$MARK_BEGIN"
  printf '%s\n' "$ENTRIES"
  printf '%s\n' "$MARK_END"
} | crontab -

echo "✓ crontab 已更新（标记块替换式 ⇒ 重复安装不叠加）："
crontab -l | sed -n "/$MARK_BEGIN/,/$MARK_END/p"
