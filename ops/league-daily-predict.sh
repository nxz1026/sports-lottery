#!/usr/bin/env bash
# league-daily-predict.sh — league-v2 每日全量预测（由 league-daily-predict.timer 触发）
#
# 为什么走 HTTP API 而不是直接调 CLI：
#   配额守卫（config.DAILY_TRIGGER_LIMIT）与并发守卫（jobs.active_job）都在
#   web/services/jobs.py 里。直调 `python scripts/predict.py` 会绕过它们，
#   与页面上的手动任务撞车、吃光当日配额。走 API 则两者都生效。
#
# 为什么串行：
#   足球预测、篮球预测、AI 富化**共享同一个并发守卫**（active_job），
#   同时发只会拿到 409。因此必须等前一个到终态再发下一个。
#
# trigger=timer：
#   三个作业端点都接受 trigger 字段（白名单 manual/timer/cron），
#   传 timer 后作业在 Dashboard「数据源与任务」页可与手动作业区分。
#
# 退出码：0=全部成功；1=有作业失败/触发失败；2=登录失败；3=配额耗尽（提前退出）
set -uo pipefail

BASE="${LEAGUE_API_BASE:-http://127.0.0.1:8077/api/v1}"
# 凭据优先取 .env（service 单元用 EnvironmentFile 注入 AUTH_USERNAME/AUTH_PASSWORD），
# 避免在两处各写一份；LEAGUE_AUTH_* 仅用于临时覆盖。
AUTH_USER="${LEAGUE_AUTH_USER:-${AUTH_USERNAME:-a}}"
AUTH_PASS="${LEAGUE_AUTH_PASS:-${AUTH_PASSWORD:-a}}"
JOB_TIMEOUT="${LEAGUE_JOB_TIMEOUT:-3600}"    # 单个作业等待上限（秒）
POLL_INTERVAL="${LEAGUE_POLL_INTERVAL:-15}"  # 轮询间隔（秒）
PY="${LEAGUE_PYTHON:-/home/ubuntu/.venvs/league/bin/python}"

JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT
rc=0

log() { printf '[%s] %s\n' "$(date -u '+%F %T UTC')" "$*"; }

# api <curl args...>：回显 body，最后一行是 HTTP 状态码
api() { curl -sS --max-time 30 -b "$JAR" -c "$JAR" -w $'\n%{http_code}' "$@"; }

# 从 {"job":{...}} 里取一个字段
job_field() {
  "$PY" -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit
print((d.get("job") or {}).get(sys.argv[1], ""))' "$1"
}

wait_job() {
  local jid="$1" deadline=$(( SECONDS + JOB_TIMEOUT )) st
  while [ "$SECONDS" -lt "$deadline" ]; do
    st="$(api "$BASE/jobs/$jid" | sed '$d' | job_field status)"
    case "$st" in
      done)      log "  作业 $jid 完成"; return 0 ;;
      failed|timeout|cancelled) log "  作业 $jid 终态=$st，视为失败"; return 1 ;;
      "")        log "  作业 $jid 状态读取失败，${POLL_INTERVAL}s 后重试" ;;
    esac
    sleep "$POLL_INTERVAL"
  done
  log "  作业 $jid 等待超时（${JOB_TIMEOUT}s）"
  return 1
}

# trigger <label> <endpoint> [json-body]
trigger() {
  local label="$1" ep="$2" body="${3:-}" resp code jid
  log "触发 $label"
  if [ -n "$body" ]; then
    resp="$(api -X POST "$BASE/$ep" -H 'Content-Type: application/json' -d "$body")"
  else
    resp="$(api -X POST "$BASE/$ep")"
  fi
  code="$(tail -n1 <<<"$resp")"
  case "$code" in
    202|409)
      jid="$(sed '$d' <<<"$resp" | job_field id)"
      [ "$code" = "409" ] && log "  已有作业在跑，改为等待 $jid"
      [ -n "$jid" ] || { log "  $label 响应缺少 job.id"; rc=1; return; }
      wait_job "$jid" || rc=1
      ;;
    429)
      log "  今日配额已用尽，跳过 $label"
      rc=3
      ;;
    *)
      log "  $label 触发失败 HTTP $code: $(sed '$d' <<<"$resp")"
      rc=1
      ;;
  esac
}

log "登录 $BASE"
resp="$(api -X POST "$BASE/login" -H 'Content-Type: application/json' \
        -d "{\"username\":\"$AUTH_USER\",\"password\":\"$AUTH_PASS\"}")"
if [ "$(tail -n1 <<<"$resp")" != "200" ]; then
  log "登录失败 HTTP $(tail -n1 <<<"$resp"): $(sed '$d' <<<"$resp")"
  exit 2
fi
log "登录成功"

# 1) 足球全联赛：argv 必须显式带 --all，否则 predict.py 的 --league 默认值 epl
#    只会刷新英超（这正是旧进程内 cron 的实际行为，与其注释「全联赛」相反）。
#    3000 次已用线上真实运行验证；默认 10000 次会超过 web 作业 600s 超时。
trigger "足球全联赛预测" "jobs/predict" '{"all":true,"n_simulations":3000,"trigger":"timer"}'

# 2) NBA：休赛期揭幕战在数月后，默认 1 天窗口会得 0 场，故放宽到 90 天
trigger "NBA 篮球预测" "jobs/predict-bball" '{"ahead_days":90,"trigger":"timer"}'

# 3) AI 富化（配额与 predict 共享同一计数器）
trigger "AI 富化" "jobs/ai-enrich" '{"trigger":"timer"}'

# 4) topic 新鲜度检查：超 24h 落 /home/ubuntu/logs/dsh-freshness-alerts.log
#    不外推通知、不阻塞主流程。纯 GET，超时 30s。
ALERT_LOG="${LEAGUE_FRESHNESS_ALERT_LOG:-/home/ubuntu/logs/dsh-freshness-alerts.log}"
mkdir -p "$(dirname "$ALERT_LOG")"
resp="$(api "$BASE/jc/freshness?threshold_hours=24" || true)"
fresh_json="$(sed '$d' <<<"$resp" || true)"
fresh_code="$(tail -n1 <<<"$resp" || echo "000")"
if [ "$fresh_code" = "200" ] && [ -n "$fresh_json" ]; then
  while IFS= read -r line; do
    [ -n "$line" ] && log "$line" >> "$ALERT_LOG"
  done <<<"$(printf '%s' "$fresh_json" | /home/ubuntu/.venvs/league/bin/python -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    raise SystemExit
for a in d.get('alerts') or []:
    print(a)
")"
fi

log "结束，退出码 $rc"
exit "$rc"
