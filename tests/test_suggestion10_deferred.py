"""建议 2/建议 1 两条遗留（路线图 §6.10）：竞彩官方 id 多对一映射 + raw→fact 物化 CLI。

两层：
  · **无库单测**（本文件主体）：用假 cursor / MagicMock 锁住 SQL 口径与分支行为 ——
    映射表优先、aliases 字符串/数组两种形态、缺表降级要出声、撞车不覆盖、
    物化 CLI 默认只读（回滚不提交）、参数互斥返回码 2、DDL 文件的关键语句。
  · **有库集成测试**（连不上库 skip）：映射表行数与唯一性、279→#2351、
    物化 dry-run 零写入 / execute 幂等。

为什么不做「把 aliases->>'sporttery' 改成数组」（原计划）：
  `->>` 作用在 jsonb 数组上返回**数组的 JSON 文本**（`["506","279"]`）而不是 NULL
  ⇒ 旧 SQL `(aliases->>'sporttery') = ANY(%s)` 会一条都匹配不上 ⇒ 152 支队映射静默全断；
  且 `jc_id` 是 STORED 生成列、`team_jc_key` partial unique 只能守主 id ⇒ 次 id 失去防撞。
  实测 245 个未映射 id 里 **0 个**能靠改 schema 救回（ref.team 里压根没有那一行），
  数组化真正能救的只有「一支队两个 id」这 1 例 3 个 side —— 映射表同样能救，且零破坏。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
for _p in (str(REPO_ROOT), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ingest import jc_write  # noqa: E402
from store import materialize  # noqa: E402

DDL = REPO_ROOT / "docs" / "db" / "suggestion10_team_sporttery_map.sql"
ROADMAP = REPO_ROOT / "docs" / "project" / "产品路线图-竞彩闭环-20261020.md"


# ────────────────────────── 假 cursor ──────────────────────────

class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeCursor:
    """按 SQL 子串分派的假 cursor：记录调用，按内容返回预设行。"""

    def __init__(self, handlers: dict[str, list]):
        self.handlers = handlers
        self.calls: list[tuple[str, object]] = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        for key, rows in self.handlers.items():
            if key in query:
                return _Result(rows)
        return _Result([])

    def queries(self) -> list[str]:
        return [q for q, _ in self.calls]


@pytest.fixture(autouse=True)
def _reset_map_cache():
    """映射表就绪状态是**模块级缓存** ⇒ 每个用例都要复位，否则用例之间互相污染。"""
    jc_write._MAP_TABLE_OK = None
    jc_write._MAP_TABLE_WARNED = False
    yield
    jc_write._MAP_TABLE_OK = None
    jc_write._MAP_TABLE_WARNED = False


# ────────────────────────── A1. resolve_jc_teams ──────────────────────────

def test_resolve_prefers_mapping_table():
    """映射表命中 ⇒ 直接拿到 team_id（不再查 aliases）。"""
    cur = FakeCursor({
        "to_regclass": [(True,)],
        "FROM ref.team_sporttery": [(2351, "279")],
    })
    assert jc_write.resolve_jc_teams(cur, 279, None) == (2351, None)
    assert not any("LATERAL" in q for q in cur.queries()), "映射表命中就不该再查 aliases"


def test_resolve_falls_back_to_aliases_string_form():
    """映射表没命中 ⇒ 回退 aliases 的**字符串**形态（存量口径）。"""
    cur = FakeCursor({
        "to_regclass": [(True,)],
        "FROM ref.team_sporttery": [],
        "LATERAL": [(2351, "506")],
    })
    assert jc_write.resolve_jc_teams(cur, 506, None) == (2351, None)
    assert any("LATERAL" in q for q in cur.queries())


def test_resolve_aliases_sql_accepts_array_form():
    """aliases 迁成数组后**必须还能命中** ⇒ SQL 要用 jsonb_array_elements_text 展开。"""
    cur = FakeCursor({
        "to_regclass": [(True,)],
        "FROM ref.team_sporttery": [],
        "LATERAL": [(2351, "506"), (2351, "279")],
    })
    assert jc_write.resolve_jc_teams(cur, 506, 279) == (2351, 2351)
    sql = jc_write.TEAM_ALIAS_SQL
    assert "jsonb_array_elements_text" in sql
    assert "jsonb_typeof" in sql, "必须区分字符串/数组两种形态（否则 ->> 会返回数组文本）"
    assert "jsonb_build_array" in sql, "字符串形态要包成单元素数组再展开"


def test_resolve_mapping_table_wins_over_aliases():
    """同一个 id 在两处指向不同队 ⇒ **映射表赢**（它是多 id 的真源）。"""
    cur = FakeCursor({
        "to_regclass": [(True,)],
        "FROM ref.team_sporttery": [(2351, "279")],
        "LATERAL": [(9999, "279")],
    })
    assert jc_write.resolve_jc_teams(cur, 279, None) == (2351, None)


def test_resolve_missing_id_returns_none():
    """查不到 ⇒ None（**不许猜**）。"""
    cur = FakeCursor({"to_regclass": [(True,)], "FROM ref.team_sporttery": [], "LATERAL": []})
    assert jc_write.resolve_jc_teams(cur, 999, 998) == (None, None)
    assert jc_write.resolve_jc_teams(cur, None, None) == (None, None)


def test_resolve_degrades_when_mapping_table_missing():
    """映射表还没建 ⇒ 退回旧的单 id 语义，**但必须出声**（不许静默降级）。"""
    cur = FakeCursor({"to_regclass": [(False,)], "LATERAL": [(2351, "506")]})
    assert jc_write.resolve_jc_teams(cur, 506, None) == (2351, None)
    assert not any("FROM ref.team_sporttery" in q and "SELECT" in q for q in cur.queries())
    assert jc_write._MAP_TABLE_WARNED is True, "缺表必须告警一次"


def test_resolve_handles_one_team_two_official_ids():
    """一支队两个官方 id（贝西克塔斯 279 / 毕尔巴鄂竞技 506）⇒ 都解析到同一行。"""
    cur = FakeCursor({
        "to_regclass": [(True,)],
        "FROM ref.team_sporttery": [(2351, "279"), (2351, "506")],
    })
    assert jc_write.resolve_jc_teams(cur, 279, 506) == (2351, 2351)


# ────────────────────────── A2. set_team_sporttery ──────────────────────────

def test_set_team_sporttery_inserts_and_is_idempotent():
    """新增成功 ⇒ True；已存在且指向同一支 ⇒ True（幂等）。"""
    cur = FakeCursor({"INSERT INTO ref.team_sporttery": [(2351,)]})
    assert jc_write.set_team_sporttery(cur, 2351, 279) is True
    cur = FakeCursor({"INSERT INTO ref.team_sporttery": [],
                       "SELECT team_id FROM ref.team_sporttery": [(2351,)]})
    assert jc_write.set_team_sporttery(cur, 2351, 279) is True


def test_set_team_sporttery_refuses_to_repoint_existing_id():
    """该 id 已属**别的队** ⇒ 返回 False 且不覆盖（改指向 = 改历史 join 键，人工决策）。"""
    cur = FakeCursor({"INSERT INTO ref.team_sporttery": [],
                       "SELECT team_id FROM ref.team_sporttery": [(9999,)]})
    assert jc_write.set_team_sporttery(cur, 2351, 279) is False
    assert "DO UPDATE" not in jc_write.TEAM_MAP_INSERT_SQL, "绝不能 DO UPDATE（会悄悄改指向）"


# ────────────────────────── A3. DDL 关键语句 ──────────────────────────

def test_ddl_file_has_the_required_statements():
    """DDL 必须包含：建表 + 主键 + 幂等迁移 + 只补 NULL 的回填 + 授权。"""
    sql = DDL.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS ref.team_sporttery" in sql
    assert "PRIMARY KEY (sport, jc_id)" in sql
    assert "ON CONFLICT (sport, jc_id) DO NOTHING" in sql
    assert "FROM ref.team t" in sql and "t.jc_id IS NOT NULL" in sql
    assert "m.home_team_id IS NULL" in sql, "回填只补 NULL，不许改已有映射"
    assert "m.away_team_id IS NULL" in sql
    grant = [ln for ln in sql.splitlines()
             if "GRANT SELECT, INSERT, UPDATE ON ref.team_sporttery" in ln]
    assert grant, "league_ing 授权缺失"
    assert "DELETE" not in grant[0].upper(), "league_ing 不该有 DELETE（与 ref.team 同口径）"


def test_ddl_does_not_touch_ref_team_rows():
    """DDL **不许**改 ref.team 的任何一行（只新建表 + 灌映射）。"""
    sql = DDL.read_text(encoding="utf-8")
    assert "UPDATE ref.team" not in sql
    assert "DELETE FROM ref.team" not in sql
    assert "ALTER TABLE ref.team" not in sql


def test_roadmap_has_section_610_with_measured_numbers():
    """路线图 §6.10 必须记录实测数字（防止只改代码不改文档）。"""
    road = ROADMAP.read_text(encoding="utf-8")
    assert "### 6.10 两条遗留收尾" in road
    for token in ("397", "152", "245", "ref.team_sporttery", "226", "3 个 side"):
        assert token in road, token


# ────────────────────────── B. 物化 CLI ──────────────────────────

def _mock_conn():
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (6048, 9529, 267, 0)
    return conn


def test_materialize_dry_run_rolls_back_and_never_commits(monkeypatch):
    """默认预演 ⇒ **回滚**、**不提交**（一个字节都不落库）。"""
    conn = _mock_conn()
    monkeypatch.setattr(materialize.pg, "connect", lambda role: conn)
    monkeypatch.setattr(materialize, "run_fixtures", lambda c, h: {"fixtures": 1})
    monkeypatch.setattr(materialize, "run_results", lambda c, h: {"results": 1})
    assert materialize.main([]) == 0
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_materialize_execute_commits_and_does_not_roll_back(monkeypatch):
    """--execute ⇒ 提交，不回滚。"""
    conn = _mock_conn()
    monkeypatch.setattr(materialize.pg, "connect", lambda role: conn)
    monkeypatch.setattr(materialize, "run_fixtures", lambda c, h: {"fixtures": 1})
    monkeypatch.setattr(materialize, "run_results", lambda c, h: {"results": 1})
    assert materialize.main(["--execute"]) == 0
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_materialize_rejects_conflicting_scope_flags(capsys):
    """--fixtures-only 与 --results-only 互斥 ⇒ 返回码 2（参数错必须明说，不能静默）。"""
    assert materialize.main(["--fixtures-only", "--results-only"]) == 2


def test_materialize_reports_before_and_after_counts(monkeypatch, capsys):
    """输出必须带 before/after 行数 —— 幂等与否全靠它对账。"""
    conn = _mock_conn()
    monkeypatch.setattr(materialize.pg, "connect", lambda role: conn)
    monkeypatch.setattr(materialize, "run_fixtures", lambda c, h: {"fixtures": 1})
    monkeypatch.setattr(materialize, "run_results", lambda c, h: {"results": 1})
    assert materialize.main(["--execute"]) == 0
    out = capsys.readouterr().out
    payload = out[out.index("{"):out.rindex("}") + 1]
    import json
    data = json.loads(payload)
    assert data["mode"] == "execute" and data["written"] is True
    assert data["before"]["fact_fixture"] == 6048
    assert data["after"]["fact_fixture"] == 6048
    assert "scope" in data and "blocks" in data["scope"]


def test_materialize_connect_failure_returns_2(monkeypatch):
    """连不上库 ⇒ 返回码 2 且**明说**（绝不降级到别的角色）。"""
    def boom(role):
        raise RuntimeError("DSN_ING 未设置")
    monkeypatch.setattr(materialize.pg, "connect", boom)
    assert materialize.main([]) == 2


def test_materialize_has_entrypoint():
    """CLI 必须有 __main__ 入口（少了它 = 静默什么都不做、退出码 0）。"""
    src = (SCRIPTS / "store" / "materialize.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in src
    assert "raise SystemExit(main())" in src


# ────────────────────────── 有库集成测试（连不上 skip）──────────────────────────

@pytest.fixture()
def conn():
    try:
        from store import pg
        connection = pg.connect("ing")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no db: {type(exc).__name__}: {exc}")
    yield connection
    connection.rollback()
    connection.close()


def test_db_mapping_table_has_unique_ids_and_bexi(conn):
    """真库：映射表已建、一个官方 id 只属一支队、**279 → #2355 / 506 → #2351**。

    ⚠️ **2026-10-23 订正**：本断言原先写「279 与 506 都指向 #2351」，那是
    **2026-10-22 纠正之前**的旧状态。纠正依据（README v4.6 / 路线图 §6.13.2）：
    279 的 payload 字面是「**贝西克塔斯**」，挂在「毕尔巴鄂竞技」(#2351) 头上是
    **从对手反推球队的猜测**；用俱乐部身份真主键 `af_id` 核对：
    #2351 毕尔巴鄂竞技 af_id=531 → Athletic Club（506 归它 ✓）、
    #2355 皇家贝蒂斯 af_id=543 → Real Betis（**279 + 505 归它 ✓**）
    ⇒ **279 已改指 #2355**，506 仍指 #2351。生产库实测：
    `SELECT jc_id, team_id FROM ref.team_sporttery WHERE jc_id IN (279,506)`
    ⇒ `279|2355` / `506|2351`。
    ⇒ **生产是对的、是这条断言旧了**（本条曾因此在 Oracle 上 failed）。
    """
    exists = conn.execute("SELECT to_regclass('ref.team_sporttery') IS NOT NULL").fetchone()[0]
    assert exists, "ref.team_sporttery 未建（先跑 docs/db/suggestion10_team_sporttery_map.sql）"
    dup = conn.execute("SELECT count(*) FROM (SELECT sport, jc_id FROM ref.team_sporttery "
                       "GROUP BY sport, jc_id HAVING count(*) > 1) d").fetchone()[0]
    assert dup == 0, f"有 {dup} 个官方 id 指向多支行"
    rows = conn.execute("SELECT jc_id, team_id FROM ref.team_sporttery "
                         "WHERE jc_id IN (279, 506) ORDER BY jc_id").fetchall()
    assert rows == [(279, 2355), (506, 2351)], rows


def test_db_resolve_bexi_both_ids(conn):
    """真库：resolve_jc_teams(279, 506) → (#2355 皇家贝蒂斯, #2351 毕尔巴鄂竞技)。

    ⚠️ 2026-10-23 订正：见上一条的说明 —— 279 已改指 #2355（纠正前是 #2351）。
    """
    home, away = jc_write.resolve_jc_teams(conn.cursor(), 279, 506)
    assert (home, away) == (2355, 2351)


def test_db_materialize_dry_run_writes_nothing(conn):
    """真库：dry-run 跑完，fact 行数**一模一样**。"""
    before = materialize.counts(conn)
    materialize.materialize(conn, None)
    conn.rollback()
    after = materialize.counts(conn)
    assert before == after
