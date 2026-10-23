#!/usr/bin/env bash
# 落港目录保留策略清理（2026-10-23）
#
# ## 为什么需要这个脚本
#
# 落港目录 ``/srv/league-staging/incoming/cn-collector`` 原本是**只增不减**的
# （运维红线：只追加不删），实测已长到 **1.6 GB / 2655 个 .done marker /
# 19300+ 个数据文件**（09-15 起）。后果有三个：
#
#   ① **每次装载都要扫全目录**（装载器无增量水位，``iter_markers`` 返回全部
#      marker）⇒ 装载越来越慢，且历史坏文件每次都被重新报错；
#   ② **6~29 个历史坏文件**（内容交织/截断 ⇒ JSONDecodeError）留在盘上，
#      每次装载重复报错；更糟的是它们让所属 marker **永久 ok=false**
#      ⇒ 水位失效 ⇒ 那几批**每 10 分钟被重装一遍** ⇒ ``ops.ingest_log``
#      每天凭空多 1 万行（实测：最近 24h 10201 行，**其中没有一个新文件**）；
#   ③ 无上限增长 ⇒ 迟早撑爆磁盘。
#
# ## 保留窗口怎么定的（**数据驱动**，不是拍脑袋）
#
#   - 落港目录的职责 = **传输中转 + 排障取证**。业务数据早已落库
#     （``fact.jc_offer`` 2026-09-15 起全在库，182600 行），
#     且 ``ops.file_arrival`` 逐文件记了「已装载」⇒ **落港文件删了不影响数据完整性**。
#   - 排障窗口：人工排障看的是**最近几天**（改配置、回补、查缺档）。
#   - ⇒ 默认保留 **30 天**（``JC_STAGING_KEEP_DAYS``），
#     覆盖所有活跃排障场景，同时把 1.6 GB 压到可控量级。
#
# ## ⚠️ 关键顺序：**先删盘上文件 → 再删台账**
#
# ``ops.file_arrival`` 是装载器判「这个文件已装载」的依据。若**只删台账**
# 而文件还在盘上 ⇒ 那批文件立刻变成「未装载的孤儿」⇒ **每轮被重装**
# ⇒ **反而制造出新的重复日志**（实测：待删的 4530 项里 99.65% 的文件还在盘上）。
# ⇒ 本脚本**先**移走盘上文件，**再**删对应台账行。
#
# ## 坏文件单独隔离（不直接删）
#
# 坏文件移进 ``$QUARANTINE``（**保留**，可回滚），不丢证据。
# 移走后它所属的 marker 就能在下轮装载时标 ok=true ⇒ 停止重复重装。
#
# 用法：
#   bash ~/bin/jc-staging-retention.sh            # 报告（不动任何东西）
#   bash ~/bin/jc-staging-retention.sh --apply    # 真执行
set -uo pipefail

DIR=${JC_STAGING_DIR:-/srv/league-staging/incoming/cn-collector}
# ⚠️ 隔离区必须放在**已存在、league 可写**的目录里：
#    ``/srv/league-staging`` 本身属主是 root（league 不可写、不能在里面建目录），
#    但 ``/srv/league-staging/bad`` 是 league 拥有且可写 ⇒ 用它。
QUAR=${JC_QUARANTINE_DIR:-/srv/league-staging/bad}
KEEP_DAYS=${JC_STAGING_KEEP_DAYS:-30}
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1
LOG=/home/ubuntu/logs/jc-staging-retention.log
LOCK=/home/ubuntu/logs/jc-staging-retention.lock
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p /home/ubuntu/logs
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$STAMP] 上一轮清理还在跑，跳过" >> "$LOG"
  exit 0
fi

log() { echo "[$STAMP] $*" | tee -a "$LOG"; }

log "== 开始 dir=$DIR 保留 $KEEP_DAYS 天 apply=$APPLY =="

if [ ! -d "$DIR" ]; then log "✗ 落港目录不存在：$DIR"; exit 7; fi
# 落港目录归 league 所有 ⇒ 文件操作**必须**用 league 身份做（root 移文件会改属主）
if ! id league >/dev/null 2>&1; then log "✗ 没有 league 用户"; exit 7; fi
# ⚠️ **只有真跑才建隔离目录**：否则每次 dry-run 都会留下一个空目录 ⇒ 无上限堆积空目录
if [ "$APPLY" = "1" ]; then
  sudo -n -u league mkdir -p "$QUAR/$STAMP" || { log "✗ 建隔离目录失败：$QUAR/$STAMP"; exit 7; }
fi

# ── 1) 坏文件：隔离（保留证据，不直接删） ────────────────────────────
log "-- 扫描坏文件（逐行 json.loads）--"
sudo -n -u league python3 - "$DIR" "$QUAR/$STAMP" "$APPLY" <<'PY'
import json, shutil, sys
from pathlib import Path
root = Path(sys.argv[1]); qdir = Path(sys.argv[2]); apply_ = sys.argv[3] == "1"
if apply_:
    qdir.mkdir(parents=True, exist_ok=True)   # ⚠️ dry-run 不建（否则每次留一个空目录）
bad = []
for t in sorted(p.name for p in root.iterdir() if p.is_dir()):
    for f in sorted((root / t).glob("*.jsonl")):
        try:
            with open(f, encoding="utf-8") as fh:
                for i, ln in enumerate(fh, 1):
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        json.loads(ln)
                    except Exception:
                        bad.append((t, f, i))
                        break
        except Exception:
            bad.append((t, f, 0))
print("BAD_FILES=%d" % len(bad))
moved = 0
for t, f, line in bad:
    rel = f.relative_to(root)
    print("  %-52s line=%d" % (str(rel), line))
    if apply_:
        dest = qdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest))
        moved += 1
print("QUARANTINED=%d dir=%s" % (moved, qdir))
PY
[ $? -eq 0 ] || { log "✗ 坏文件扫描失败"; exit 8; }

# ── 2) 过期文件：先移走盘上文件 ───────────────────────────────────────
# ⚠️ 判据用**文件名里的日期**（``YYYY-MM-DD`` 前缀）而不是 mtime ——
#    文件名才是契约（批次时间戳），mtime 可能被传输工具 touch 改过。
#    （实测抽样 10/10 一致，但契约优先。）
log "-- 统计过期文件（文件名日期超过 $KEEP_DAYS 天）--"
sudo -n -u league python3 - "$DIR" "$KEEP_DAYS" "$APPLY" <<'PY'
import re, shutil, sys, time
from pathlib import Path
root = Path(sys.argv[1]); keep_days = int(sys.argv[2]); apply_ = sys.argv[3] == "1"
cut = time.time() - keep_days * 86400
PAT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T")

def file_date(p: Path):
    """文件名日期 → 时间戳；解析不出来就退回 mtime。"""
    m = PAT.match(p.name)
    if not m:
        return p.stat().st_mtime
    import calendar
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return calendar.timegm((y, mo, d, 0, 0, 0, 0, 0, 0))
    except Exception:
        return p.stat().st_mtime

files = [f for t in root.iterdir() if t.is_dir() for f in t.glob("*.jsonl")]
files += [f for t in root.iterdir() if t.is_dir() for f in t.glob("*.empty")]
old = [f for f in files if file_date(f) < cut]
markers = [m for m in root.glob("*.done") if file_date(m) < cut]
print("OLD_FILES=%d OLD_MARKERS=%d TOTAL_FILES=%d" % (len(old), len(markers), len(files)))
if apply_:
    n = 0
    for f in old:
        try:
            f.unlink(); n += 1
        except OSError:
            pass
    m = 0
    for mk in markers:
        try:
            mk.unlink(); m += 1
        except OSError:
            pass
    print("REMOVED_FILES=%d REMOVED_MARKERS=%d" % (n, m))
else:
    print("DRY-RUN（未删）")
# 把要删的相对路径打出来，供后面的台账清理用
with open("/tmp/jc-staging-expired.txt", "w", encoding="utf-8") as fh:
    for f in old:
        fh.write(str(f.relative_to(root)) + "\n")
PY
[ $? -eq 0 ] || { log "✗ 过期文件扫描失败"; exit 9; }

# ── 3) 台账：**默认不动** ─────────────────────────────────────────────
# ⚠️ **删了文件就不要再删台账**（与直觉相反，但这里是对的）：
#   - ``ops.file_arrival`` 是「这个文件已装载」的依据。文件删了、台账留着
#     ⇒ 以后同一文件再被推送时会被判「已装载」⇒ **不重复装载**（安全）。
#   - 台账删了 ⇒ 万一文件被重新推送 ⇒ 判「未装载」⇒ 又要装一遍（多一轮 IO，
#     不算错，但没必要）。
#   ⇒ 结论：**只删盘上文件，台账保留**（它只是历史记录，19K 行很小）。
#   ⇒ 这里只**报告**台账里有多少行对应的文件已被移走，供人工核对。
if [ "$APPLY" = "1" ] && [ -s /tmp/jc-staging-expired.txt ]; then
  log "-- 核对台账（已移走的文件在 file_arrival 里仍留记录 ⇒ 避免将来重复装载）--"
  python3 - <<'PY' | tee -a "$LOG"
from pathlib import Path
rows = [ln.strip() for ln in Path("/tmp/jc-staging-expired.txt").read_text(encoding="utf-8").splitlines() if ln.strip()]
print("已移走文件 %d 个；它们在 ops.file_arrival 里的记录**保留**（防将来重复装载）" % len(rows))
PY
fi

log "== 结束 =="
