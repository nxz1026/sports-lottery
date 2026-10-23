#!/usr/bin/env bash
# ops 台账保留策略 —— 每日运行入口（2026-10-23）
#
# ## 为什么要它
#
# ``ops.ingest_log`` 曾经长到 **1208 万行 / 1740 MB**，而根因不是「表太大」，
# 而是装载器每 10 分钟把同一批老文件重装一遍（治本已修，见 §6.13.6）。
# 本脚本是**兜底**：给 ops 三张表加窗口，避免再出现无上限增长。
#
# ## 口径（详见 docs/db/ops_retention.sql 的注释）
#
#   - **两个窗口**，不是无脑删：
#     · ``keep_days`` = 7 → ``ops.ingest_log``（纯日志：窗口内全留，
#       更早的**每个 (topic, src_file) 只留最后一条** —— 「这个文件最后装成什么样」
#       不能丢；治本后实测每天只写 ~550 行 ⇒ 7 天足够覆盖一周排障）。
#     · ``ledger_keep_days`` = 37 → ``ops.file_arrival`` + ``ops.ingest_marker``。
#       ⚠️ **37 必须 > 落港保留天数（30）**：这两个表是「已装载 / 水位」台账，
#       必须比它记录的文件活得久；否则台账先消失 ⇒ 老文件被装载器判成「孤儿」
#       ⇒ **每轮全部重装**（2026-10-23 实测：keep_days=7 时下一轮 cron 重装了
#       **4295 个文件**，ingest_log 当场 +4295 行）。
#   - ``ops.ingest_marker`` **只删 ok=true 且过期**的 —— ok=false 是「下轮要重试」
#     的批次，删了就等于把失败批次当已处理 ⇒ 数据静默缺失。
#   - **只删 ops 台账，绝不碰业务表**（fact.* / analysis.* 一律不动）。
#
# ## 为什么不用 VACUUM FULL
#
# ``VACUUM FULL`` 能把空间真正还给 OS（实测 1740 MB → 418 MB），但它要拿
# **ACCESS EXCLUSIVE 锁** ⇒ 会**阻塞每 10 分钟一次的装载** ⇒ 只能低峰期/手动跑。
# 日常 cron 用普通 ``VACUUM (ANALYZE)``（只把空间标记为可重用，不锁表）。
#
# 用法：
#   bash ~/bin/jc-ops-retention.sh            # 等价于下面的 psql（每日 cron 调的就是它）
#   JC_OPS_KEEP_DAYS=14 bash ~/bin/jc-ops-retention.sh   # 临时改窗口
set -uo pipefail

BIN=${JC_OPS_BIN_DIR:-/home/ubuntu/bin}
LOG=${JC_OPS_LOG:-/home/ubuntu/logs/ops-retention.log}
LOCK=${JC_OPS_LOCK:-/home/ubuntu/logs/ops-retention.lock}
KEEP_DAYS=${JC_OPS_KEEP_DAYS:-7}
# ⚠️ **台账窗口必须 > 落港保留天数(30)**：file_arrival/ingest_marker 是「已装载/水位」
#    台账，必须比它记录的文件活得久 ⇒ 否则老文件被判孤儿 ⇒ 每轮重装（2026-10-23 实测
#    keep_days=7 时 4295 个文件被重装）。默认 37。
LEDGER_KEEP_DAYS=${JC_OPS_LEDGER_KEEP_DAYS:-37}
# ⚠️ 日常**不**开 vacuum_full（会阻塞装载）；需要回收磁盘时手动加 -v vacuum_full=1。
VACUUM_FULL=${JC_OPS_VACUUM_FULL:-0}
SQL="$BIN/ops_retention.sql"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p /home/ubuntu/logs
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$STAMP] 上一轮 ops 保留还在跑，跳过" >> "$LOG"
  exit 0
fi

if [ ! -f "$SQL" ]; then
  echo "[$STAMP] ✗ 找不到 $SQL（先跑 install-jc-retention-cron.sh 装上去）" >> "$LOG"
  exit 7
fi

{
  echo "[$STAMP] == ops 保留清理 keep_days=$KEEP_DAYS ledger_keep_days=$LEDGER_KEEP_DAYS vacuum_full=$VACUUM_FULL =="
  # ⚠️ DELETE 必须由 postgres 执行：league_ing 只有 INSERT/SELECT/UPDATE，没有 DELETE。
  # ⚠️ **用 stdin 喂 SQL，不用 -f 文件**：/home/ubuntu 是 0750（other 进不去）
  #    ⇒ postgres 读不到 ~/bin/ops_retention.sql。这里由 ubuntu（属主）读文件、
  #    经管道喂给 psql ⇒ postgres 只需读 stdin。
  # ⚠️ ON_ERROR_STOP=1：出错立刻停（别在 BEGIN/COMMIT 之间继续往下跑）。
  sudo -n -u postgres psql -d league -v ON_ERROR_STOP=1 \
       -v keep_days="$KEEP_DAYS" -v ledger_keep_days="$LEDGER_KEEP_DAYS" \
       -v apply=1 -v vacuum_full="$VACUUM_FULL" -f - < "$SQL"
} >> "$LOG" 2>&1
rc=$?
echo "[$STAMP] rc=$rc" >> "$LOG"
exit $rc
