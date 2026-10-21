"""竞彩官方 id 批量建行 + 灌映射（路线图 §6.12.3 A）的落地锁。

背景（2026-10-21 生产实测）：``fact.jc_match`` 里出现过的官方 id 共 397 个，
153 个已映射、**244 个没有对应的 ref.team 行** ⇒ ``home_team_id/away_team_id`` 永远是 NULL。
实测这 244 个中文名**两两不同**、与既有 ``ref.team.name_cn`` **零冲突** ⇒ 1:1 建行无歧义。

本文件锁三件事：
  ① **只增不改**：建行/灌映射必须带 ``ON CONFLICT ... DO NOTHING``，回填必须只补 ``IS NULL``；
  ② **不猜**：队名只取 ``fact.jc_match`` 的竞彩官方中文名（唯一真源）；
  ③ **幂等 + 默认只读**：CLI 缺省是预演（回滚），生产回填入口是 ``scripts/ingest/jc_team_backfill.py``。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

from ingest import jc_write  # noqa: E402

SQL_FILE = REPO_ROOT / "docs" / "db" / "suggestion12_team_backfill.sql"


# ────────────────────────── 假 cursor ──────────────────────────

class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeCursor:
    """按 SQL 子串分派的假 cursor；rowcount 可预设（模拟 INSERT/UPDATE 的影响行数）。"""

    def __init__(self, handlers: dict[str, list] | None = None, rowcounts: dict[str, int] | None = None):
        self.handlers = handlers or {}
        self.rowcounts = rowcounts or {}
        self.calls: list[tuple[str, object]] = []
        self.rowcount = 0

    def execute(self, query, params=None):
        self.calls.append((query, params))
        self.rowcount = 0
        for key, n in self.rowcounts.items():
            if key in query:
                self.rowcount = n
                break
        for key, rows in self.handlers.items():
            if key in query:
                return _Result(rows)
        return _Result([])

    def queries(self) -> list[str]:
        return [q for q, _ in self.calls]


# ────────────────────────── ① 只增不改 / 只补 NULL ──────────────────────────

def test_build_rows_is_insert_only():
    """建行必须是 INSERT + ON CONFLICT DO NOTHING（绝不 UPDATE 既有行）。"""
    sql = jc_write.TEAM_ENSURE_ROWS_SQL
    assert sql.lstrip().startswith("INSERT INTO ref.team")
    assert "ON CONFLICT (sport, name_cn) DO NOTHING" in sql
    assert "UPDATE" not in sql.upper().replace("UPDATED", "")
    assert "DELETE" not in sql.upper()


def test_fill_mapping_never_overwrites():
    """灌映射必须 ON CONFLICT (sport, jc_id) DO NOTHING —— 改指向等于改写历史 join 键。"""
    sql = jc_write.TEAM_FILL_MAP_SQL
    assert sql.lstrip().startswith("INSERT INTO ref.team_sporttery")
    assert "ON CONFLICT (sport, jc_id) DO NOTHING" in sql
    assert "DO UPDATE" not in sql


def test_backfill_only_fills_null():
    """场次回填只补 NULL ⇒ 已有映射绝不被改写。"""
    for sql in (jc_write.TEAM_BACKFILL_HOME_SQL, jc_write.TEAM_BACKFILL_AWAY_SQL):
        assert "UPDATE fact.jc_match" in sql
        assert "IS NULL" in sql
        assert "team_id IS NOT NULL" not in sql


def test_names_come_from_jc_match_only():
    """队名只取 fact.jc_match 的官方中文名（唯一真源），不做任何名称推断。"""
    for sql in (jc_write.TEAM_ENSURE_ROWS_SQL, jc_write.TEAM_FILL_MAP_SQL):
        assert "fact.jc_match" in sql
        assert "home_cn" in sql and "away_cn" in sql
        assert "team_translations" not in sql


def test_deterministic_main_id_for_multi_id_team():
    """一支队多个官方 id 时，建行取**最小 id** 当主 id（DISTINCT ON + ORDER BY）⇒ 可复算。"""
    assert "DISTINCT ON (u.cn)" in jc_write.TEAM_ENSURE_ROWS_SQL
    assert "ORDER BY u.cn, u.jc_id" in jc_write.TEAM_ENSURE_ROWS_SQL


# ────────────────────────── ② ensure_jc_team_mappings ──────────────────────────

def test_ensure_returns_counts():
    cur = FakeCursor(handlers={"GROUP BY jc_id": [(0,)]},
                     rowcounts={"INSERT INTO ref.team (": 244,
                                "INSERT INTO ref.team_sporttery": 244,
                                "UPDATE fact.jc_match m SET home_team_id": 221,
                                "UPDATE fact.jc_match m SET away_team_id": 221})
    stats = jc_write.ensure_jc_team_mappings(cur)
    assert stats["teams_created"] == 244
    assert stats["mappings_added"] == 244
    assert stats["home_backfilled"] == 221
    assert stats["away_backfilled"] == 221
    assert stats["multi_name_ids"] == 0


def test_ensure_runs_four_statements_in_order():
    cur = FakeCursor(handlers={"GROUP BY jc_id": [(0,)]})
    jc_write.ensure_jc_team_mappings(cur)
    qs = cur.queries()
    assert len(qs) == 5, qs          # 建行 / 灌映射 / 回填主 / 回填客 / 多名校验
    assert "INSERT INTO ref.team (sport" in qs[0]
    assert "INSERT INTO ref.team_sporttery" in qs[1]
    assert "home_team_id" in qs[2]
    assert "away_team_id" in qs[3]


def test_ensure_is_idempotent_by_construction():
    """第二次跑时，四条写语句的影响行数必须都是 0（ON CONFLICT + IS NULL 的效果）。"""
    cur = FakeCursor(handlers={"GROUP BY jc_id": [(0,)]},
                     rowcounts={"INSERT INTO ref.team (": 0,
                                "INSERT INTO ref.team_sporttery": 0,
                                "UPDATE fact.jc_match m SET home_team_id": 0,
                                "UPDATE fact.jc_match m SET away_team_id": 0})
    stats = jc_write.ensure_jc_team_mappings(cur)
    assert stats["teams_created"] == 0
    assert stats["mappings_added"] == 0
    assert stats["home_backfilled"] == 0
    assert stats["away_backfilled"] == 0


def test_ensure_reports_multi_name_ids():
    """同一个官方 id 出现多个中文名 ⇒ 必须报出来（不静默二选一）。"""
    cur = FakeCursor(handlers={"GROUP BY jc_id": [(3,)]})
    stats = jc_write.ensure_jc_team_mappings(cur)
    assert stats["multi_name_ids"] == 3


# ────────────────────────── ③ CLI 契约 + SQL 文件对账 ──────────────────────────

def test_cli_exists_and_defaults_to_dry_run():
    from ingest import jc_team_backfill
    src = (REPO_ROOT / "scripts" / "ingest" / "jc_team_backfill.py").read_text(encoding="utf-8")
    assert "--execute" in src
    assert '"--execute", action="store_true"' in src
    # 默认路径必须先 rollback 再打印
    assert src.index("conn.rollback()") < src.index("payload[\"after\"] = counts(conn, args.sport)")
    assert callable(jc_team_backfill.main)


def test_cli_counts_before_after():
    from ingest import jc_team_backfill
    sql = jc_team_backfill.COUNTS_SQL
    for key in ("ref.team", "ref.team_sporttery", "fact.jc_match"):
        assert key in sql
    assert "unmapped" in sql or "u.jc_id" in sql


def test_sql_file_is_single_transaction_and_mirrors_code():
    """docs/db 里的手工版必须与代码版同口径（守门在 check_docs_claims.py 里也有对账断言）。"""
    sql = SQL_FILE.read_text(encoding="utf-8")
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "ON CONFLICT (sport, name_cn) DO NOTHING" in sql
    assert "ON CONFLICT (sport, jc_id) DO NOTHING" in sql
    assert sql.count("IS NULL") >= 2
    assert "UPDATE fact.jc_match" in sql
    assert "DELETE" not in sql.upper()


def test_sql_file_does_not_touch_existing_rows():
    """手工版也不许 UPDATE/DELETE 既有 ref.team 行。"""
    sql = SQL_FILE.read_text(encoding="utf-8").upper()
    assert "UPDATE REF.TEAM" not in sql
    assert "DELETE FROM REF.TEAM" not in sql


# ────────────────────────── ④ 采集侧接线 ──────────────────────────

def test_ingest_calls_ensure_after_batches():
    """每轮采集必须调 ensure_jc_team_mappings ⇒ 新队不再继续攒映射缺口。"""
    src = (REPO_ROOT / "scripts" / "ingest" / "jc_load.py").read_text(encoding="utf-8")
    assert "jc_write.ensure_jc_team_mappings(cur)" in src
    # 补齐失败不许拖垮采集
    assert "队映射补齐失败" in src


@pytest.mark.parametrize("path", ["scripts/ingest/jc_write.py", "scripts/ingest/jc_load.py",
                                  "scripts/ingest/jc_team_backfill.py"])
def test_new_files_have_no_fffd(path):
    """中文注释里混进 U+FFFD 会变成方块 ⇒ 一律不许。"""
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert "\ufffd" not in text
