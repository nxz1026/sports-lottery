"""R50-3: web/routers/predictions.py::_market_projection 5 cases direct + D-94 lock.

_market_projection (L61-L97, 37 行) Project only source-backed structured odds;
never infer from confidence:
1) doc['market'] 非 dict -> {"status": "missing"}
2) source/captured_at/market_type/selections 任一缺 -> {"status": "partial"}
3) market_type != '1x2' 或 selections != {home, draw, away} -> partial
4) decimal_odds 解析失败 / odds <= 1 -> partial
5) 完整路径: devig_probs = (1/v)/total + margin = total-1
6) model_probs 来自 _validated_model_probs (D-96 待锁) -> edge + ev

R44-1 D-56 已锁 _bball_extras, R44-2 D-52 _print_summary, 但 _market_projection
自身核心契约 (5 status 路径 + devig + edge + ev + vig 守门) 0 直测。

D-94 锁定 5 契约:
- D-94-1: 完整 happy (1x2 + 3 selections + odds>1) -> status=complete + devig +
  margin + edge + ev
- D-94-2: market 缺 -> {"status": "missing"}
- D-94-3: market 缺字段 (source/captured_at/market_type/selections 任一) -> partial
- D-94-4: market_type != "1x2" -> partial (但 source 等仍返)
- D-94-5: odds <= 1 (vig 守门) -> partial

依赖 stub:
- _validated_model_probs (monkeypatch 返受控 probs)
- 其它 _market_projection 内部用 (_league_names, _bball_extras 等) 真跑
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
def preds_mod(monkeypatch):
    """Spec-load predictions.py + stub _validated_model_probs。"""
    spec = importlib.util.spec_from_file_location(
        "predictions_market_projection_test",
        REPO_ROOT / "web" / "routers" / "predictions.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # _validated_model_probs stub 返受控
    monkeypatch.setattr(mod, "_validated_model_probs",
                        lambda doc: {"home": 0.50, "draw": 0.30, "away": 0.20})
    return mod


def _doc_with_market(market: dict) -> dict:
    return {"market": market, "predictions": []}


# ──────────────────────── D-94 五契约 ────────────────────────


def test_market_projection_d94_1_happy_path_complete_with_edge_ev(preds_mod):
    """R50-3 D-94-1: 完整 happy - status=complete + devig + edge + ev + margin。

    1x2 + home/draw/away 3 selections + 合理 odds (2.0/3.4/4.0) ->
    total=1/2+1/3.4+1/4=0.5+0.294+0.25=1.044
    devig: home=0.479, draw=0.282, away=0.239
    margin=0.044
    model: home=0.50, draw=0.30, away=0.20
    edge: home=0.021, draw=0.018, away=-0.039
    ev: home=0.50*2-1=0, draw=0.30*3.4-1=0.02, away=0.20*4-1=-0.20
    """
    doc = _doc_with_market({
        "source": "official",
        "captured_at": "2026-07-15T15:00:00+08:00",
        "market_type": "1x2",
        "odds_format": "decimal",
        "selections": {"home": 2.0, "draw": 3.4, "away": 4.0},
    })
    out = preds_mod._market_projection(doc)
    assert out["status"] == "complete"
    assert out["source"] == "official"
    assert out["market_type"] == "1x2"
    # devig 归一化
    assert abs(sum(out["devig_probs"].values()) - 1.0) < 0.001
    # implied_probs (1/v 原始)
    assert abs(out["implied_probs"]["home"] - 0.5) < 0.001
    # margin
    assert abs(out["margin"] - 0.044) < 0.005
    # model_probs + edge + ev
    assert out["model_probs"] == {"home": 0.50, "draw": 0.30, "away": 0.20}
    assert out["edge_prob"]["home"] == pytest.approx(0.021, abs=0.005)
    assert out["edge_prob"]["draw"] == pytest.approx(0.018, abs=0.005)
    assert out["edge_prob"]["away"] == pytest.approx(-0.039, abs=0.005)
    # ev_per_unit
    assert out["ev_per_unit"]["home"] == pytest.approx(0.0, abs=0.001)
    assert out["ev_per_unit"]["draw"] == pytest.approx(0.02, abs=0.005)
    assert out["ev_per_unit"]["away"] == pytest.approx(-0.20, abs=0.005)


def test_market_projection_d94_2_missing_market_returns_status_missing(preds_mod):
    """R50-3 D-94-2: doc['market'] 缺 -> {"status": "missing"} (L64-65)。

    LOCK: 完全无 market 键 -> 'missing' (区别于 'partial' 即有但字段不全)。
    """
    out = preds_mod._market_projection({})
    assert out == {"status": "missing"}

    out_none = preds_mod._market_projection({"market": None})
    assert out_none == {"status": "missing"}

    # market 非 dict (字符串/列表) 也算 missing
    out_str = preds_mod._market_projection({"market": "not a dict"})
    assert out_str == {"status": "missing"}


def test_market_projection_d94_3_missing_required_fields_returns_partial(preds_mod):
    """R50-3 D-94-3: market 缺字段 (source/captured_at/market_type/selections 任一) -> partial。

    L70 `if not source or not captured_at or not market_type or not isinstance(selections, dict): partial`。
    4 个字段任一缺 -> partial。
    """
    base = {"source": "x", "captured_at": "y", "market_type": "1x2",
             "selections": {"home": 2.0}}
    # 缺 source
    out1 = preds_mod._market_projection(_doc_with_market({**base, "source": ""}))
    assert out1["status"] == "partial"
    # 缺 captured_at
    out2 = preds_mod._market_projection(_doc_with_market({**base, "captured_at": None}))
    assert out2["status"] == "partial"
    # 缺 market_type
    out3 = preds_mod._market_projection(_doc_with_market({**base, "market_type": ""}))
    assert out3["status"] == "partial"
    # selections 非 dict
    out4 = preds_mod._market_projection(_doc_with_market({**base, "selections": []}))
    assert out4["status"] == "partial"


def test_market_projection_d94_4_market_type_not_1x2_returns_partial(preds_mod):
    """R50-3 D-94-4: market_type != "1x2" -> partial (但 source 等仍返, L75-76)。

    LOCK: market_type="asian_handicap" / "over_under" 等也返 partial,
    但保留 source/captured_at/market_type/selections 字段。
    """
    doc = _doc_with_market({
        "source": "official",
        "captured_at": "2026-07-15T15:00:00+08:00",
        "market_type": "asian_handicap",
        "selections": {"home": 1.9, "away": 2.0},
    })
    out = preds_mod._market_projection(doc)
    assert out["status"] == "partial"
    # source 等仍返 (L72-74 partial 默认返这些)
    assert out["source"] == "official"
    assert out["market_type"] == "asian_handicap"
    # 缺 decimal_odds/edge/ev/margin (L75 后 return out 未升级)
    assert "devig_probs" not in out


def test_market_projection_d94_5_odds_le_1_vig_check_returns_partial(preds_mod):
    """R50-3 D-94-5: odds <= 1 (vig 守门) -> partial (L85-86)。

    L85 `if any(not math.isfinite(v) or v <= 1 for v in decimals.values())`:
    odds <= 1 表示无 vig (无博彩利润), 视为异常数据 -> partial。
    """
    # odds=1.0 (无 vig) -> partial
    doc1 = _doc_with_market({
        "source": "x", "captured_at": "y", "market_type": "1x2",
        "selections": {"home": 1.0, "draw": 3.4, "away": 4.0},
    })
    out1 = preds_mod._market_projection(doc1)
    assert out1["status"] == "partial"
    # odds=0.5 (无效) -> partial
    doc2 = _doc_with_market({
        "source": "x", "captured_at": "y", "market_type": "1x2",
        "selections": {"home": 0.5, "draw": 3.4, "away": 4.0},
    })
    out2 = preds_mod._market_projection(doc2)
    assert out2["status"] == "partial"
    # 字符串非数字 (解析错) -> partial
    doc3 = _doc_with_market({
        "source": "x", "captured_at": "y", "market_type": "1x2",
        "selections": {"home": "abc", "draw": 3.4, "away": 4.0},
    })
    out3 = preds_mod._market_projection(doc3)
    assert out3["status"] == "partial"