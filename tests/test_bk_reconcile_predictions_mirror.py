"""P2-14 第三份实现对账：scripts/core/backtest._bk_reconcile_predictions
vs 手算期望 + web/services/accuracy.league_accuracy_web 内部计数。

`_bk_reconcile_predictions(preds, actuals)` 是"方向/比分/OU 命中率"的
第三份实现（与 league_accuracy_web / league_accuracy 算法 1:1 复制）。
模块 docstring 已声明"故意复制 + PR review 守门"。

本测试断言：

1. 同一组 (actuals, preds) 下，``_bk_reconcile_predictions`` 返回的
   ``(total, correct_dir, correct_score, correct_ou, details)`` 与手算
   完全一致
2. 与 ``league_accuracy_web`` 的内部计数（``reconciled`` = total、
   correct_dir/score/ou 计数）逐字段相等 ⇒ 三个实现（web + scripts +
   backtest）对同一数据产同一结果
3. ``details`` 列表每项的 ``direction_correct`` 字段与手算 ``is_home_win
   and h>a`` / ``is_draw and h==a`` / ``is_away_win and h<a`` 完全一致

若任一端改了判定公式（如 is_draw 从"平"/"平局"改成"Draw"），本测试
即刻捕获。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _build_fixture():
    """与 test_accuracy_mirror 同款 6 场 preds 混合 fixture，便于互查。"""
    actuals = {
        "Arsenal|Chelsea": (2, 0),
        "Liverpool|ManCity": (1, 1),
        "Spurs|WestHam": (0, 3),
    }
    preds = [
        {"home_en": "Arsenal", "away_en": "Chelsea",
         "direction": "Arsenal 胜", "predicted_score": "2-0", "over_under": "Over 2.5"},
        {"home_en": "Liverpool", "away_en": "ManCity",
         "direction": "Liverpool 胜", "predicted_score": "2-1", "over_under": "Under 2.5"},
        {"home_en": "Spurs", "away_en": "WestHam",
         "direction": "Spurs 胜", "predicted_score": "1-0", "over_under": "Under 2.5"},
        {"home_en": "Arsenal", "away_en": "Chelsea",
         "direction": "Arsenal vs Chelsea 主胜", "predicted_score": "3-0", "over_under": "Over 2.5"},
        {"home_en": "Liverpool", "away_en": "ManCity",
         "direction": "Liverpool vs ManCity 平局", "predicted_score": "1-1", "over_under": "Under 2.5"},
        {"home_en": "Spurs", "away_en": "WestHam",
         "direction": "Spurs vs WestHam 平局", "predicted_score": "1-1", "over_under": "Over 2.5"},
    ]
    return actuals, preds


def _expected_counts(actuals, preds):
    """手算 expected (total, correct_dir, correct_score, correct_ou)。

    算法与 ``_bk_reconcile_predictions`` 同（1:1 复制），但完全独立实现，
    用于守住"三个实现对同一 fixture 算同一答案"。
    """
    from core.backtest import _bk_stable_key

    total = correct_dir = correct_score = correct_ou = 0
    details = []
    for p in preds:
        key = _bk_stable_key(p)
        r = actuals.get(key)
        if not r:
            continue
        h_act, a_act = r
        total += 1
        d = p.get("direction", "")
        home_team = p.get("home", "")
        away_team = p.get("away", "")
        is_draw = "平" in d or "平局" in d
        is_home_win = is_away_win = False
        if not is_draw:
            if d.startswith(home_team):
                is_home_win = True
            elif d.startswith(away_team):
                is_away_win = True
        dir_ok = (
            (is_home_win and h_act > a_act)
            or (is_draw and h_act == a_act)
            or (is_away_win and h_act < a_act)
        )
        if dir_ok:
            correct_dir += 1
        if p.get("predicted_score", "") == f"{h_act}-{a_act}":
            correct_score += 1
        ou = p.get("over_under", "")
        if "Over" in ou and h_act + a_act > 2.5:
            correct_ou += 1
        elif "Under" in ou and h_act + a_act < 2.5:
            correct_ou += 1
        details.append({
            "match": p.get("match", ""),
            "predicted": p.get("predicted_score", ""),
            "actual": f"{h_act}-{a_act}",
            "direction_correct": dir_ok,
        })
    return total, correct_dir, correct_score, correct_ou, details


def test_counts_match_hand_computed_with_same_fixture():
    """``_bk_reconcile_predictions`` 输出 = 手算期望。"""
    from core.backtest import _bk_reconcile_predictions

    actuals, preds = _build_fixture()
    expected_total, expected_dir, expected_score, expected_ou, expected_details = _expected_counts(actuals, preds)

    got_total, got_dir, got_score, got_ou, got_details = _bk_reconcile_predictions(preds, actuals)

    assert got_total == expected_total
    assert got_dir == expected_dir
    assert got_score == expected_score
    assert got_ou == expected_ou
    assert got_details == expected_details


def test_counts_match_league_accuracy_web_when_loaders_monkeypatched(monkeypatch):
    """``_bk_reconcile_predictions`` 与 ``league_accuracy_web`` 内部计数一致。

    monkeypatch 两侧 loader → 同一 fixture → 断言 web 输出字段与 _bk
    返回的 (total, correct_dir, ...) 完全相等（web 输出是 round 比值，
    反推即可得底层计数）。
    """
    actuals, preds = _build_fixture()

    from web.services import accuracy
    monkeypatch.setattr(accuracy, "_collect_league_data",
                        lambda league_key, cutoff: (actuals, preds))

    web_out = accuracy.league_accuracy_web("epl", days=7)
    assert web_out is not None

    # 反推 web 端底层计数
    web_total = web_out["reconciled"]
    web_dir_count = round(web_out["direction_accuracy"] * web_total)
    web_score_count = round(web_out["score_accuracy"] * web_total)
    web_ou_count = round(web_out["over_under_accuracy"] * web_total)

    from core.backtest import _bk_reconcile_predictions
    got_total, got_dir, got_score, got_ou, _ = _bk_reconcile_predictions(preds, actuals)

    assert web_total == got_total
    assert web_dir_count == got_dir
    assert web_score_count == got_score
    assert web_ou_count == got_ou


def test_counts_match_league_accuracy_scripts_when_loaders_monkeypatched(monkeypatch):
    """``_bk_reconcile_predictions`` 与 ``league_accuracy``（scripts 版）一致。"""
    actuals, preds = _build_fixture()

    from core import backtest
    monkeypatch.setattr(backtest, "_bk_collect_league_data",
                        lambda league_key, cutoff: (actuals, preds))

    scripts_out = backtest.league_accuracy("epl", days=7)
    assert scripts_out is not None

    scripts_total = scripts_out["reconciled"]
    scripts_dir_count = round(scripts_out["direction_accuracy"] * scripts_total)
    scripts_score_count = round(scripts_out["score_accuracy"] * scripts_total)
    scripts_ou_count = round(scripts_out["over_under_accuracy"] * scripts_total)

    from core.backtest import _bk_reconcile_predictions
    got_total, got_dir, got_score, got_ou, _ = _bk_reconcile_predictions(preds, actuals)

    assert scripts_total == got_total
    assert scripts_dir_count == got_dir
    assert scripts_score_count == got_score
    assert scripts_ou_count == got_ou


def test_details_match_hand_computed_per_row():
    """``details`` 列表逐项与手算对账（direction_correct / predicted / actual）。"""
    from core.backtest import _bk_reconcile_predictions

    actuals, preds = _build_fixture()
    _, _, _, _, got_details = _bk_reconcile_predictions(preds, actuals)
    _, _, _, _, expected_details = _expected_counts(actuals, preds)

    assert len(got_details) == len(expected_details)
    for g, e in zip(got_details, expected_details):
        assert g == e, f"got={g} expected={e}"


def test_unmatched_preds_excluded():
    """actuals 中无对应键的 preds 不计 total。"""
    from core.backtest import _bk_reconcile_predictions

    actuals = {"Arsenal|Chelsea": (2, 0)}
    preds = [
        {"home_en": "Arsenal", "away_en": "Chelsea",
         "direction": "Arsenal 胜", "predicted_score": "2-0", "over_under": "Over 2.5"},
        {"home_en": "Liverpool", "away_en": "ManCity",  # 无对应 actuals
         "direction": "Liverpool 胜", "predicted_score": "1-0", "over_under": "Over 2.5"},
    ]
    got_total, got_dir, got_score, got_ou, got_details = _bk_reconcile_predictions(preds, actuals)

    assert got_total == 1
    assert len(got_details) == 1
    assert got_details[0]["match"] == ""  # fixture 没设 match 字段
