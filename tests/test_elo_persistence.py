"""R29 #3: core/elo.py 持久化 trio (save_elo_ratings / load_elo_ratings /
get_or_init_elo_ratings) + core/rankings.py::fetch_fifa_rankings 直测。

test_elo.py 仅覆盖纯函数层（expected_score/update_elo/process_match_result/
init_ratings_from_fifa/elo_to_lambda_scale/init_ratings_from_fifa）；持久化 trio
完全裸露，FIFA 加载层也仅在 test_build_parser_flags.py 被 monkeypatched。

任何 fs/encoding 回归让冷启动 fallback 不可见，本轮补 14 测锁定。
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


@pytest.fixture
def elo_mod(monkeypatch, tmp_path):
    """加载 core/elo.py 为唯一 module name；monkeypatch _ELO_STATE_FILE 到 tmp。"""
    spec = importlib.util.spec_from_file_location(
        "elo_persist_test", REPO_ROOT / "scripts" / "core" / "elo.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_ELO_STATE_FILE", tmp_path / "elo_state.json")
    return mod


@pytest.fixture
def rankings_mod(monkeypatch, tmp_path):
    """加载 core/rankings.py 为唯一 module name；monkeypatch FOOTBALL_DIR 到 tmp。"""
    spec = importlib.util.spec_from_file_location(
        "rankings_test", REPO_ROOT / "scripts" / "core" / "rankings.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    fake_root = tmp_path
    monkeypatch.setattr(mod, "FOOTBALL_DIR", fake_root)
    return mod


# ──────────────────────── save_elo_ratings ────────────────────────


def test_save_writes_sorted_with_saved_at_and_count(elo_mod):
    """R29 #3：写盘 payload 含 ratings (按 elo 倒序) + _saved_at + _team_count。"""
    ratings = {"Liverpool": 1900.0, "Arsenal": 1800.0, "Chelsea": 1750.0,
              "Spurs": 1700.0, "ManCity": 2000.0}
    elo_mod.save_elo_ratings(ratings)
    payload = json.loads(elo_mod._ELO_STATE_FILE.read_text(encoding="utf-8"))
    # 排序：ManCity 2000 第一
    ratings_list = list(payload["ratings"].items())
    assert ratings_list[0] == ("ManCity", 2000.0)
    assert ratings_list[-1] == ("Spurs", 1700.0)
    assert payload["_team_count"] == 5
    assert "_saved_at" in payload
    # _saved_at 形如 2026-10-15T12:34:56Z
    assert len(payload["_saved_at"]) == 20


def test_save_swallows_oserror(elo_mod, monkeypatch, caplog):
    """R29 #3：open 抛 OSError → 吞 + warning，不抛穿。"""
    def bad_open(*a, **k):
        raise OSError("写不动")

    monkeypatch.setattr("builtins.open", bad_open)
    with caplog.at_level("WARNING"):
        elo_mod.save_elo_ratings({"A": 1500.0})
    assert any("Failed to save ELO ratings" in r.message for r in caplog.records)


# ──────────────────────── load_elo_ratings ────────────────────────


def test_load_returns_none_when_file_missing(elo_mod):
    """R29 #3：_ELO_STATE_FILE 不存在 → None。"""
    assert elo_mod.load_elo_ratings() is None


def test_load_returns_dict_when_valid(elo_mod):
    """R29 #3：写盘 ratings 字典 → load_elo_ratings 返同字典。"""
    ratings = {"A": 1500.0, "B": 1600.0}
    elo_mod.save_elo_ratings(ratings)
    out = elo_mod.load_elo_ratings()
    assert out == ratings


def test_load_returns_none_for_bad_json(elo_mod):
    """R29 #3：坏 JSON → None（不抛穿）。"""
    elo_mod._ELO_STATE_FILE.write_text("{not valid json", encoding="utf-8")
    assert elo_mod.load_elo_ratings() is None


def test_load_returns_none_when_ratings_not_dict(elo_mod):
    """R29 #3：顶层 OK 但 ratings 字段是非 dict（如 list / 空 dict）→ None。"""
    # ratings 是 list
    elo_mod._ELO_STATE_FILE.write_text(json.dumps({
        "ratings": [1, 2, 3], "_saved_at": "2026-01-01T00:00:00Z",
    }), encoding="utf-8")
    assert elo_mod.load_elo_ratings() is None

    # ratings 是空 dict
    elo_mod._ELO_STATE_FILE.write_text(json.dumps({
        "ratings": {}, "_saved_at": "2026-01-01T00:00:00Z",
    }), encoding="utf-8")
    assert elo_mod.load_elo_ratings() is None


def test_load_filters_non_numeric_values(elo_mod):
    """R29 #3：ratings 含字符串等非数值 → 被过滤；数值键保留。"""
    elo_mod._ELO_STATE_FILE.write_text(json.dumps({
        "ratings": {
            "A": 1500.0, "B": "bad-string", "C": None,
            "D": 1600.5, "E": True,  # True == 1 (int)，保留
        },
        "_saved_at": "2026-01-01T00:00:00Z",
    }), encoding="utf-8")
    out = elo_mod.load_elo_ratings()
    assert "A" in out
    assert "D" in out
    assert "E" in out  # True 是 int 子类，被 isinstance(int) 捕获
    assert "B" not in out
    assert "C" not in out


# ──────────────────────── get_or_init_elo_ratings ────────────────────────


def test_get_or_init_cache_hit_returns_cached(elo_mod):
    """R29 #3：持久化有效且 len>10 → 直接返缓存，**不触发 init_ratings_from_fifa**。"""
    cached = {f"Team{i}": 1500.0 + i for i in range(20)}
    elo_mod.save_elo_ratings(cached)
    init_called = {"n": 0}

    def fake_init(fifa):
        init_called["n"] += 1
        return {}

    elo_mod.init_ratings_from_fifa = fake_init

    out = elo_mod.get_or_init_elo_ratings(fifa_rankings={"Brazil": 1})
    assert out == cached
    assert init_called["n"] == 0  # 缓存命中未触发 init


def test_get_or_init_short_cache_force_init(elo_mod):
    """R29 #3：缓存仅 5 条（<10）→ 即使 fifa=None 也会 init + save（走 force refresh 路径）。"""
    short_cache = {f"T{i}": 1500.0 for i in range(5)}
    elo_mod.save_elo_ratings(short_cache)
    init_called = {"n": 0, "fifa": None}

    def fake_init(fifa):
        init_called["n"] += 1
        init_called["fifa"] = fifa
        return {f"Init{i}": 1500.0 for i in range(20)}

    elo_mod.init_ratings_from_fifa = fake_init

    out = elo_mod.get_or_init_elo_ratings(fifa_rankings={"A": 1})
    assert init_called["n"] == 1
    assert init_called["fifa"] == {"A": 1}  # 透传 fifa
    assert len(out) == 20


def test_get_or_init_force_refresh_skips_cache(elo_mod):
    """R29 #3：force_refresh=True → 忽略缓存直接 init。"""
    elo_mod.save_elo_ratings({f"T{i}": 1500.0 for i in range(20)})  # 大量缓存
    init_called = {"n": 0}

    def fake_init(fifa):
        init_called["n"] += 1
        return {"Fresh": 1500.0}

    elo_mod.init_ratings_from_fifa = fake_init

    out = elo_mod.get_or_init_elo_ratings(fifa_rankings={"A": 1}, force_refresh=True)
    assert init_called["n"] == 1
    assert out == {"Fresh": 1500.0}


def test_get_or_init_no_cache_auto_fetch_fifa(elo_mod, monkeypatch):
    """R29 #3：无缓存 + fifa=None → 自动调 fetch_fifa_rankings() 触发 init。"""
    fetch_called = {"n": 0}

    def fake_fetch():
        fetch_called["n"] += 1
        return {"A": 1, "B": 2}

    def fake_init(fifa):
        return {f"X{i}": 1500.0 for i in range(20)}

    sys.modules["core.rankings"] = type(sys)("rankings_fake")
    sys.modules["core.rankings"].fetch_fifa_rankings = staticmethod(fake_fetch)
    elo_mod.init_ratings_from_fifa = fake_init

    out = elo_mod.get_or_init_elo_ratings()  # fifa=None
    assert fetch_called["n"] == 1
    assert len(out) == 20


# ──────────────────────── fetch_fifa_rankings ────────────────────────


def test_fetch_fifa_local_with_more_than_10_entries(rankings_mod, tmp_path):
    """R29 #3：本地 fifa_rankings.json 含 >10 条 → 用本地不 fallback。"""
    rank_file = tmp_path / "references" / "fifa_rankings.json"
    rank_file.parent.mkdir(parents=True, exist_ok=True)
    local = {f"Country{i}": i + 1 for i in range(15)}
    rank_file.write_text(json.dumps(local), encoding="utf-8")

    out = rankings_mod.fetch_fifa_rankings()
    assert out == local


def test_fetch_fifa_local_missing_uses_default(rankings_mod):
    """R29 #3：本地文件不存在 → 返内置 _DEFAULT_RANKINGS（>10 entries）。"""
    # tmp_path 没有 references/ 目录 → fallback
    out = rankings_mod.fetch_fifa_rankings()
    assert "Argentina" in out
    assert len(out) > 10


def test_fetch_fifa_local_bad_json_uses_default(rankings_mod, tmp_path):
    """R29 #3：本地文件存在但内容是坏 JSON → 静默 fallback 到 _DEFAULT_RANKINGS，不抛穿。"""
    rank_file = tmp_path / "references" / "fifa_rankings.json"
    rank_file.parent.mkdir(parents=True, exist_ok=True)
    rank_file.write_text("{not valid json", encoding="utf-8")

    out = rankings_mod.fetch_fifa_rankings()
    assert "Argentina" in out
    assert len(out) > 10


def test_fetch_fifa_local_under_10_uses_default(rankings_mod, tmp_path):
    """R29 #3：本地文件只有 <10 entries → fallback（保护生产数据稀疏时不静默走残缺）。"""
    rank_file = tmp_path / "references" / "fifa_rankings.json"
    rank_file.parent.mkdir(parents=True, exist_ok=True)
    # 仅 3 条
    rank_file.write_text(json.dumps({"A": 1, "B": 2, "C": 3}), encoding="utf-8")

    out = rankings_mod.fetch_fifa_rankings()
    # 应 fallback
    assert "Argentina" in out  # 内置含 Argentina
    assert "A" not in out  # 本地稀疏版全弃
