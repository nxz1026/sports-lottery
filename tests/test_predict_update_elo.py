"""R31 #1: scripts/predict.py::_update_elo 4 测直测。

现状：函数本体 0 直测；R29-2 `test_setup_league_run_branches.py` (8 处) 与
`test_predict_log_error_wrappers.py` (2 处) 仅 monkeypatch fake stub 替，
所有 wrapper 分支从未执行。

本轮补 4 测，覆盖：
- 空 past 仍触发 init + save（不依赖 past 长度）
- 全部畸形 score → process_match_result 0 次调用
- 多 dash "1-2-3" → split 取 [0]/[1]，第三段吞掉（潜在漂移点锁定）
- force_refresh 透传到 get_or_init_elo_ratings
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def predict_mod():
    """加载 predict.py 为唯一 module name。"""
    spec = importlib.util.spec_from_file_location(
        "update_elo_test", REPO_ROOT / "scripts" / "predict.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_update_elo_empty_past_calls_init_and_save(predict_mod, monkeypatch):
    """R31 #1：past=[] → 仍触发 get_or_init + save；process_match_result 0 次调用。"""
    init_calls = {"n": 0, "force_refresh": None}
    save_calls = {"n": 0}
    pmr_calls = {"n": 0}

    def fake_init(fifa, force_refresh=False):
        init_calls["n"] += 1
        init_calls["force_refresh"] = force_refresh
        return {"Brazil": 1800.0, "England": 1900.0}

    def fake_save(ratings):
        save_calls["n"] += 1

    def fake_pmr(*args, **kwargs):
        pmr_calls["n"] += 1

    monkeypatch.setattr(predict_mod, "get_or_init_elo_ratings", fake_init)
    monkeypatch.setattr(predict_mod, "process_match_result", fake_pmr)
    monkeypatch.setattr(predict_mod, "save_elo_ratings", fake_save)

    out = predict_mod._update_elo([], fifa_rankings={"Brazil": 1}, force_refresh=False)
    assert init_calls["n"] == 1
    assert init_calls["force_refresh"] is False
    assert save_calls["n"] == 1
    assert pmr_calls["n"] == 0  # 空 past 不调
    assert out == {"Brazil": 1800.0, "England": 1900.0}


def test_update_elo_all_malformed_scores_skips_process(predict_mod, monkeypatch):
    """R31 #1：畸形 score "x-y"/"1"/""/"abc" → ValueError/IndexError 全捕 → 0 次 process。"""
    monkeypatch.setattr(predict_mod, "get_or_init_elo_ratings",
                        lambda *a, **k: {"A": 1500.0, "B": 1500.0})
    monkeypatch.setattr(predict_mod, "save_elo_ratings", lambda r: None)

    pmr_calls = {"n": 0}

    def fake_pmr(*args, **kwargs):
        pmr_calls["n"] += 1

    monkeypatch.setattr(predict_mod, "process_match_result", fake_pmr)

    malformed = [
        {"score": "x-y", "home_en": "A", "away_en": "B"},
        {"score": "1", "home_en": "C", "away_en": "D"},  # 缺 "-"
        {"score": "", "home_en": "E", "away_en": "F"},  # 空
        {"score": "abc", "home_en": "G", "away_en": "H"},  # 非数字
    ]
    out = predict_mod._update_elo(malformed, fifa_rankings={})
    assert pmr_calls["n"] == 0  # 4 条全部 try/except 吞了


def test_update_elo_multi_dash_score_uses_first_two_parts(predict_mod, monkeypatch):
    """R31 #1："1-2-3" 多 dash → split 取 [0]/[1]，第三段吞掉。记录潜在漂移点。

    当前实现 `m.get("score", "0-0").split("-")[0/1]`：对 `"1-2-3"` 解析为
    `home_goals=1, away_goals=2`，实际比分可能是 `12-3` 但被误读为 `1-2`。
    本测试锁定当前行为，避免未来 silent 修复时回归。
    """
    monkeypatch.setattr(predict_mod, "get_or_init_elo_ratings",
                        lambda *a, **k: {"A": 1500.0, "B": 1500.0})
    monkeypatch.setattr(predict_mod, "save_elo_ratings", lambda r: None)

    pmr_calls = {"args": []}

    def fake_pmr(home_en, away_en, hg, ag, ratings):
        pmr_calls["args"].append((home_en, away_en, hg, ag))

    monkeypatch.setattr(predict_mod, "process_match_result", fake_pmr)

    predict_mod._update_elo(
        [{"score": "1-2-3", "home_en": "A", "away_en": "B"}],
        fifa_rankings={})

    # 当前行为：third segment "3" 静默丢弃
    assert pmr_calls["args"] == [("A", "B", 1, 2)]


def test_update_elo_force_refresh_true_passed_to_init(predict_mod, monkeypatch):
    """R31 #1：force_refresh=True → 透传到 get_or_init_elo_ratings；False 同样透传。"""
    captured = {"calls": []}

    def fake_init(fifa, force_refresh=False):
        captured["calls"].append(force_refresh)
        return {"A": 1500.0}

    monkeypatch.setattr(predict_mod, "get_or_init_elo_ratings", fake_init)
    monkeypatch.setattr(predict_mod, "save_elo_ratings", lambda r: None)
    monkeypatch.setattr(predict_mod, "process_match_result", lambda *a, **k: None)

    predict_mod._update_elo([], fifa_rankings={"A": 1}, force_refresh=True)
    predict_mod._update_elo([], fifa_rankings={"A": 1}, force_refresh=False)

    assert captured["calls"] == [True, False]
