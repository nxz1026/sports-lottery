"""「竞彩 id 279 挂错队」纠正件的契约测试（路线图 §6.13）。

背景：``docs/db/suggestion10_team_sporttery_map.sql`` 第 3 节手写了
``279 → 2351``（毕尔巴鄂竞技），依据是「从对手反推球队」的猜测；
真相是 **279 = 贝西克塔斯 = Real Betis**（应指向 #2355，af_id=543）。

本测试锁住纠正件的**形状**（不连库）：
  - 唯一真源在 .sql 文件里，Python 入口**不复制**语句
  - 预演 = 同一份 SQL + ROLLBACK
  - 纪律：**不新建 ref.team 行**、**不碰 506**、**只改 279 相关的场次**
  - 目标队用 **af_id** 定位（身份真主键），不写死 team_id
  - DDL 源文件里那条错误推断与错误列注释必须已被翻过来
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = REPO_ROOT / "docs" / "db" / "suggestion13_betis_id_repoint.sql"
DDL10 = REPO_ROOT / "docs" / "db" / "suggestion10_team_sporttery_map.sql"


def _load_mod():
    spec = importlib.util.spec_from_file_location(
        "jc_team_id_repoint_test", REPO_ROOT / "scripts" / "ingest" / "jc_team_id_repoint.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


# ── 纯函数 ──────────────────────────────────────────────────────────────

def test_strip_backup_block_removes_only_backup_statements():
    mod = _load_mod()
    sql = ("-- @backup-block:start\nCREATE TABLE x AS SELECT 1;\n-- @backup-block:end\n"
           "UPDATE y SET z = 1;\n")
    out = mod.strip_backup_block(sql)
    assert "CREATE TABLE" not in out
    assert "UPDATE y SET z = 1;" in out


def test_strip_backup_block_is_noop_without_markers():
    mod = _load_mod()
    sql = "UPDATE y SET z = 1;\n"
    assert mod.strip_backup_block(sql) == sql


def test_to_rollback_replaces_only_the_last_commit():
    mod = _load_mod()
    sql = "BEGIN;\n-- COMMIT; 在注释里\nUPDATE y SET z = 1;\nCOMMIT;\n"
    out = mod.to_rollback(sql)
    assert out.count("COMMIT;") == 1          # 注释里那句没被动
    assert out.rstrip().endswith("ROLLBACK;")
    assert "UPDATE y SET z = 1;" in out


def test_to_rollback_keeps_sql_without_commit():
    mod = _load_mod()
    assert mod.to_rollback("SELECT 1;") == "SELECT 1;"


# ── SQL 文件的形状 ──────────────────────────────────────────────────────

def test_sql_is_single_transaction():
    """单事务（注释里提到的 COMMIT 不算 ⇒ 只数「整行就是」的那些）。"""
    sql = _sql()
    stmts = [ln.strip() for ln in sql.splitlines()]
    assert stmts.count("BEGIN;") == 1
    assert stmts.count("COMMIT;") == 1


def test_sql_asserts_target_exists_before_touching_anything():
    """没有前置断言 ⇒ 目标队行不存在时会静默改 0 行（静默失败）。"""
    sql = _sql()
    assert "DO $$" in sql
    assert "RAISE EXCEPTION" in sql
    assert "af_id = 543" in sql


def test_sql_does_not_create_new_ref_team_rows():
    """纪律：**不新建 ref.team 行**（#2355 就是 Betis 的行）。"""
    sql = _sql().upper()
    assert "INSERT INTO REF.TEAM " not in sql
    assert "INSERT INTO REF.TEAM(" not in sql


def test_sql_does_not_delete_anything():
    sql = _sql().upper()
    assert "DELETE " not in sql
    assert "TRUNCATE" not in sql


def test_sql_updates_mapping_and_both_sides_of_matches():
    sql = _sql()
    assert "UPDATE ref.team_sporttery" in sql
    assert "UPDATE fact.jc_match" in sql
    assert "home_sporttery_id = 279" in sql
    assert "away_sporttery_id = 279" in sql


def test_sql_does_not_touch_jc_506():
    """506 = 毕尔巴鄂竞技，必须原样保留（只改 1 条映射）。"""
    sql = _sql()
    # 只在对账输出里出现 506，不得出现在任何 UPDATE 的 WHERE 里
    for stmt in sql.split(";"):
        if "UPDATE" in stmt.upper() and "606" in stmt:
            raise AssertionError("不该有 606")
        if "UPDATE" in stmt.upper() and "506" in stmt:
            raise AssertionError("UPDATE 语句里出现了 506：" + stmt[:120])
    assert "jc_id = 506" in sql      # 只在验证输出里


def test_sql_fixes_the_column_comment():
    sql = _sql()
    assert "COMMENT ON COLUMN ref.team_sporttery.jc_id" in sql
    assert "皇家贝蒂斯" in sql


def test_sql_has_backup_block_markers():
    """备份块必须带标记：CLI 角色无 CREATE 权限，要能整块剥掉。"""
    sql = _sql()
    assert "-- @backup-block:start" in sql
    assert "-- @backup-block:end" in sql


# ── DDL 源文件里的错误推断必须已被翻过来 ────────────────────────────────

def test_ddl10_no_longer_maps_279_to_bilbao_row():
    ddl = DDL10.read_text(encoding="utf-8")
    assert "'football', 279, 2351" not in ddl
    assert "SELECT 'football', 279, 2351" not in ddl


def test_ddl10_maps_279_to_the_betis_row_by_af_id():
    ddl = DDL10.read_text(encoding="utf-8")
    assert "af_id = 543" in ddl


def test_ddl10_column_comment_is_corrected():
    ddl = DDL10.read_text(encoding="utf-8")
    assert "毕尔巴鄂竞技 506 / 贝西克塔斯 279" not in ddl
    assert "皇家贝蒂斯" in ddl


def test_ddl10_records_the_correction_reason():
    """错误推断必须留痕（否则下一个人会以为那行一直是这么写的）。"""
    ddl = DDL10.read_text(encoding="utf-8")
    assert "2026-10-22 订正" in ddl
    assert "suggestion13_betis_id_repoint.sql" in ddl


# ── CLI 入口 ────────────────────────────────────────────────────────────

def test_cli_has_main_entrypoint():
    """少了 __main__ 入口 ⇒ `python scripts/ingest/jc_team_id_repoint.py` 静默什么都不做。"""
    src = (REPO_ROOT / "scripts" / "ingest" / "jc_team_id_repoint.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in src
    assert "raise SystemExit(main())" in src


def test_cli_defaults_to_dry_run():
    """默认必须预演（真写库要显式 --execute）。"""
    src = (REPO_ROOT / "scripts" / "ingest" / "jc_team_id_repoint.py").read_text(encoding="utf-8")
    assert '"--execute", action="store_true"' in src
    assert "conn.rollback()" in src
