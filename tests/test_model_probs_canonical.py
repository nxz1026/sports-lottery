"""**模型概率唯一真源**守门测试 — P0 / 2026-10-23 实测事故。

事故（生产实测，「斯图加特 vs 门兴格拉德巴赫」，德甲 2026-10-23）
----------------------------------------------------------------
同一场比赛，页面上出现 4 个互相矛盾的「赢面」：

* ``probs``            = ``{"h": 0.6466, "d": 0.1405, "a": 0.2129}``  ← 决策层（推荐单）用的
* ``ml_proba``         = ``[0.9778, 0.0081, 0.0141]``                ← 卡片概率条 / edge / EV 用的
* ``confidence_score`` = ``0.793``（= (0.6466 − 0.25) × 2，把握度评分）  ← 卡片标成「赢面」
* ``reasoning_factors`` = ``home_ml_true_prob=0.704`` / ``draw_true_prob=0.137``
  / ``away_ml_true_prob=0.159``                                     ← 串关 EV 用的

⇒ 卡片说主胜「每投 1 元长期多赚 0.33 元」，决策层算出主胜 EV **−0.159**（要亏）；
   卡片让买主胜、推荐单让买客胜 —— 同一页两个面板互相打脸。

根因：``ml_proba`` 存的是 **ML 分类器原始输出**（未与规则模型融合），强弱悬殊的场次
会塌成 ``[0.978, 0.008, 0.014]``，而下游全把它当「模型概率」。

本文件锁住的纪律
----------------
1. ``core.model_probs`` 是**唯一真源**：``model_probs`` → ``probs.{h,d,a}``
   → ``reasoning_factors.*_prob_weighted``；
2. **绝不**用 ``ml_proba``（分类器原始输出）；
3. **绝不**用 ``*_true_prob``（ESPN 市场先验，不是模型输出）；
4. 归一化容差 **1e-3**（4 位小数四舍五入的舍入误差上界 ≈1.5e-4；旧容差 1e-6 把
   99/162 的真实场次误判为非法 ⇒ edge/EV 静默缺失）；
5. 三侧齐全才出向量，缺一 ⇒ None（「不知道」≠「概率为 0」）；
6. web 层三个消费点（API edge/EV、串关 EV、票面三向）必须全部走同一真源。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.model_probs import (  # noqa: E402
    DEFAULT_SUM_TOL,
    as_prob,
    resolve_model_probs,
    resolve_prob,
)

# 生产实测的「斯图加特 vs 门兴格拉德巴赫」原始预测行关键字段
STUTTGART_DOC = {
    "home": "斯图加特",
    "away": "门兴格拉德巴赫",
    "direction": "斯图加特 胜",
    "confidence_score": 0.793,
    # 融合后最终概率（真源）
    "probs": {"h": 0.6466, "d": 0.1405, "a": 0.2129},
    # ML 分类器原始输出（塌方）—— 必须被无视
    "ml_proba": [0.9778, 0.0081, 0.0141],
    "reasoning_factors": {
        # 市场先验（ESPN 去水概率）—— 必须被无视
        "home_true_prob": 0.7353,
        "draw_true_prob": 0.1905,
        "away_true_prob": 0.0742,
        # 旧的混合字段（平局腿根本不是模型输出）—— 必须被无视
        "home_ml_true_prob": 0.704,
        "away_ml_true_prob": 0.159,
        # 模型自己的加权概率（合法回退源）
        "home_prob_weighted": 0.647,
        "draw_prob_weighted": 0.140,
        "away_prob_weighted": 0.213,
    },
}


# ───────────────────────── core.model_probs ─────────────────────────


def test_resolve_prob_prefers_probs_over_everything():
    """真源优先：probs 压过 ml_proba / *_true_prob / *_ml_true_prob。"""
    assert resolve_prob(STUTTGART_DOC, "h") == pytest.approx(0.6466)
    assert resolve_prob(STUTTGART_DOC, "d") == pytest.approx(0.1405)
    assert resolve_prob(STUTTGART_DOC, "a") == pytest.approx(0.2129)


def test_resolve_prob_never_uses_ml_proba_nor_market_prior():
    """ml_proba（分类器原始输出）与 *_true_prob（市场先验）都不是模型概率。"""
    # 只给 ml_proba ⇒ 取不到（不是 0，也不是那个塌方值）
    assert resolve_prob({"ml_proba": [0.9778, 0.0081, 0.0141]}, "h") is None
    # 只给市场先验 ⇒ 取不到
    assert resolve_prob({"reasoning_factors": {
        "home_true_prob": 0.7353, "draw_true_prob": 0.1905}}, "h") is None
    # 只给 *_ml_true_prob ⇒ 取不到（旧的混合字段不是真源）
    assert resolve_prob({"reasoning_factors": {"home_ml_true_prob": 0.704}}, "h") is None


def test_resolve_prob_falls_back_to_prob_weighted():
    """旧预测文件没有 probs ⇒ 回退 reasoning_factors.*_prob_weighted（模型输出）。"""
    old = {"reasoning_factors": {
        "home_prob_weighted": 0.647, "draw_prob_weighted": 0.140,
        "away_prob_weighted": 0.213}}
    assert resolve_prob(old, "h") == pytest.approx(0.647)
    assert resolve_prob(old, "a") == pytest.approx(0.213)


def test_resolve_model_probs_returns_sane_vector_for_stuttgart():
    """回归：斯图加特那行必须返回 0.6466/0.1405/0.2129，不是塌方值。"""
    out = resolve_model_probs(STUTTGART_DOC)
    assert out is not None
    assert out["home"] == pytest.approx(0.6466)
    assert out["draw"] == pytest.approx(0.1405)
    assert out["away"] == pytest.approx(0.2129)
    # 绝不能是塌方的 0.9778
    assert out["home"] != pytest.approx(0.9778)


def test_resolve_model_probs_none_when_unknown():
    """「不知道」⇒ None（不是 0.0）。"""
    assert resolve_model_probs({}) is None
    assert resolve_model_probs({"ml_proba": [0.5, 0.3, 0.2]}) is None
    assert resolve_model_probs(None) is None


def test_resolve_model_probs_tolerance_is_rounding_aware():
    """容差 1e-3：4 位小数四舍五入的误差必须被接受，明显不归一必须被拒。"""
    assert DEFAULT_SUM_TOL >= 1e-3
    rounded = {"probs": {"h": 0.6466, "d": 0.1405, "a": 0.2129}}  # 和 = 1.0
    assert resolve_model_probs(rounded) is not None
    # 和 = 1.0001（4 位小数舍入的正常误差）⇒ 接受
    assert resolve_model_probs({"model_probs": {
        "home": 0.5, "draw": 0.3, "away": 0.2001}}) is not None
    # 和 = 1.1 ⇒ 拒绝
    assert resolve_model_probs({"model_probs": {
        "home": 0.5, "draw": 0.3, "away": 0.3}}) is None


def test_as_prob_rejects_bool_and_nan():
    assert as_prob(True) is None
    assert as_prob(float("nan")) is None
    assert as_prob(float("inf")) is None
    assert as_prob("0.5") is None
    assert as_prob(-0.1) is None
    assert as_prob(1.5) is None
    assert as_prob(0.5) == 0.5


# ───────────────────────── web 层三个消费点 ─────────────────────────


def test_web_api_edge_and_ev_are_not_fake():
    """回归：主胜 EV 必须是**负的**（旧实现算出 +0.33 的假账）。

    ESPN 赔率 home=1.36 / draw=5.25 / away=6.00 ⇒ devig home ≈ 0.6731；
    模型真源 home=0.6466 ⇒ edge ≈ −0.026、EV ≈ −0.121（**亏**）。
    旧实现用塌方的 0.9778 ⇒ EV 被算成 +0.330 ⇒ 卡片骗彩民「每投 1 元多赚 0.33 元」。
    """
    from web.routers.predictions import _market_projection

    doc = dict(STUTTGART_DOC)
    doc["market"] = {
        "source": "espn",
        "captured_at": "2026-10-23T09:30:02.112895+08:00",
        "market_type": "1x2",
        "odds_format": "decimal",
        "selections": {"home": 1.36, "draw": 5.25, "away": 6.0},
    }
    out = _market_projection(doc)
    assert out["status"] == "complete"
    assert out["model_probs"]["home"] == pytest.approx(0.6466)
    assert out["edge_prob"]["home"] < 0
    assert out["ev_per_unit"]["home"] < 0
    # 客胜才是正 EV（与推荐单同一方向、同一模型）
    assert out["ev_per_unit"]["away"] == pytest.approx(0.2129 * 6.0 - 1.0, abs=1e-9)


def test_web_api_rejects_ml_proba_only_doc():
    """只有 ml_proba 的行 ⇒ 不出模型概率 ⇒ 不出 edge/EV（而不是拿塌方值编数）。"""
    from web.routers.predictions import _market_projection

    doc = {
        "ml_proba": [0.9778, 0.0081, 0.0141],
        "market": {
            "source": "espn",
            "captured_at": "2026-10-23T09:30:02.112895+08:00",
            "market_type": "1x2",
            "odds_format": "decimal",
            "selections": {"home": 1.36, "draw": 5.25, "away": 6.0},
        },
    }
    out = _market_projection(doc)
    assert "model_probs" not in out
    assert "edge_prob" not in out
    assert "ev_per_unit" not in out


def test_combo_uses_canonical_probs():
    """串关 EV 的模型概率必须来自唯一真源（平局腿也不能是市场先验）。"""
    from web.routers.combo import _resolve_model_probs

    out = _resolve_model_probs(STUTTGART_DOC)
    assert out is not None
    assert out["draw"] == pytest.approx(0.1405)   # 不是 draw_true_prob=0.1905
    assert out["home"] == pytest.approx(0.6466)   # 不是 ml_proba=0.9778
    assert out["away"] == pytest.approx(0.2129)   # 不是 away_ml_true_prob=0.159


def test_ticket_uses_canonical_probs():
    """票面三向概率必须来自唯一真源。"""
    from web.routers.ticket import _model_three

    out = _model_three(STUTTGART_DOC)
    assert out is not None
    assert out["主胜"] == pytest.approx(0.6466)
    assert out["平"] == pytest.approx(0.1405)
    assert out["客胜"] == pytest.approx(0.2129)


def test_archive_prob_delegates_to_canonical_source():
    """归档层（决策层 / CLV 管线）也必须走同一真源。"""
    from model.archive import _prob

    assert _prob(STUTTGART_DOC, "h") == pytest.approx(0.6466)
    assert _prob(STUTTGART_DOC, "d") == pytest.approx(0.1405)
    assert _prob({"ml_proba": [0.9, 0.05, 0.05]}, "h") is None
