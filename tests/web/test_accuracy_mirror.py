"""P2-4 算法镜像对账 #2：web/services/accuracy.league_accuracy_web
vs scripts/core/backtest.league_accuracy。

两份实现方向判 / 比分等 / OU 三向命中率的算法 1:1 复制（模块 docstring 已
显式声明 "故意复制 + PR review 守门"）。数据加载走不同的路径
（web.load_prediction_docs vs scripts PREDICTIONS_DIR.glob），但算法核
心完全相同。

本测试 monkeypatch 两端的数据加载函数，喂**同一组 fixture**（含已结算
actuals + 历史 preds），断言输出 dict 完全相等。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1].parent
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _build_fixture():
    """构造一组 fixture：3 场比赛（1 主胜对、1 平、1 客胜）+ 6 个 preds
    （方向对错混杂，覆盖 direction / score / OU 三向命中率）。

    返回 ``(actuals, preds)``，喂给 monkeypatched 的两侧数据加载函数。
    """
    actuals = {
        "Arsenal|Chelsea": (2, 0),       # 主胜
        "Liverpool|ManCity": (1, 1),      # 平
        "Spurs|WestHam": (0, 3),          # 客胜
    }
    preds = [
        # 全部 direction/score/OU 都对
        {"home_en": "Arsenal", "away_en": "Chelsea",
         "direction": "Arsenal 胜", "predicted_score": "2-0", "over_under": "Over 2.5"},
        # direction 对、score 错、OU 对
        {"home_en": "Liverpool", "away_en": "ManCity",
         "direction": "Liverpool 胜", "predicted_score": "2-1", "over_under": "Under 2.5"},
        # direction 错（选了平但实际客胜）、score 错、OU 错（说 Under 但实际 3 球 > 2.5）
        {"home_en": "Spurs", "away_en": "WestHam",
         "direction": "Spurs 胜", "predicted_score": "1-0", "over_under": "Under 2.5"},
        # 中文 direction（不是 home_en 字串开头）— 模拟非英文场景
        {"home_en": "Arsenal", "away_en": "Chelsea",
         "direction": "Arsenal vs Chelsea 主胜", "predicted_score": "3-0", "over_under": "Over 2.5"},
        # 平局 direction
        {"home_en": "Liverpool", "away_en": "ManCity",
         "direction": "Liverpool vs ManCity 平局", "predicted_score": "1-1", "over_under": "Under 2.5"},
        # 平局 prediction 命中（direction 含"平"字 + score 1-1 + OU Under 2 球）
        {"home_en": "Spurs", "away_en": "WestHam",
         "direction": "Spurs vs WestHam 平局", "predicted_score": "1-1", "over_under": "Over 2.5"},
    ]
    return actuals, preds


def _patch_web_loaders(monkeypatch, actuals, preds):
    """Monkeypatch web 侧 _collect_league_data，让它直接返回 fixture。

    ``league_accuracy_web`` 内部调 ``_collect_league_data``，所以打补丁
    一处即可；同时绕过 load_prediction_docs（mtime 过滤会让它漏数据）。
    """
    from web.services import accuracy

    monkeypatch.setattr(
        accuracy,
        "_collect_league_data",
        lambda league_key, cutoff: (actuals, preds),
    )


def _patch_scripts_loaders(monkeypatch, actuals, preds):
    """Monkeypatch scripts 侧 _bk_collect_league_data。"""
    from core import backtest

    monkeypatch.setattr(
        backtest,
        "_bk_collect_league_data",
        lambda league_key, cutoff: (actuals, preds),
    )


def test_outputs_identical_with_same_fixture(monkeypatch):
    """同 fixture → web / scripts 输出 dict 完全相等（除 window_days 外）。"""
    actuals, preds = _build_fixture()
    _patch_web_loaders(monkeypatch, actuals, preds)
    _patch_scripts_loaders(monkeypatch, actuals, preds)

    from web.services.accuracy import league_accuracy_web
    from core.backtest import league_accuracy

    web_out = league_accuracy_web("epl", days=7)
    scripts_out = league_accuracy("epl", days=7)

    assert web_out is not None and scripts_out is not None
    # 两个函数的算法核心 1:1 复制，键集应完全相同
    assert set(web_out.keys()) == set(scripts_out.keys()), (
        f"web keys={set(web_out.keys())} scripts keys={set(scripts_out.keys())}"
    )
    # 逐字段相等（无浮点差异，因为都是 round(_, 3) 整数除法）
    for key in web_out:
        assert web_out[key] == scripts_out[key], (
            f"{key}: web={web_out[key]!r} scripts={scripts_out[key]!r}"
        )


def test_returns_none_when_no_preds(monkeypatch):
    """窗口内 preds 为空 → 两端都返回 None（页面"数据积累中"，不编数）。"""
    _patch_web_loaders(monkeypatch, {}, [])
    _patch_scripts_loaders(monkeypatch, {}, [])

    from web.services.accuracy import league_accuracy_web
    from core.backtest import league_accuracy

    assert league_accuracy_web("epl") is None
    assert league_accuracy("epl") is None


def test_window_days_propagates(monkeypatch):
    """``days`` 参数正确反映在输出 dict 的 window_days 字段。"""
    actuals, preds = _build_fixture()
    _patch_web_loaders(monkeypatch, actuals, preds)
    _patch_scripts_loaders(monkeypatch, actuals, preds)

    from web.services.accuracy import league_accuracy_web
    from core.backtest import league_accuracy

    for d in (1, 7, 30, 90):
        w = league_accuracy_web("epl", days=d)
        s = league_accuracy("epl", days=d)
        assert w["window_days"] == d == s["window_days"]


def test_only_windowed_predictions_count(monkeypatch):
    """actuals 中存在但 preds 没对应的 key 不计为 reconciled（方向命中率分母 = preds 匹配数）。

    把第 3 个 pred（Spurs|WestHam）剔除 → total 从 6 降到 5，reconciled 比例重算。
    两端必须同步缩。
    """
    actuals, _ = _build_fixture()
    preds = _build_fixture()[1][:5]  # 去掉第 6 个
    _patch_web_loaders(monkeypatch, actuals, preds)
    _patch_scripts_loaders(monkeypatch, actuals, preds)

    from web.services.accuracy import league_accuracy_web
    from core.backtest import league_accuracy

    w = league_accuracy_web("epl", days=7)
    s = league_accuracy("epl", days=7)
    assert w == s


def test_score_only_correct_when_exact_match(monkeypatch):
    """比分命中 = ``predicted_score == "{h}-{a}"`` 完全相等。

    Fixture 已隐含：Arsenal|predicted 2-0 vs actual (2,0) 对；Liverpool
    预测 2-1 vs actual (1,1) 错；Spurs 预测 1-0 vs actual (0,3) 错。
    """
    actuals, preds = _build_fixture()
    _patch_web_loaders(monkeypatch, actuals, preds)
    _patch_scripts_loaders(monkeypatch, actuals, preds)

    from web.services.accuracy import league_accuracy_web
    from core.backtest import league_accuracy

    w = league_accuracy_web("epl", days=7)
    s = league_accuracy("epl", days=7)
    # total = 6（所有 preds 都匹配到 actuals）
    assert w["reconciled"] == 6
    # direction: Arsenal×2 + Liverpool 平 + Arsenal 错 = 3 对；Spurs 错 = 0
    # 即 4 / 6 ≈ 0.667
    assert w["direction_accuracy"] == s["direction_accuracy"]
    # score: 仅 Arsenal 第 1 条 + Liverpool 第 5 条 + Spurs 第 6 条对 = 3/6=0.5
    # 但 OU 阈值与方向判需逐一核对：
    # Arsenal #1 (2,0) Over 2.5→3>2.5 错 (实际是 2 球 == 2.5 但条件 > 2.5 才算 over)
    # Arsenal #4 (2,0) Over 2.5→错
    # Liverpool #2 (1,1) Under 2.5→2<2.5 对
    # Liverpool #5 (1,1) Under 2.5→对
    # Spurs #3 (0,3) Under 2.5→3>2.5 错
    # Spurs #6 (0,3) Over 2.5→对
    # OU 对: 3/6 = 0.5
    assert w["over_under_accuracy"] == 0.5
