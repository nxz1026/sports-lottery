"""R25 #2: scripts/import_backfill.py 全 0 直测补测。

现状：`_adapt / _load / main` 三个函数 0 直测；`import_backfill.py` 把 API-
Football 格式平铺数据适配为 references/historical_past_matches.json 训练样本
的标准格式，靠手动跑 + 看 log 行验证。失败模式为 silent logger.warning。

本轮补 10 测，覆盖：
- _adapt 5：缺 fulltime / 非 int 比分 / 负数比分 / 缺 teams 或 kickoff /
  全字段完整输出 7 字段
- _load 3：路径不存在 → [] / 文件坏 JSON → [] / 顶层无 fixtures 键 → [] sk=0
- main() 2：3 league 文件存在 → 调 3 次 append；--dry-run 不调 append

关键设计：用 importlib.util.spec_from_file_location 把脚本加载到独立
module name。BACKFILL_FILES 在 import 时求值（含 RESULTS_DIR），需要
monkeypatch bf_mod.BACKFILL_FILES 整条改向 tmp_path 文件。main() 内部
`from core.calibration import append_historical_past_matches` 用 sys.modules
预注入 fake 模块拦截。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

MOD_PATH = REPO_ROOT / "scripts" / "import_backfill.py"


@pytest.fixture
def ib_mod(tmp_path, monkeypatch):
    """加载 import_backfill 为唯一 module name，把 BACKFILL_FILES 改向 tmp。"""
    spec = importlib.util.spec_from_file_location(
        "ib_test", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # BACKFILL_FILES 是 list[tuple] 模块顶层常量求值（含真实 RESULTS_DIR）。
    # 整条 monkeypatch 到 tmp_path；tests 自行写入 fixture 文件。
    new_files = [
        ("epl",        tmp_path / "backfill_epl_2022-2024.json"),
        ("laliga",     tmp_path / "backfill_laliga_2022-2024.json"),
        ("seriea",     tmp_path / "backfill_seriea_2022-2024.json"),
        ("bundesliga", tmp_path / "backfill_bundesliga_2022-2024.json"),
        ("ligue1",     tmp_path / "backfill_ligue1_2022-2024.json"),
    ]
    monkeypatch.setattr(mod, "BACKFILL_FILES", new_files)
    return mod, tmp_path


def _write_fixture(path: Path, fixtures: list[dict], *, no_fixtures_key: bool = False,
                    bad_json: bool = False) -> None:
    if bad_json:
        path.write_text("{not valid json", encoding="utf-8")
        return
    payload = {"league": "demo"} if no_fixtures_key else {"fixtures": fixtures}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ──────────────────────── _adapt 边界 ────────────────────────


def test_adapt_skips_when_fulltime_missing(ib_mod):
    """R25 #2：score.fulltime.home 或 .away 缺失 → None。"""
    ib_mod, _ = ib_mod
    # 仅 home 缺
    assert ib_mod._adapt({"score": {"fulltime": {"home": None, "away": 2}},
                          "teams": {"home": {"name": "A"}, "away": {"name": "B"}},
                          "fixture": {"date": "2024-01-01"}}) is None
    # 仅 away 缺
    assert ib_mod._adapt({"score": {"fulltime": {"home": 2, "away": None}},
                          "teams": {"home": {"name": "A"}, "away": {"name": "B"}},
                          "fixture": {"date": "2024-01-01"}}) is None
    # 整 score 全缺
    assert ib_mod._adapt({"score": {}, "teams": {"home": {"name": "A"}, "away": {"name": "B"}},
                          "fixture": {"date": "2024-01-01"}}) is None


def test_adapt_skips_when_score_non_integer(ib_mod):
    """R25 #2：score 含非 int 字符串（"2.5" / "abc" / "None"）→ None。"""
    ib_mod, _ = ib_mod
    base = lambda s: {"score": {"fulltime": {"home": s, "away": 0}},
                      "teams": {"home": {"name": "A"}, "away": {"name": "B"}},
                      "fixture": {"date": "2024-01-01"}}
    assert ib_mod._adapt(base("2.5")) is None  # int("2.5") 抛 ValueError
    assert ib_mod._adapt(base("abc")) is None
    assert ib_mod._adapt(base(None)) is None  # type(None) → TypeError


def test_adapt_skips_negative_scores(ib_mod):
    """R25 #2：分数 < 0 → None（API-Football 不应产出负分，跳过保护）。"""
    ib_mod, _ = ib_mod
    fx = {"score": {"fulltime": {"home": -1, "away": 2}},
          "teams": {"home": {"name": "A"}, "away": {"name": "B"}},
          "fixture": {"date": "2024-01-01"}}
    assert ib_mod._adapt(fx) is None
    fx = {"score": {"fulltime": {"home": 2, "away": -2}},
          "teams": {"home": {"name": "A"}, "away": {"name": "B"}},
          "fixture": {"date": "2024-01-01"}}
    assert ib_mod._adapt(fx) is None


def test_adapt_skips_when_teams_or_kickoff_missing(ib_mod):
    """R25 #2：home.name / away.name / fixture.date 任一为空或缺失 → None。"""
    ib_mod, _ = ib_mod
    full_score = {"home": 2, "away": 1}
    # 缺 home
    assert ib_mod._adapt({"score": {"fulltime": full_score},
                          "teams": {"home": {"name": ""}, "away": {"name": "B"}},
                          "fixture": {"date": "2024-01-01"}}) is None
    # 缺 away
    assert ib_mod._adapt({"score": {"fulltime": full_score},
                          "teams": {"home": {"name": "A"}, "away": {"name": None}},
                          "fixture": {"date": "2024-01-01"}}) is None
    # 缺 kickoff
    assert ib_mod._adapt({"score": {"fulltime": full_score},
                          "teams": {"home": {"name": "A"}, "away": {"name": "B"}},
                          "fixture": {}}) is None
    # kickoff 空字符串（`not kickoff` 命中 None 或 ""）
    assert ib_mod._adapt({"score": {"fulltime": full_score},
                          "teams": {"home": {"name": "A"}, "away": {"name": "B"}},
                          "fixture": {"date": ""}}) is None


def test_adapt_full_record_returns_7_fields(ib_mod):
    """R25 #2：全字段完整（扁平 dotted key，与 backfill_seasons.py 输出对齐）→ 7 字段。"""
    ib_mod, _ = ib_mod
    # _adapt 期望扁平 dotted key（fx.get("score.fulltime.home") 是字面查找）
    fx = {
        "fixture.id": 100,
        "fixture.date": "2024-03-15T15:00:00+00:00",
        "league.id": 39,
        "teams.home.name": "Liverpool",
        "teams.away.name": "Arsenal",
        "score.fulltime.home": 2,
        "score.fulltime.away": 1,
        "score.halftime.home": 1,
        "score.halftime.away": 0,
    }
    out = ib_mod._adapt(fx)
    assert out == {
        "name": "Liverpool vs Arsenal",
        "status": "STATUS_FULL_TIME",
        "completed": True,
        "kickoff_utc": "2024-03-15T15:00:00+00:00",
        "home": "Liverpool",
        "away": "Arsenal",
        "score": "2-1",
    }


# ──────────────────────── _load 文件层 fallback ────────────────────────


def test_load_returns_empty_when_file_missing(ib_mod, caplog):
    """R25 #2：路径不存在 → 返回 []，log warning，不抛穿。"""
    ib_mod, tmp = ib_mod
    rows = ib_mod._load("epl", tmp / "nope.json")
    assert rows == []
    # 验证 warning 已触发
    assert any("文件不存在" in r.message for r in caplog.records)


def test_load_returns_empty_when_json_invalid(ib_mod):
    """R25 #2：bad JSON → 返回 []，不抛穿。"""
    ib_mod, tmp = ib_mod
    bad = tmp / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    assert ib_mod._load("epl", bad) == []


def test_load_returns_empty_when_no_fixtures_key(ib_mod):
    """R25 #2：顶层无 fixtures 键 → 返回 []，skipped=0。"""
    ib_mod, tmp = ib_mod
    path = tmp / "no_fixtures.json"
    _write_fixture(path, [], no_fixtures_key=True)
    assert ib_mod._load("epl", path) == []


# ──────────────────────── main() 多 league + dry-run ────────────────────────


def test_main_calls_append_per_league_with_dry_run(ib_mod, monkeypatch, tmp_path):
    """R25 #2：--dry-run → 不调 append，只输出日志含 'dry-run'。"""
    ib_mod, tmp = ib_mod
    # 3 league 文件写入（每个含 2 fixture），扁平 dotted key 与 _adapt 契约对齐
    fixtures = [{
        "fixture.id": 1,
        "fixture.date": "2024-01-01T15:00:00+00:00",
        "teams.home.name": "A",
        "teams.away.name": "B",
        "score.fulltime.home": 2,
        "score.fulltime.away": 1,
    }]
    for league in ("epl", "laliga", "seriea"):
        path = tmp / f"backfill_{league}_2022-2024.json"
        _write_fixture(path, fixtures)

    monkeypatch.setattr(sys, "argv", ["import_backfill.py", "--dry-run"])
    append_calls = []
    # 用 monkeypatch.setitem 自动清理 sys.modules，防止后续 predict.py 测试被污染
    monkeypatch.setitem(sys.modules, "core.calibration", SimpleNamespace(
        append_historical_past_matches=lambda lk, rows: append_calls.append((lk, len(rows))) or 0))

    ib_mod.main()

    # dry-run 下应该不调 append
    assert append_calls == []


def test_main_invokes_append_with_per_league_count(ib_mod, monkeypatch, tmp_path):
    """R25 #2：3 league 文件存在 → 调 3 次 append；append 返回值累加 total。"""
    ib_mod, tmp = ib_mod
    # 各 league fixtures 数不同（验证 after-append 总计是正确的）
    counts = {"epl": 2, "laliga": 0, "seriea": 5}
    for league, n in counts.items():
        path = tmp / f"backfill_{league}_2022-2024.json"
        if n > 0:
            # _load 输入 backfill_seasons.py 输出格式 = 扁平 dotted key
            fixtures = [{
                "fixture.id": i + 1,
                "fixture.date": f"2024-01-{i+1:02d}T15:00:00+00:00",
                "teams.home.name": f"H{i}",
                "teams.away.name": f"A{i}",
                "score.fulltime.home": 1,
                "score.fulltime.away": 0,
            } for i in range(n)]
            _write_fixture(path, fixtures)

    monkeypatch.setattr(sys, "argv", ["import_backfill.py"])
    append_calls = {}

    def fake_append(lk, rows):
        append_calls[lk] = len(rows)
        return len(rows)

    # monkeypatch.setitem 自动还原 sys.modules，防止后续 predict.py 等
    # `from core.calibration import ...` 拿到 SimpleNamespace 而失败
    monkeypatch.setitem(sys.modules, "core.calibration", SimpleNamespace(
        append_historical_past_matches=fake_append))

    ib_mod.main()

    # 仅 epl + seriea 有数据，laliga 因文件缺被 skipped
    assert append_calls == {"epl": 2, "seriea": 5}
