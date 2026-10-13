"""单个 JSONL 文件 → 指定 stg 表（国内机官方数据装载，schema 校验先行）。

契约见 docs/国内采集机实施文档-v1.md §5：
- 一行一个 JSON 对象；非 JSON / 非 object / 缺必需字段 → 整行进 rejected，绝不入库；
- src_hash 由本模块对**解析后的对象**的规范化 JSON 求 sha256：采集机与本地无法保证字节序一致，
  幂等键必须本地可复算，故不采信入参 src_hash 的值，只把它当"契约必需字段"校验存在性；
- 写路径只有 INSERT … ON CONFLICT (src_hash) DO NOTHING：stg 无 UPDATE/DELETE 权限，重放天然安全；
- 事务归调用方持有（本模块不 commit）——测试即靠这一点把落库痕迹整体 rollback。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from .parse_values import canonical_src_hash  # §5.0 幂等键算法 — 单一真源 see P4-3

STG_TABLES: frozenset[str] = frozenset(
    {"jczq_offer", "jczq_result", "jc_issue", "jc_issue_result", "jclq_offer", "jclq_result", "lottery_draw"}
)
INSERT_SQL = "INSERT INTO stg.{} (line, src_hash, src_file) VALUES (%s, %s, %s) ON CONFLICT (src_hash) DO NOTHING"
MAX_REJECTED_DETAIL = 50
RAW_PREVIEW_CHARS = 200
# 行级必需字段：只钉采集文档 §5 的语义骨架列，细化字段等契约 v1.1 冻结后随工单改
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "jczq_offer": ("snap_ts", "jc_display_num", "play_type", "options"),
    "jczq_result": ("match_display_num", "home_cn", "away_cn", "ft"),
    "jc_issue": ("issue_no", "game", "matches"),
    "jc_issue_result": ("issue_no", "game", "results"),
    "jclq_offer": ("snap_ts", "jc_display_num", "play_type", "options"),
    "jclq_result": ("match_display_num", "ft"),
    "lottery_draw": ("game", "issue_no", "draw_date", "numbers"),
}


def reject_reason(obj: Any, table: str) -> str | None:
    """合法返回 None，否则返回拒绝原因（测试的契约断言直接复用）。"""
    if not isinstance(obj, dict):
        return "not a json object"
    if not isinstance(obj.get("src_hash"), str) or not obj["src_hash"]:
        return "missing src_hash"
    missing = [field for field in REQUIRED_FIELDS[table] if obj.get(field) in (None, "", [], {})]
    return f"missing required field(s): {','.join(missing)}" if missing else None


def _parse(raw: str) -> tuple[Any, str | None]:
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"invalid json: {exc.msg}"


def _reject(acc: dict[str, Any], lineno: int, reason: str, raw: str) -> None:
    acc["n"] += 1
    if len(acc["detail"]) < MAX_REJECTED_DETAIL:
        acc["detail"].append({"lineno": lineno, "reason": reason, "raw": raw[:RAW_PREVIEW_CHARS]})


def load_file(conn: Connection, path: str | Path, table: str) -> dict[str, Any]:
    """把 path 的 JSONL 追加进 stg.<table>；返回 rows_in/rows_ups/rejected_n/rejected 四元摘要。"""
    if table not in STG_TABLES:
        raise ValueError(f"unknown stg table {table!r}")
    src_file = Path(path).name
    rows_in = rows_ups = 0
    acc: dict[str, Any] = {"n": 0, "detail": []}
    statement = sql.SQL(INSERT_SQL).format(sql.Identifier(table))
    with open(path, encoding="utf-8") as handle, conn.cursor() as cur:
        for lineno, raw in enumerate(handle, 1):
            raw = raw.strip()
            if not raw:
                continue
            rows_in += 1
            obj, error = _parse(raw)
            reason = error or reject_reason(obj, table)
            if reason:
                _reject(acc, lineno, reason, raw)
                continue
            cur.execute(statement, (Jsonb(obj), canonical_src_hash(obj), src_file))
            rows_ups += cur.rowcount
    return {"rows_in": rows_in, "rows_ups": rows_ups, "rejected_n": acc["n"], "rejected": acc["detail"]}
