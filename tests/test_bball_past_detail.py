"""R39-6: scripts/bball/run.py::_past_detail 4 tests direct + D-31 non-numeric lock.

回看详情 helper 0 direct tests; run() backtest path 间接覆盖 happy path,
但 _past_detail 的边界 (tie / 非数字 scores / scores 缺失) 未锁。

helper 内含一条隐契约 (D-31):
- `actual_winner = prediction["home"] if hs > aws else prediction["away"]`
  用 Python 默认 `>`, 即便 hs/aws 同是字符串也走字典序比较
  (e.g., "5" > "3" 在字典序是 True, 因为 '5' (ASCII 53) > '3' (ASCII 51));
  这个锁定是因为上游从 The Odds API 拿到的 scores 应为 int, 但若抓取
  链路或数据污染跌出 string, `_past_detail` 会 silent 走错 winner。
- `actual_score` 在 hs/aws 任一 None 时 = "" (filter short-circuit); 否则
  原样拼接: `f"{hs}-{aws}"` 把数值类型转字符串。
- `actual_winner` 同样仅在 hs/aws 双非 None 时写入。
- `prediction` 复制返回 (dict(prediction)), 非 mutate 共享。

4 tests:
- home wins: scores {home: 105, away: 100} -> winner=home, score="105-100"
- tie: scores {home: 100, away: 100} -> winner=away (else 分支), score="100-100"
- scores 缺失: game.scores absent -> 无 winner, score="" (filter 触发)
- D-31 字符串 scores: hs/aws 都是 str ("5" / "3") -> Python 默认 `>`
  走字典序比较 (locked 当前行为, 避免日后 silent 切到 try/except 失锁)
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bball import run as bball_run  # noqa: E402

# ──────────────────────── standard branches ────────────────────────


def _prediction():
    """_past_detail 输入: 已经过 _prediction 的 dict (含 home/away 等)。"""
    return {
        "home": "Lakers", "away": "Celtics",
        "home_en": "Los Angeles Lakers", "away_en": "Boston Celtics",
        "spread_pred": -3.5, "total_pred": 215.5,
    }


def test_past_detail_home_win_sets_winner_and_score():
    """R39-6: home 赢 -> actual_winner=prediction['home'], actual_score='105-100'."""
    game = {
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "scores": {"Los Angeles Lakers": 105, "Boston Celtics": 100},
    }
    out = bball_run._past_detail(game, _prediction())
    assert out["actual_winner"] == "Lakers"  # prediction["home"]
    assert out["actual_score"] == "105-100"


def test_past_detail_tie_uses_else_branch_winner():
    """R39-6: hs == aws -> `hs > aws` 为 False -> winner = prediction['away']。

    锁住 else 分支: tie 票给 away (不是 home), 避免后人改 `>=` 想给 home。
    """
    game = {
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "scores": {"Los Angeles Lakers": 100, "Boston Celtics": 100},
    }
    out = bball_run._past_detail(game, _prediction())
    # hs > aws 为 False -> else 分支: actual_winner = prediction["away"]
    assert out["actual_winner"] == "Celtics"
    assert out["actual_score"] == "100-100"


# ──────────────────────── missing scores ────────────────────────


def test_past_detail_missing_scores_no_winner_no_score():
    """R39-6: game.scores 缺 (默认 {}) -> filter 双 None -> 无 winner, score=""。

    关键: `actual_winner` 字段完全不应出现 (而非 None); `actual_score = ""`
    (字符串空而非 None)。前端按 key 存在与否决定渲染 'TBD' 还是隐藏。
    """
    game = {
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        # scores 缺 — 默认 {}
    }
    out = bball_run._past_detail(game, _prediction())
    assert "actual_winner" not in out
    assert out["actual_score"] == ""
    assert isinstance(out["actual_score"], str)


# ──────────────────────── D-31 non-numeric lock ────────────────────────


def test_past_detail_d31_string_scores_lexicographic_compare():
    """R39-6 D-31: scores 是 str -> Python 默认 `>` 走字典序比较 (锁定 bug-shape)。

    `scores = {"home": "10", "away": "9"}` (字符串非数字):
    - f-string 拼接 OK: actual_score = "10-9"
    - "10" > "9" 字典序: 比较 '1' vs '9', '1' (ASCII 49) < '9' (ASCII 57),
      所以 "10" > "9" 字典序是 False -> winner = prediction["away"]

    锁定现状: 当前实现依赖 Python 默认 `>` (字典序而非数值), 若日后改用
    try/except ValueError 转 int 比较, 这个测试会因 winner 变了而 fail,
    提醒契约变更需讨论 (前端期待依赖此行为)。

    注意: 本测还揭示真实订单 vs 字典序的非直觉差异, 是 D-31 锁的副作用。
    """
    game = {
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "scores": {"Los Angeles Lakers": "10", "Boston Celtics": "9"},
    }
    out = bball_run._past_detail(game, _prediction())
    # 字典序: "10" > "9" 是 False (因为 '1' < '9'); winner = else 分支 = away
    assert out["actual_winner"] == "Celtics"
    assert out["actual_score"] == "10-9"
