"""R47-8: web/services/team_match.py::nba_rows 4 cases direct + D-81 lock.

nba_rows (L253-L276, 24 行) 从 NBA 预测产出行 (每日一图篮球表)。
每行: 对阵、胜负倾向、让分推荐、大小分推荐、胜分差、波胆比分。
输入为 store.latest_by_league()["nba"]["data"]["predictions"]。

R47-4 D-76 + R47-7 D-80 已锁 algo_suggestion / poisson_hhad, 但 nba_rows
自身核心契约 (7 字段透传 + home/away 缺省跳过 + 空 list 兜底) 0 直测。

D-81 锁定 4 契约:
- D-81-1: 主流程 happy (3 predictions, 1 缺 home -> 2 入选 + 完整 7 字段)
- D-81-2: 空 predictions -> []
- D-81-3: predictions=None -> [] (L261 `for p in predictions or []` 兜底)
- D-81-4: 完整字段透传 - direction/spread_pred/total_pred/margin/score 各自透传

依赖: 无, 纯函数 (无外部 IO)。
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
def tm_mod():
    """Spec-load web/services/team_match.py。"""
    spec = importlib.util.spec_from_file_location(
        "team_match_nba_rows_test",
        REPO_ROOT / "web" / "services" / "team_match.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-81 四契约 ────────────────────────


def test_nba_rows_d81_1_happy_skip_missing_team(tm_mod):
    """R47-8 D-81-1: 主流程 happy - 3 predictions, 1 缺 home -> 2 入选。

    pred1 完整 / pred2 缺 home / pred3 完整 -> 出 2 行, pred2 跳过。
    """
    out = tm_mod.nba_rows([
        {"home": "Lakers", "away": "Celtics", "direction": "主胜",
         "spread_prediction": "Lakers -5.5", "total_prediction": "Over 220.5",
         "predicted_margin": 8, "predicted_score": "112-104"},
        {"home": "", "away": "Heat", "direction": "客胜"},  # 缺 home -> 跳过
        {"home": "Bucks", "away": "Nets", "direction": "主胜",
         "spread_prediction": "Bucks -3.5", "total_prediction": "Under 230.5",
         "predicted_margin": 6, "predicted_score": "118-112"},
    ])
    assert len(out) == 2
    # 字段完整 (Lakers 行)
    lal = out[0]
    assert lal["home"] == "Lakers"
    assert lal["away"] == "Celtics"
    assert lal["match"] == "Lakers vs Celtics"  # '{home} vs {away}'
    assert lal["direction"] == "主胜"
    assert lal["spread_pred"] == "Lakers -5.5"
    assert lal["total_pred"] == "Over 220.5"
    assert lal["margin"] == 8
    assert lal["score"] == "112-104"


def test_nba_rows_d81_2_empty_list_returns_empty(tm_mod):
    """R47-8 D-81-2: 空 predictions -> [] (L261 for 0 次)。"""
    assert tm_mod.nba_rows([]) == []


def test_nba_rows_d81_3_none_returns_empty(tm_mod):
    """R47-8 D-81-3: predictions=None -> [] (L261 `or []` 兜底)。

    None 触发 falsy `or []` 兜底。LOCK: None -> []。
    (字符串/非 list 输入不在契约内, 实际会抛 AttributeError - 不锁此行为)
    """
    assert tm_mod.nba_rows(None) == []


def test_nba_rows_d81_4_all_missing_team_returns_empty(tm_mod):
    """R47-8 D-81-4: 所有 predictions 缺 home/away -> [] (全跳过)。

    L264 `if not home or not away: continue` 让所有 home='' 或 away='' 跳过。
    """
    out = tm_mod.nba_rows([
        {"home": "", "away": ""},
        {"home": None, "away": None},
        {"home": "Lakers", "away": ""},
        {"home": "", "away": "Heat"},
    ])
    assert out == []
    # None 也走 str(None) or '' 兜底
    assert tm_mod.nba_rows([{"home": None, "away": "Heat"}]) == []