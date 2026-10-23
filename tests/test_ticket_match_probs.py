"""R40-5: web/routers/ticket.py::_match_probs 6 tests direct + D-36 lock.

按 (home, away) 反查字典 helper 0 direct tests; ticket/sfc 端点 wrapper
覆盖 happy path 但 helper 自身 fuzzy 命中 + miss 兜底 + None-value 边界
未锁。helper 失败会让 14 场票面全标 no_probs 而静默无信号。

helper 含一条契约 (D-36):
- 三层兜底顺序: (1) 精确 (h,a) 命中 (2) fuzzy teams_match_safe (3) miss
  返 None (不编数)
- 空 pred_by_pair / 空 home / 空 away -> None
- 输入 whitespace -> .strip() 后命中 (隐式归一)
- _model_three 返 None 时整函数返 None (不编数)

6 tests (HIGH):
- (a) 精确 (home,away) 命中 -> _model_three dict 透传
- (b) fuzzy 命中 (mock teams_match_safe True) -> dict 透传
- (c) miss 全 -> None (空 fuzzy 兜)
- (d) pred_by_pair={} -> None (early return)
- (e) home/away 含 whitespace -> .strip() 归一命中
- (f) _model_three 返 None (e.g., reasoning_factors 缺 key) -> 整函数 None
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.routers.ticket import _match_probs


def _entry(home="Liverpool", away="ManU",
           home_ml=0.6, draw=0.25, away_ml=0.15):
    """构造预测 entry; **模型概率**用 `probs`（融合后最终值）+ home/away 用于 fuzzy 候选。

    ⚠️ P0（2026-10-23）：旧夹具用 `reasoning_factors.{home_ml_true_prob, draw_true_prob,
    away_ml_true_prob}` —— 那不是模型概率（`draw_ml_true_prob` 键**根本不存在**，
    `*_true_prob` 是 ESPN **市场先验**）⇒ 已改成唯一真源 `probs.{h,d,a}`。
    """
    return {
        "home": home, "away": away,
        "probs": {"h": home_ml, "d": draw, "a": away_ml},
    }


def _entry_market_prior_only(home="Liverpool", away="ManU"):
    """只带**市场先验**（`reasoning_factors.*_true_prob`）的 entry —— P0 必须拒绝它。"""
    return {
        "home": home, "away": away,
        "reasoning_factors": {
            "home_true_prob": 0.6,
            "draw_true_prob": 0.25,
            "away_true_prob": 0.15,
        },
    }


# ──────────────────────── D-36 三层兜底 ────────────────────────


def test_match_probs_d36_exact_match_returns_model_three_dict():
    """R40-5 D-36: 精确 (h, a) 命中 -> _model_three dict 透传。"""
    pred = {("Liverpool", "ManU"): _entry()}
    out = _match_probs("Liverpool", "ManU", pred)
    assert out == {"主胜": 0.6, "平": 0.25, "客胜": 0.15}


def test_match_probs_d36_fuzzy_fallback(monkeypatch):
    """R40-5 D-36: 精确 miss -> fuzzy teams_match_safe True 兜底 -> dict 透传。

    mock teams_match_safe 在 cand_home/cand_away 与 home/away 匹配时返 True,
    验证 _match_probs 调 fuzzy 路径成功。
    """
    pred = {
        ("Manchester United", "Liverpool FC"):
            _entry(home="Manchester United", away="Liverpool FC"),
    }
    # lazy import: from web.services.team_match import teams_match_safe
    monkeypatch.setattr(
        "web.services.team_match.teams_match_safe",
        lambda h1, a1, h2, a2: True)  # 任何比对都返 True (测试 fuzzy 兜底触发)

    # home/away 字符串不同, 但 fuzzy 命中
    out = _match_probs("Liverpool", "ManU", pred)
    assert out == {"主胜": 0.6, "平": 0.25, "客胜": 0.15}


def test_match_probs_d36_full_miss_returns_none(monkeypatch):
    """R40-5 D-36: 模糊也 miss -> None (不编数)。"""
    pred = {("Chelsea", "Arsenal"): _entry(home="Chelsea", away="Arsenal")}
    # fuzzy 永远不命中
    monkeypatch.setattr(
        "web.services.team_match.teams_match_safe",
        lambda h1, a1, h2, a2: False)

    out = _match_probs("Liverpool", "ManU", pred)
    assert out is None


def test_match_probs_d36_empty_pred_returns_none():
    """R40-5 D-36: pred_by_pair={} (early return) -> None 不抛错。

    防止空字典遍历 (e.g., _pred_by_pair 返 {} 兜底时) 触发 KeyError。
    """
    assert _match_probs("Liverpool", "ManU", {}) is None


# ──────────────────────── 输入归一 ────────────────────────


def test_match_probs_d36_strips_whitespace_home_away():
    """R40-5 D-36: home / away 含前后空白 -> .strip() 归一后命中。

    用户输入可能含不可见字符 (换行 / 全角空格), 不能让精确 hit 失效。
    """
    pred = {("Liverpool", "ManU"): _entry()}
    out = _match_probs("  Liverpool  ", "\nManU\t", pred)
    assert out == {"主胜": 0.6, "平": 0.25, "客胜": 0.15}


# ──────────────────────── _model_three 透传 ────────────────────────


def test_match_probs_d36_model_three_none_when_no_proba():
    """R40-5 D-36: entry 无三向 prob -> _model_three 返 None -> 整函数 None。

    防止 helper 编数 (e.g., 默认 {0.33, 0.33, 0.33}) 污染票面。
    """
    pred = {("Liverpool", "ManU"): {
        "home": "Liverpool", "away": "ManU",
        "reasoning_factors": {},  # 缺三向 key
    }}
    out = _match_probs("Liverpool", "ManU", pred)
    assert out is None


def test_match_probs_p0_rejects_market_prior_only():
    """P0（2026-10-23）：只有**市场先验**（`reasoning_factors.*_true_prob`）时必须返 None。

    `*_true_prob` 是 ESPN 赔率去水后的**市场先验**，不是模型输出 ⇒ 拿它算命中概率
    是循环论证（用庄家的赔率去猜庄家的结果）。旧实现读 `draw_true_prob` 当平局腿
    ⇒ 票面概率失真 ⇒ 必须拒绝。
    """
    pred = {("Liverpool", "ManU"): _entry_market_prior_only()}
    assert _match_probs("Liverpool", "ManU", pred) is None


def test_match_probs_p0_rejects_ml_proba_only():
    """P0（2026-10-23）：只有 `ml_proba`（ML 分类器原始输出）时必须返 None。

    `ml_proba` 未经与规则模型融合，强弱悬殊的场次会塌成 [0.978, 0.008, 0.014] ⇒
    拿它当「模型赢面」会让票面与推荐单/卡片互相矛盾。
    """
    pred = {("Liverpool", "ManU"): {
        "home": "Liverpool", "away": "ManU",
        "ml_proba": [0.9778, 0.0081, 0.0141],
    }}
    assert _match_probs("Liverpool", "ManU", pred) is None
