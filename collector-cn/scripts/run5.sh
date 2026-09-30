#!/usr/bin/env bash
# run5.sh — 采集机一键安装/自检脚本（在腾讯机上执行）
#
# 远端只跑这一条，不要拆分、不要改写：
#   curl -sSk https://140.83.62.161/cpt/_pkg/run5.sh | bash
#
# 流程：取包 → 校验 sha256 → 解包 → 只读诊断(报告自动推回 oracle) → 重起调度 → 状态快照(推回)
#
# 之所以做成"一条命令"：远端 AI 会话多次擅自跳过步骤、只跑部分 topic。
# 把逻辑收进脚本，远端就只剩"执行这一条"这一个动作，没有解释空间。
set -uo pipefail

PKG="collector-cn-dd2b9b8-linux5.tgz"
WANT="66e2bd84e308690fe6eac67bbc43274bd4d0ee0d762239ad3b36f1f659996c24"
BASE="https://140.83.62.161/cpt/_pkg"
WORK="/home/agentuser/Work"
SRC="$WORK/src"
CN="$SRC/collector-cn"
export HOME="$WORK"
export COLLECTOR_WORK_ROOT="$WORK"
export COLLECTOR_SSH_CONFIG="$WORK/.ssh/config"
export COLLECTOR_IDENTITY="$WORK/.ssh/collector-league"

log() { printf '\n===== %s =====\n' "$*"; }

push_to_oracle() {  # push_to_oracle <本地文件>
  local f="$1"
  tar -czf - --mode=0644 -C "$(dirname "$f")" "$(basename "$f")" \
    | ssh -F "$COLLECTOR_SSH_CONFIG" -o BatchMode=yes -o IdentitiesOnly=yes \
          -i "$COLLECTOR_IDENTITY" oracle-league \
          "tar -C /srv/league-staging/incoming/cn-collector -xzf -"
  echo "push rc=${PIPESTATUS[1]:-$?}"
}

log "1/6 下载 $PKG"
mkdir -p "$WORK" || { echo "无法创建 $WORK"; exit 1; }
curl -sSk --max-time 300 -o "$WORK/$PKG" "$BASE/$PKG" || { echo "下载失败"; exit 1; }
ls -l "$WORK/$PKG"

log "2/6 校验 sha256"
GOT="$(sha256sum "$WORK/$PKG" | cut -d' ' -f1)"
echo "got  = $GOT"
echo "want = $WANT"
[ "$GOT" = "$WANT" ] || { echo "校验不一致，终止（不覆盖现有安装）"; exit 1; }

log "3/6 解包到 $SRC"
mkdir -p "$SRC" || { echo "无法创建 $SRC"; exit 1; }
tar -xzf "$WORK/$PKG" -C "$SRC" || { echo "解包失败"; exit 1; }
chmod 0755 "$CN"/scripts/*.sh "$CN"/scripts/scheduler.py 2>/dev/null
ls -l "$CN/scripts"

log "4/6 只读诊断（报告自动推回 oracle）"
bash "$CN/scripts/diagnose.sh"

log "5/6 停旧调度并重新拉起"
for pat in 'scheduler.py' 'supervise.sh'; do
  ps -eo pid,args 2>/dev/null | grep -F "$pat" | grep -v grep | awk '{print $1}' \
    | xargs -r kill 2>/dev/null || true
done
sleep 2
bash "$CN/scripts/bootstrap.sh"

log "6/6 状态快照（推回 oracle）"
ST="$CN/logs/run5_status_$(date -u +%Y%m%dT%H%M%SZ).txt"
mkdir -p "$CN/logs"
{
  echo "### run5 status  $(date -u +%FT%TZ)  umask=$(umask)"
  bash "$CN/scripts/status.sh"
} > "$ST" 2>&1
cat "$ST"
push_to_oracle "$ST"

log "run5.sh 结束"
