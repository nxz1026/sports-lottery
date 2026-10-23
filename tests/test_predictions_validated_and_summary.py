"""R50-5: web/routers/predictions.py::_validated_model_probs + _prediction_summary 4 cases direct + D-96 lock.

_validated_model_probs (L43-L58, 16 行) 返归一化 1X2 概率向量:
1) value = doc['model_probs'] 或 doc['ml_proba']
2) list/tuple 3 元素 -> dict zip (home, draw, away)
3) 非 dict 或 keys != {home, draw, away} -> None
4) float 转换失败 -> None
5) 不在 [0, 1] 或 !finite -> None
6) sum != 1.0 (容差 1e-6) -> None

_prediction_summary (L100-L123, 24 行) 单场预测精简视图:
1) summary 12 默认字段 (含 market = _market_projection, D-94 已锁)
2) 11 字段白名单 (L116-118) 透传
3) spread_pred/total_pred/win_prob 任一 -> _bball_extras (D-56 已锁)

R44-1 D-56 + R44-2 D-52 + R50-3 D-94 已锁 _bball_extras / _print_summary
/ _market_projection, 但 _validated_model_probs / _prediction_summary 自身
核心契约 (5 校验路径 + 12 默认字段 + 白名单透传 + basketball 触发) 0 直测。

D-96 锁定 4 契约:
- D-96-1: _validated_model_probs 主流程 - dict 完整 + 归一化 sum=1 -> 返 probs
- D-96-2: _validated_model_probs 校验失败 (缺字段/非数/和不等 1) -> None
- D-96-3: _prediction_summary 主流程 - 12 字段 + 白名单透传
- D-96-4: _prediction_summary spread_pred 触发 _bball_extras (basketball 字段)

依赖 stub:
- _market_projection (D-94 已锁, 真跑)
- _bball_extras (D-56 已锁, 真跑)
- _validated_model_probs 真跑
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def preds_mod():
    """Spec-load predictions.py (D-56/D-94 内部 helper 真跑)。"""
    spec = importlib.util.spec_from_file_location(
        "predictions_validated_and_summary_test",
        REPO_ROOT / "web" / "routers" / "predictions.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-96 四契约 ────────────────────────


def test_validated_model_probs_d96_1_happy_normalized_probs(preds_mod):
    """R50-5 D-96-1: _validated_model_probs 主流程 - 完整 dict + 归一化 sum=1。

    model_probs = {home: 0.5, draw: 0.3, away: 0.2}, sum=1.0 -> 返原 dict。
    """
    doc = {"model_probs": {"home": 0.5, "draw": 0.3, "away": 0.2}}
    out = preds_mod._validated_model_probs(doc)
    assert out == {"home": 0.5, "draw": 0.3, "away": 0.2}

    # list 3 元素 -> dict zip (L46-47)
    doc_list = {"model_probs": [0.4, 0.3, 0.3]}
    out_list = preds_mod._validated_model_probs(doc_list)
    assert out_list == {"home": 0.4, "draw": 0.3, "away": 0.3}

    # P0（2026-10-23）：ml_proba 是 **ML 分类器原始输出**（未与规则模型融合），
    # 不是「模型概率」⇒ **不再被接受**。实测它会塌成 [0.978, 0.008, 0.014]，
    # 被当成模型概率后算出的 edge/EV 全是假账（主胜 EV 被算成 +0.33，
    # 而决策层用同一个模型算出 −0.159 ⇒ 卡片与推荐单给相反建议）。
    doc_ml = {"ml_proba": {"home": 0.6, "draw": 0.2, "away": 0.2}}
    assert preds_mod._validated_model_probs(doc_ml) is None
    # 唯一真源 = probs（predictor 融合后的最终概率，与决策层同口径）
    doc_probs = {"probs": {"h": 0.6, "d": 0.2, "a": 0.2}}
    assert preds_mod._validated_model_probs(doc_probs) == {"home": 0.6, "draw": 0.2, "away": 0.2}


def test_validated_model_probs_d96_2_validation_failures_return_none(preds_mod):
    """R50-5 D-96-2: 校验失败 (缺字段/非数/和不等 1) -> None。

    5 失败路径:
    - 缺字段 (e.g. 只有 home+draw) -> None (L48)
    - keys 多了 (e.g. 含 x) -> None
    - 字符串非数字 (e.g. 'abc') -> None (L52-53)
    - v < 0 或 v > 1 -> None (L54)
    - sum != 1.0 (容差 1e-6) -> None (L56-57)
    """
    # 缺字段
    doc_missing = {"model_probs": {"home": 0.5, "draw": 0.5}}  # 无 away
    assert preds_mod._validated_model_probs(doc_missing) is None
    # keys 多了
    doc_extra = {"model_probs": {"home": 0.4, "draw": 0.3, "away": 0.3, "x": 0}}
    assert preds_mod._validated_model_probs(doc_extra) is None
    # 字符串非数字
    doc_str = {"model_probs": {"home": "abc", "draw": 0.3, "away": 0.2}}
    assert preds_mod._validated_model_probs(doc_str) is None
    # v < 0
    doc_neg = {"model_probs": {"home": -0.1, "draw": 0.6, "away": 0.5}}
    assert preds_mod._validated_model_probs(doc_neg) is None
    # v > 1
    doc_big = {"model_probs": {"home": 1.5, "draw": 0.0, "away": -0.5}}
    assert preds_mod._validated_model_probs(doc_big) is None
    # sum != 1.0
    doc_sum = {"model_probs": {"home": 0.5, "draw": 0.3, "away": 0.3}}  # sum=1.1
    assert preds_mod._validated_model_probs(doc_sum) is None
    # 缺 model_probs + ml_proba -> None
    assert preds_mod._validated_model_probs({}) is None


def test_prediction_summary_d96_3_happy_path_12_fields_plus_whitelist(preds_mod):
    """R50-5 D-96-3: _prediction_summary 主流程 - 12 字段 + 白名单透传。

    doc 含默认 12 字段 + 白名单 ml_model_used 等 -> summary 含所有。
    """
    doc = {
        "match": "A vs B", "home": "A", "away": "B",
        "direction": "主胜", "stars": "2-star", "confidence_score": 0.55,
        "predicted_score": "1-0", "over_under": "Over 2.5", "btts": "Yes",
        "kickoff_utc": "2026-07-15T15:00:00+00:00", "data_window": "20260715-20260720",
        # 白名单 11 字段
        "odds_data_available": True, "confidence_note": "high",
        "reasoning_factors": {"x": 1}, "ml_model_used": "dixon_coles",
        "ml_proba": [0.5, 0.3, 0.2], "poisson_top3": [{"score": "1-0"}],
        "lambda_home": 1.5, "lambda_away": 1.0,
        "lambda_home_ci95": [1.2, 1.8], "lambda_away_ci95": [0.8, 1.2],
    }
    summary = preds_mod._prediction_summary(doc, "2026-07-15")
    # 12 默认字段
    assert summary["match"] == "A vs B"
    assert summary["home"] == "A"
    assert summary["away"] == "B"
    assert summary["direction"] == "主胜"
    assert summary["stars"] == "2-star"
    assert summary["confidence_score"] == 0.55
    assert summary["predicted_score"] == "1-0"
    assert summary["over_under"] == "Over 2.5"
    assert summary["btts"] == "Yes"
    assert summary["kickoff_utc"] == "2026-07-15T15:00:00+00:00"
    assert summary["data_window"] == "20260715-20260720"
    # market 字段 (D-94 _market_projection 真跑, 无 market -> missing)
    assert summary["market"] == {"status": "missing"}
    # 白名单 11 字段透传
    assert summary["odds_data_available"] is True
    assert summary["confidence_note"] == "high"
    assert summary["ml_model_used"] == "dixon_coles"
    assert summary["lambda_home"] == 1.5
    assert summary["lambda_away"] == 1.0
    # 不在白名单的字段不传 (e.g. doc 自身)
    assert "extra_field" not in summary


def test_prediction_summary_d96_4_bball_extras_triggered_by_spread(preds_mod):
    """R50-5 D-96-4: spread_pred/total_pred/win_prob 任一 -> _bball_extras 触发 (D-56)。

    doc 含 spread_pred -> summary 含 spread_pred/total_pred/win_prob 字段。
    """
    doc = {
        "home": "Lakers", "away": "Celtics",
        "spread_pred": "Lakers -5.5", "total_pred": "Over 220.5",
        "win_prob": 0.65,
    }
    summary = preds_mod._prediction_summary(doc, "2026-07-15")
    # D-56 _bball_extras 透传
    assert summary["spread_pred"] == "Lakers -5.5"
    assert summary["total_pred"] == "Over 220.5"
    assert summary["win_prob"] == 0.65
    # 仅 win_prob 触发也走 _bball_extras
    doc_win = {"home": "A", "away": "B", "win_prob": 0.55}
    summary_win = preds_mod._prediction_summary(doc_win, "2026-07-15")
    assert summary_win["win_prob"] == 0.55
    # 无 spread/total/win_prob 不触发 (D-56 R44-1 验证)
    doc_none = {"home": "A", "away": "B"}
    summary_none = preds_mod._prediction_summary(doc_none, "2026-07-15")
    assert "spread_pred" not in summary_none
    assert "win_prob" not in summary_none
