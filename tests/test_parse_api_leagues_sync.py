"""P2-22 AF/FD 联赛映射单一真源契约。

``scripts/store/parse_api.AF_LEAGUES`` / ``FD_LEAGUES`` 派生自
``scripts/core/leagues.LEAGUE_CONFIG``（P2-22 迁移）。本测试守住：

1. 派生结果 = 历史硬编码值（向后兼容 ABI）
2. 与 ``core/leagues.LEAGUE_CONFIG`` 等势：每个足球联赛 key 都同时
   出现在 AF + FD 两张表里
3. NBA 等无 ``api_football_id`` / ``league_id`` 字段的项自动跳过
4. 加新联赛到 LEAGUE_CONFIG 不会漂移：临时注一枚 CSL（api_football_id=472），
   断言 AF_LEAGUES 跟随、FD_LEAGUES 不跟随（无 league_id），测试完恢复
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def test_af_leagues_matches_historical():
    """AF_LEAGUES 与 P2-22 迁移前硬编码值一致（向后兼容）。"""
    from store import parse_api

    assert parse_api.AF_LEAGUES == {
        39: "epl", 140: "laliga", 78: "bundesliga", 135: "seriea", 61: "ligue1",
    }


def test_fd_leagues_matches_historical():
    """FD_LEAGUES 与 P2-22 迁移前硬编码值一致。"""
    from store import parse_api

    assert parse_api.FD_LEAGUES == {
        "PL": "epl", "PD": "laliga", "BL1": "bundesliga",
        "SA": "seriea", "FL1": "ligue1",
    }


def test_nba_excluded_from_both():
    """NBA（无 api_football_id / league_id）不污染 AF_LEAGUES / FD_LEAGUES。"""
    from core.leagues import LEAGUE_CONFIG
    from store import parse_api

    # LEAGUE_CONFIG 含 nba 但 nba 没有 api_football_id / league_id 字段
    assert "nba" in LEAGUE_CONFIG
    assert "nba" not in parse_api.AF_LEAGUES.values()
    assert "nba" not in parse_api.FD_LEAGUES.values()


def test_af_fd_keys_pair_with_league_config_football():
    """每个足球联赛 key（去 nba）都同时出现在 AF + FD 两张表里。"""
    from core.leagues import LEAGUE_CONFIG
    from store import parse_api

    football_keys = {
        k for k, v in LEAGUE_CONFIG.items()
        if isinstance(v.get("api_football_id"), int) and isinstance(v.get("league_id"), str)
    }
    af_keys = set(parse_api.AF_LEAGUES.values())
    fd_keys = set(parse_api.FD_LEAGUES.values())

    assert football_keys == af_keys == fd_keys, (
        f"足球联赛 key 三处等势破裂：LEAGUE_CONFIG={football_keys} "
        f"AF={af_keys} FD={fd_keys}"
    )


def test_backfill_validate_leagues_api_id_matches_league_config():
    """``backfill_validate.LEAGUES`` 的 api_football_id 与 LEAGUE_CONFIG 对账。

    backfill_validate.LEAGUES 是 CLI 校验脚本独有结构（含 filename_suffix
    + 期望场数），api_football_id 必须与 LEAGUE_CONFIG 一致。
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from backfill_validate import LEAGUES
    from core.leagues import LEAGUE_CONFIG

    for lg_key, (_suffix, af_id, _expected) in LEAGUES.items():
        cfg_af_id = LEAGUE_CONFIG[lg_key].get("api_football_id")
        assert cfg_af_id == af_id, (
            f"backfill_validate.LEAGUES[{lg_key!r}] api_football_id={af_id} "
            f"≠ LEAGUE_CONFIG[{lg_key!r}]['api_football_id']={cfg_af_id}"
        )


def test_adding_new_league_to_config_propagates_to_af(monkeypatch):
    """临时在 LEAGUE_CONFIG 注一枚 CSL（api_football_id=472）→ AF_LEAGUES 跟随；
    FD_LEAGUES 因 CSL 缺 league_id 不跟随。"""
    from core import leagues
    monkeypatch.setitem(
        leagues.LEAGUE_CONFIG, "csl",
        {"api_football_id": 472, "league_id": None, "name": "Test CSL"},
    )
    # force re-import parse_api to pick up the monkeypatched LEAGUE_CONFIG
    import importlib

    from store import parse_api
    importlib.reload(parse_api)

    assert parse_api.AF_LEAGUES.get(472) == "csl"
    assert "csl" not in parse_api.FD_LEAGUES.values()


def test_removing_league_from_config_propagates(monkeypatch):
    """临时从 LEAGUE_CONFIG 删 epl → AF/FD 都不含 epl。"""
    from core import leagues
    monkeypatch.delitem(leagues.LEAGUE_CONFIG, "epl")

    import importlib

    from store import parse_api
    importlib.reload(parse_api)

    assert "epl" not in parse_api.AF_LEAGUES.values()
    assert "epl" not in parse_api.FD_LEAGUES.values()
