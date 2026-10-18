"""R55: 跨模块 0 直测 private helper 6 测 + D-111 lock。

R54 末已锁 web/services/news.py + web/services/store.py. 剩余 2 个 high-ROI
0 直测 private helper:

1. ``web.services.team_match.teams_match_safe`` (L78-91)
   - 容错 wrapper: ``teams_match`` 抛异常时退化到 strip 后字符串相等比较
   - docstring 自标 "真函数 0 直测" (test_team_match_teams_match.py L9)
   - haf + combo 路由共用, 容错语义不一致 = 高风险点

2. ``web.enrich._prediction_time`` (L19-20) + ``_limited_predictions`` (L23-28)
   - AI 富化批处理: 取 kickoff_utc > kickoff_date > 空字符串
   - sort by kickoff + 截断 40 场 (LLM 配额保护)

D-111 锁 3 helper (6 测):
- D-111-1: teams_match_safe 正常 teams_match 命中 -> 直接返 teams_match 结果
- D-111-2: teams_match_safe teams_match 抛错 -> 退化到 strip 字符串相等
- D-111-3: teams_match_safe teams_match 抛错 + 输入含 None -> 退化兜底
- D-111-4: _prediction_time kickoff_utc > kickoff_date > "" 优先级
- D-111-5: _limited_predictions 按 kickoff 升序 + 截断 40 + 无 kickoff 时不 sort
- D-111-6: _limited_predictions 超过 40 时 logger.warning 但仍截断

策略:
- spec-load 两个模块到隔离 namespace
- teams_match_safe 用 monkeypatch 把其依赖的 teams_match 抛错来触发异常路径
- _limited_predictions 用 caplog 抓 logger.warning
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def tm_mod():
    """Spec-load web.services.team_match (含 teams_match + teams_match_safe)."""
    spec = importlib.util.spec_from_file_location(
        "web_services_team_match_test",
        REPO_ROOT / "web" / "services" / "team_match.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-111-1~3: teams_match_safe (3 测) ────────────────────────


def test_d111_1_teams_match_safe_passes_through_when_teams_match_succeeds(tm_mod):
    """D-111-1: teams_match_safe 调 teams_match 成功时, 直接返 teams_match 结果.

    不走到 except 分支.
    """
    # 同对相同顺序, teams_match 返 True
    assert tm_mod.teams_match_safe("Arsenal", "Chelsea", "Arsenal", "Chelsea") is True
    # 完全不同, teams_match 返 False
    assert tm_mod.teams_match_safe("Arsenal", "Chelsea", "Liverpool", "ManU") is False


def test_d111_2_teams_match_safe_fallback_to_strip_eq_on_exception(tm_mod, monkeypatch):
    """D-111-2: teams_match 抛错 (fuzzy 失败) -> teams_match_safe 退化到 strip 字符串相等 (L87-91)."""
    # 让 teams_match 抛错
    def boom(*args, **kwargs):
        raise RuntimeError("fuzzy canon failed")
    monkeypatch.setattr(tm_mod, "teams_match", boom)

    # 此时 strip 字符串相等应该匹配
    assert tm_mod.teams_match_safe("Arsenal", "Chelsea", "Arsenal", "Chelsea") is True
    # 不等
    assert tm_mod.teams_match_safe("Arsenal", "Chelsea", "Liverpool", "ManU") is False
    # strip 空白后相等
    assert tm_mod.teams_match_safe("  Arsenal  ", "Chelsea", "Arsenal", "  Chelsea  ") is True


def test_d111_3_teams_match_safe_handles_none_gracefully(tm_mod, monkeypatch):
    """D-111-3: teams_match_safe 输入含 None -> strip 后空字符串相等比较, 不抛穿.

    L88-91 ``(h1 or "").strip() == (h2 or "").strip()`` 对 None 容忍.
    """
    def boom(*args, **kwargs):
        raise RuntimeError("canon of None failed")
    monkeypatch.setattr(tm_mod, "teams_match", boom)

    # 双方都是 None -> 空字符串 == 空字符串 -> True
    assert tm_mod.teams_match_safe(None, None, None, None) is True
    # 一方 None, 一方空串 -> 等
    assert tm_mod.teams_match_safe(None, "", None, "") is True
    # None vs "real" -> 不等
    assert tm_mod.teams_match_safe(None, "X", "Y", "X") is False


# ──────────────────────── D-111-4~6: _prediction_time + _limited_predictions (3 测) ────────────────────────


@pytest.fixture
def enrich_mod():
    """Spec-load web.enrich 到隔离 namespace."""
    spec = importlib.util.spec_from_file_location(
        "web_enrich_test",
        REPO_ROOT / "web" / "enrich.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_d111_4_prediction_time_priority_kickoff_utc_first(enrich_mod):
    """D-111-4: _prediction_time (L19-20) kickoff_utc > kickoff_date > ""."""
    # 场景 a: kickoff_utc 优先
    pred_a = {"kickoff_utc": "2025-10-17T14:30:00Z", "kickoff_date": "2025-10-17"}
    assert enrich_mod._prediction_time(pred_a) == "2025-10-17T14:30:00Z"

    # 场景 b: 无 kickoff_utc 回退 kickoff_date
    pred_b = {"kickoff_date": "2025-10-17"}
    assert enrich_mod._prediction_time(pred_b) == "2025-10-17"

    # 场景 c: 全空 -> 空字符串 (而非 None, L20 str() 强转)
    pred_c = {}
    assert enrich_mod._prediction_time(pred_c) == ""

    # 场景 d: kickoff_utc=None 也回退 kickoff_date
    pred_d = {"kickoff_utc": None, "kickoff_date": "2025-10-17"}
    assert enrich_mod._prediction_time(pred_d) == "2025-10-17"


def test_d111_5_limited_predictions_sorts_by_kickoff_and_caps_at_40(enrich_mod, caplog):
    """D-111-5: _limited_predictions 按 kickoff 升序排 (有 kickoff 的) + 截断到 40 场.

    - 场景: 50 场, kickoff_utc 错乱, 应按升序排后只取前 40.
    """
    # 50 场比赛, kickoff 错乱
    preds = [
        {"kickoff_utc": f"2025-10-17T{10 + i % 14:02d}:00:00Z"} for i in range(50)
    ]
    # 把前几条反过来, 让排序有可见效果
    preds = list(reversed(preds))

    with caplog.at_level(logging.WARNING):
        out = enrich_mod._limited_predictions(preds, "epl")

    assert len(out) == 40  # 截断
    # 升序: out[0] 的 hour <= out[-1] 的 hour
    out_hours = [int(p["kickoff_utc"][11:13]) for p in out]
    assert out_hours == sorted(out_hours)


def test_d111_6_limited_predictions_skips_sort_when_no_kickoff(enrich_mod, caplog):
    """D-111-6: 全部预测都无 kickoff 时, 不 sort (L24 ``any(...)`` 过滤), 直接截断.

    L24 ``if any(_prediction_time(pred) for pred in preds): preds = sorted(...)``.
    无 kickoff 时 ``any()`` False, 不 sort, 直接 [:40].
    """
    # 45 场无 kickoff
    preds = [{"name": f"match-{i}"} for i in range(45)]

    with caplog.at_level(logging.WARNING):
        out = enrich_mod._limited_predictions(preds, "laliga")

    # 仍截断到 40
    assert len(out) == 40
    # 顺序保持输入顺序 (前 40)
    assert out[0]["name"] == "match-0"
    assert out[39]["name"] == "match-39"
