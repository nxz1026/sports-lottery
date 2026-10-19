"""配置层回归锁（README §2-D9）：开关解析、逐源默认表、env 覆盖、关闭路径零副作用（不发请求/不写 raw/不记账本）。
每条用例都从「没有任何 LEAGUE_*」起跑（autouse 清理），零真网络；用真库的两条走单事务收尾 ROLLBACK，连不上即 skip。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from config import (  # noqa: E402
    SourceDisabled,
    SourcePolicy,
    allow_paid_once,
    describe,
    flag,
    source_policy,
    spend_allowed,
)
from ingest import api_get, quota, run_backfill  # noqa: E402
from store import pg  # noqa: E402

ODDS_OFF = {"source": "odds_api", "table": "af_raw", "endpoint": "https://e.invalid/o", "params": {}, "cap": 0}


@pytest.fixture(autouse=True)
def _clean_league_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("LEAGUE_")]: monkeypatch.delenv(key)


@pytest.fixture()
def conn():
    try:
        connection = pg.connect("ing")
    except Exception as exc:
        pytest.skip(f"no db: {type(exc).__name__}: {exc}")
    connection.autocommit = True
    connection.execute("BEGIN")
    yield connection
    connection.rollback()
    connection.close()


@pytest.mark.parametrize("raw,expected", [("1", True), ("ON", True), ("yes", True),
                                          ("0", False), ("false", False), ("No", False)])
def test_flag_six_spellings(monkeypatch, raw, expected):
    monkeypatch.setenv("LEAGUE_SWITCH", f" {raw} ")  # 大小写不敏感、去空白
    assert flag("SWITCH") is expected


def test_flag_and_cap_reject_unparsable_values(monkeypatch):
    assert (flag("SWITCH"), flag("SWITCH", True)) == (False, True)  # 未设＝default
    for env, bad, target in (("LEAGUE_SWITCH", "maybe", lambda: flag("SWITCH")),
                             ("LEAGUE_QUOTA_CAP_ODDS_API", "-1", lambda: source_policy("odds_api"))):
        monkeypatch.setenv(env, bad)
        with pytest.raises(ValueError, match=env):
            target()


def test_default_table_and_env_overrides(monkeypatch):
    """T2：不设任何 LEAGUE_* 时两免费源可用（cap 100/30），odds_api 一律不可用（默认关＝不需用户记得设）。"""
    assert source_policy("api_football") == SourcePolicy("api_football", True, 100, "API_FOOTBALL_KEY", False)
    assert source_policy("football_data") == SourcePolicy("football_data", True, 30, "FOOTBALL_DATA_API_KEY", False)
    assert source_policy("odds_api") == SourcePolicy("odds_api", False, 0, "NBA_API_KEY", True)
    assert (source_policy("odds_api").enabled, source_policy("api_football").daily_cap) == (False, 100)  # 各钉一条
    assert [row["source"] for row in describe()] == ["api_football", "football_data", "odds_api"]  # 行数==源数
    assert spend_allowed("api_football") is None and spend_allowed("football_data") is None
    monkeypatch.setenv("LEAGUE_QUOTA_CAP_FOOTBALL_DATA", "7")
    monkeypatch.setenv("LEAGUE_SOURCE_API_FOOTBALL", "off")
    assert [d["daily_cap"] for d in describe() if d["source"] == "football_data"] == [7]
    with pytest.raises(SourceDisabled) as exc:
        spend_allowed("api_football")
    assert exc.value.source == "api_football" and "LEAGUE_SOURCE_API_FOOTBALL" in exc.value.why
    with pytest.raises(ValueError, match="unknown source"):
        source_policy("no_such_source")


def test_paid_source_needs_one_shot_allowance(monkeypatch):
    monkeypatch.setenv("LEAGUE_SOURCE_ODDS_API", "on")
    with pytest.raises(SourceDisabled, match="LEAGUE_ALLOW_PAID"):
        spend_allowed("odds_api")
    allow_paid_once()  # 只写进程内 env，不落 .env
    assert spend_allowed("odds_api") is None and os.environ["LEAGUE_ALLOW_PAID"] == "on"


def test_disabled_source_costs_nothing(conn, monkeypatch):
    """T3：关闭的源零副作用——不碰库（假连接 execute 一被调到就炸）、不发 HTTP、不花配额；真库账本一行不多。"""
    calls = []
    monkeypatch.setattr(api_get.urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(quota, "charge", lambda *a, **k: calls.append(a))
    with pytest.raises(SourceDisabled, match="LEAGUE_SOURCE_ODDS_API"):  # 假连接：任何 SQL 一被调到就炸
        api_get.fetch(SimpleNamespace(execute=lambda *a, **k: pytest.fail("不许碰库")), **ODDS_OFF)
    before = conn.execute("SELECT count(*) FROM ops.quota_ledger").fetchone()[0]
    with pytest.raises(SourceDisabled):
        api_get.fetch(conn, **ODDS_OFF)
    assert calls == [] and conn.execute("SELECT count(*) FROM ops.quota_ledger").fetchone()[0] == before


def test_plan_prints_policy_table_with_closed_sources(conn, caplog):
    """T5：--plan 顶部策略表点出关闭源（含"关闭"字样与控制它的 env 变量），两免费源今日剩余照旧、零 HTTP。"""
    assert run_backfill.main(["--plan", "--seasons", "2099"], conn=conn) == 0
    assert "[策略] odds_api 关闭" in caplog.text and "LEAGUE_SOURCE_ODDS_API" in caplog.text
    assert "api_football 今日剩余" in caplog.text and "football_data 今日剩余" in caplog.text
