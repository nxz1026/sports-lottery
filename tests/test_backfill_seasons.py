"""R25 #1: scripts/backfill_seasons.py 全 0 直测补测。

现状：`grep -E "backfill_seasons" tests/` 返回 0 行；该脚本是纯 helper + 一处
network + 顶层 argparse/JSON 编排，所有失败统一为 silent logger.warning，再
归时本地无 anchor 定位。

本轮补 7 测，覆盖：
- _dig 路径取值：深 nested 命中 / 中间非 dict 返 None / 末段 key 缺失返 None
- _extract_fixture：完整 fixture schema / 缺 fulltime 字段值为 None
- fetch_season_with_retry：重试 + 退避（time.sleep 拦截）/ 二次仍 raise 上抛
- main() argparse 守门：缺 API_FOOTBALL_KEY / 未知 league / 空 seasons / 正常路径

关键设计：用 importlib.util.spec_from_file_location 把脚本加载到独立
module name，避免污染 sys.modules；monkeypatch 模块本地 fetch_season 跳过
network；RESULTS_DIR/LOG_DIR 改 patch 到 tmp_path 不污染真实目录。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

MOD_PATH = REPO_ROOT / "scripts" / "backfill_seasons.py"


@pytest.fixture
def bf_mod(tmp_path, monkeypatch):
    """加载 backfill_seasons 为唯一 module name，monkeypatch IO/net 到 tmp。"""
    spec = importlib.util.spec_from_file_location(
        "bf_seasons_test", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # IO 目录重定向到 tmp_path（脚本顶层写死的 REPO_ROOT/scripts/results）
    monkeypatch.setattr(mod, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(mod, "LOG_DIR", tmp_path / "logs")
    return mod


# ──────────────────────── _dig 路径取值 ────────────────────────


def test_dig_returns_value_from_deep_nested_path(bf_mod):
    """R25 #1：5 层 nested → 精确命中（fixture.id 这类深路径）。"""
    obj = {"fixture": {"date": "2024-01-01",
                        "teams": {"home": {"name": "Liverpool"},
                                  "away": {"name": "Arsenal"}}}}
    assert bf_mod._dig(obj, "fixture.date") == "2024-01-01"
    assert bf_mod._dig(obj, "fixture.teams.home.name") == "Liverpool"
    assert bf_mod._dig(obj, "fixture.teams.away.name") == "Arsenal"


def test_dig_returns_none_when_intermediate_not_dict(bf_mod):
    """R25 #1：中间节点不是 dict（如 int / list / str）→ 返 None 不抛错。"""
    obj = {"score": {"fulltime": {"home": 2}},  # home 是 int
           "teams": "invalid"}  # teams 是 str
    assert bf_mod._dig(obj, "score.fulltime.home.team") is None
    assert bf_mod._dig(obj, "teams.home.name") is None
    # 末段 key 不存在
    assert bf_mod._dig(obj, "score.fulltime.away") is None
    # 空 dict
    assert bf_mod._dig(obj, "") is None  # 空路径整段 split → ['']，cur={}, '' not in cur


# ──────────────────────── _extract_fixture ────────────────────────


def test_extract_fixture_complete_schema(bf_mod):
    """R25 #1：完整 fixture → 9 个 FIXTURE_FIELDS 全部命中。"""
    f = {
        "fixture": {"id": 12345, "date": "2024-01-01"},
        "league": {"id": 39},
        "teams": {"home": {"name": "LFC"}, "away": {"name": "ARS"}},
        "score": {"fulltime": {"home": 2, "away": 1},
                   "halftime": {"home": 1, "away": 0}},
    }
    out = bf_mod._extract_fixture(f)
    assert set(out) == set(bf_mod.FIXTURE_FIELDS)
    assert out["fixture.id"] == 12345
    assert out["fixture.date"] == "2024-01-01"
    assert out["league.id"] == 39
    assert out["teams.home.name"] == "LFC"
    assert out["teams.away.name"] == "ARS"
    assert out["score.fulltime.home"] == 2
    assert out["score.fulltime.away"] == 1
    assert out["score.halftime.home"] == 1
    assert out["score.halftime.away"] == 0


def test_extract_fixture_missing_fulltime_is_none(bf_mod):
    """R25 #1：fixture 缺 fulltime（未完赛）→ fulltime 字段值 None，其余保留。"""
    f = {
        "fixture": {"id": 99999, "date": "2099-12-31"},
        "league": {"id": 39},
        "teams": {"home": {"name": "Future"}, "away": {"name": "TBD"}},
        "score": {"halftime": {"home": 0, "away": 0}},  # 缺 fulltime
    }
    out = bf_mod._extract_fixture(f)
    assert out["score.fulltime.home"] is None
    assert out["score.fulltime.away"] is None
    assert out["score.halftime.home"] == 0
    assert out["teams.home.name"] == "Future"


# ──────────────────────── fetch_season_with_retry 重试 + 退避 ────────────────────────


def test_fetch_season_with_retry_succeeds_after_one_failure(bf_mod, monkeypatch):
    """R25 #1：第一次 raise RuntimeError，第二次成功 → 返 (fixtures, retries=1)；time.sleep 调 1 次。"""
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)

    # 第一次 raise、第二次返 fixture
    fake_payload = [{"fixture": {"id": 1}, "score": {"fulltime": {"home": 1, "away": 0}}}]

    call_count = {"n": 0}

    def fake_fetch_season(league_id, season, api_key):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("模拟 429")
        return fake_payload

    monkeypatch.setattr(bf_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(bf_mod, "fetch_season", fake_fetch_season)

    fixtures, retries = bf_mod.fetch_season_with_retry(39, 2024, "fake-key")
    assert fixtures == fake_payload
    assert retries == 1
    assert call_count["n"] == 2
    # sleep 调一次（attempt=0 时 sleep；第二次 fetch 直接成功不再 sleep）
    assert sleeps == [bf_mod.REQUEST_INTERVAL_SECONDS]


def test_fetch_season_with_retry_raises_after_exhaustion(bf_mod, monkeypatch):
    """R25 #1：SEASON_RETRIES+1 次全失败 → 最后 raise 上抛；中间 sleep 拦截。"""
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)

    def fake_fetch_season(league_id, season, api_key):
        raise RuntimeError("持续 429")

    monkeypatch.setattr(bf_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(bf_mod, "fetch_season", fake_fetch_season)

    with pytest.raises(RuntimeError, match="持续 429"):
        bf_mod.fetch_season_with_retry(39, 2024, "fake-key")
    # SEASON_RETRIES=1：attempt=0 失败 sleep，attempt=1 失败 raise 上抛前不再 sleep
    assert sleeps == [bf_mod.REQUEST_INTERVAL_SECONDS]


# ──────────────────────── main() argparse 守门 ────────────────────────


def test_main_returns_2_when_api_key_missing(bf_mod, monkeypatch, capsys):
    """R25 #1：API_FOOTBALL_KEY 未设 → 返回 2，不写结果。"""
    monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
    monkeypatch.setattr(bf_mod, "fetch_season_with_retry", lambda *a, **k: ([], 0))
    monkeypatch.setattr(sys, "argv", ["backfill_seasons.py", "--league", "epl", "--seasons", "2024"])

    rc = bf_mod.main()
    assert rc == 2
    # 早 return 不会 mkdir，目录不存在
    assert not bf_mod.RESULTS_DIR.exists()


def test_main_returns_2_when_league_unknown(bf_mod, monkeypatch):
    """R25 #1：league 不在 LEAGUE_CONFIG → 返回 2。"""
    monkeypatch.setenv("API_FOOTBALL_KEY", "fake-key")
    # LEAGUE_CONFIG 是 from-import 别名 → 直接 monkeypatch bf_mod.LEAGUE_CONFIG
    monkeypatch.setattr(bf_mod, "LEAGUE_CONFIG", {})
    monkeypatch.setattr(sys, "argv", ["backfill_seasons.py", "--league", "unknown", "--seasons", "2024"])

    rc = bf_mod.main()
    assert rc == 2


def test_main_returns_2_when_seasons_empty(bf_mod, monkeypatch, capsys):
    """R25 #1：--seasons="" / "," 全部 → seasons 解析为空 → 返回 2。"""
    monkeypatch.setenv("API_FOOTBALL_KEY", "fake-key")
    monkeypatch.setattr(sys, "argv", ["backfill_seasons.py", "--league", "epl", "--seasons", ""])
    assert bf_mod.main() == 2
    monkeypatch.setattr(sys, "argv", ["backfill_seasons.py", "--league", "epl", "--seasons", ","])
    assert bf_mod.main() == 2


def test_main_happy_path_writes_files_and_report(bf_mod, monkeypatch):
    """R25 #1：正常路径 → 写 RESULTS_DIR/backfill_epl_2023-2024.json + LOG_DIR 报告。"""
    monkeypatch.setenv("API_FOOTBALL_KEY", "fake-key")
    monkeypatch.setattr(bf_mod, "LEAGUE_CONFIG", {"epl": {"api_football_id": 39}})

    def fake(league_id, season, api_key):
        # 每季返回 2 fixture + retries 计数（必须返回 tuple，与源契约对齐）
        fixtures = [
            {"fixture": {"id": season * 10 + 1, "date": f"{season}-01-01"},
             "league": {"id": 39},
             "teams": {"home": {"name": "A"}, "away": {"name": "B"}},
             "score": {"fulltime": {"home": 1, "away": 0},
                       "halftime": {"home": 0, "away": 0}}},
            {"fixture": {"id": season * 10 + 2, "date": f"{season}-01-02"},
             "league": {"id": 39},
             "teams": {"home": {"name": "C"}, "away": {"name": "D"}},
             "score": {"fulltime": {"home": 2, "away": 1},
                       "halftime": {"home": 1, "away": 1}}},
        ]
        return fixtures, 0  # (fixtures, retries)
    monkeypatch.setattr(bf_mod, "fetch_season_with_retry", fake)
    # sleep 拦截
    monkeypatch.setattr(bf_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys, "argv",
                        ["backfill_seasons.py", "--league", "epl", "--seasons", "2023,2024"])

    rc = bf_mod.main()
    assert rc == 0

    # 检查写出文件
    out_files = list(bf_mod.RESULTS_DIR.iterdir())
    assert len(out_files) == 1
    payload = json.loads(out_files[0].read_text(encoding="utf-8"))
    assert payload["league"] == "epl"
    assert payload["seasons"] == [2023, 2024]
    # 2 季 × 2 fixture = 4
    assert len(payload["fixtures"]) == 4
    # 关键字段非 None（验证 lambda 不是返回空 dict）
    assert payload["fixtures"][0]["fixture.id"] is not None
    assert payload["fixtures"][0]["teams.home.name"] == "A"

    # 报告
    report_files = list(bf_mod.LOG_DIR.iterdir())
    assert len(report_files) == 1
    report_text = report_files[0].read_text(encoding="utf-8")
    assert "league=epl" in report_text
    assert "total_fixtures=4" in report_text
    # 报告文件名跟 league 走
    assert report_files[0].name == "ASEL-BACKFILL-EPL.report.txt"
