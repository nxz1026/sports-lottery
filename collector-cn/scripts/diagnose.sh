#!/usr/bin/env bash
# diagnose.sh — 只读诊断：逐 topic 拉一次，把 HTTP 状态 / Content-Type / 响应体前 300 字节
# 连同 JSON 解析结论写成报告，并**通过采集器同一条 tar 通道推回 oracle**。
#
# 为什么要推回文件而不是在对话里回报：远端 AI 会话的 history 涨到几百 KB 后
# session.history 会返回截断 JSON，靠聊天取回复既慢又不可靠；走文件通道是确定性的。
#
# 用法: bash diagnose.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CN_DIR="$(dirname "$HERE")"
WORK_ROOT="${COLLECTOR_WORK_ROOT:-/home/agentuser/Work}"
export HOME="$WORK_ROOT"
export COLLECTOR_SSH_CONFIG="$WORK_ROOT/.ssh/config"
export COLLECTOR_IDENTITY="$WORK_ROOT/.ssh/collector-league"
umask 022
mkdir -p "$CN_DIR/logs"

REPORT="$CN_DIR/logs/diagnose_$(date -u +%Y%m%dT%H%M%SZ).txt"

{
  echo "### host=$(hostname 2>/dev/null)  date=$(date -u +%FT%TZ)  umask=$(umask)  python=$(/usr/bin/python3 -V 2>&1)"
  echo "### egress_ip=$(curl -sS --max-time 10 https://api.ipify.org 2>&1 || echo '(unavailable)')"
  echo
  cd "$CN_DIR" || exit 1
  /usr/bin/python3 - <<'PY'
import json, sys
sys.path.insert(0, ".")
import collector as C

for topic, spec in C.CONFIG["topics"].items():
    for url in spec["candidates"][:1]:
        # 候选 URL 带 {today}/{yesterday}/... 占位符，必须像 collect_topic() 那样先展开，
        # 否则网关直接回 400（第一版诊断脚本就是漏了这步，误报了两个 topic）。
        url = C.expand_url(url)
        print(f"--- {topic} ---")
        try:
            rec = C.fetch(url)
        except Exception as e:
            print(f"  fetch raised: {type(e).__name__}: {e}")
            continue
        body = rec.get("body") or b""
        print(f"  url: {url}")
        print(f"  status={rec.get('http_status')} ctype={rec.get('content_type')!r} "
              f"attempts={rec.get('attempts')} bytes={len(body)} err={rec.get('error', '')!r}")
        try:
            j = C.loads_body(body)
            print(f"  json OK  errorCode={j.get('errorCode')!r}  success={j.get('success')!r}")
        except Exception as e:
            print(f"  json FAIL: {type(e).__name__}: {e}")
        print(f"  head300: {body[:300]!r}")
PY
} > "$REPORT" 2>&1

echo "报告已写入 $REPORT"
echo "================ 报告内容 ================"
cat "$REPORT"

echo
echo "================ 推回 oracle ================"
tar -czf - --mode=0644 -C "$(dirname "$REPORT")" "$(basename "$REPORT")" \
  | ssh -F "$COLLECTOR_SSH_CONFIG" -o BatchMode=yes -o IdentitiesOnly=yes \
        -i "$COLLECTOR_IDENTITY" oracle-league \
        "tar -C /srv/league-staging/incoming/cn-collector -xzf -"
echo "push rc=${PIPESTATUS[1]:-$?}"
