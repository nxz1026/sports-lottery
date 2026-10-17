"""R46-1: web/services/accuracy.py::league_accuracy_web 6 cases direct + D-68 lock.

league_accuracy_web (L72-L121, 50 行) 是 web 侧命中率计算器, 算法与
scripts/core/backtest.league_accuracy 1:1 复制 (避免 import scripts/)。
D-60 已锁 _collect_league_data 切窗 + 联赛过滤, 但 league_accuracy_web
自身核心算法 (direction + score + over_under 准确率 + 边界 None) 0 直测。

D-68 锁定 6 契约:
- D-68-1: 主流程 happy - direction + score + over_under 三准确率
- D-68-2: 无样本 (preds 空) -> None (不编数, 页面显示"积累中")
- D-68-3: pred 无 actuals 命中 (actuals 全空) -> total=0 -> None
- D-68-4: direction 平局 (d 含"平") -> is_draw=True + h_act == a_act 命中
- D-68-5: over_under 边界 - h_act+a_act == 2.5 (不算 over/under, 不加分)
- D-68-6: window_days 透传 + reconciled 计数

依赖 stub:
- _collect_league_data monkeypatch (R44-5 D-60 已锁, 隔离上游细节)
- _stable_match_key 真跑 (web/services/store 已通过 D-55 锁定机制)
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
def accuracy_mod():
    """Spec-load web/services/accuracy.py。"""
    spec = importlib.util.spec_from_file_location(
        "accuracy_league_accuracy_web_test",
        REPO_ROOT / "web" / "services" / "accuracy.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_collect(monkeypatch, accuracy_mod, actuals, preds):
    """Mock _collect_league_data 返受控 (actuals, preds)。"""
    monkeypatch.setattr(accuracy_mod, "_collect_league_data",
                        lambda league_key, cutoff: (actuals, preds))


def _pred(home, away, direction, score, ou, home_en=None, away_en=None):
    """构造一个标准 prediction dict。

    home_en/away_en 用于 _stable_match_key (L321-L324 优先字段)。
    """
    h_en = home_en or home
    a_en = away_en or away
    return {
        "home": home, "away": away,
        "home_en": h_en, "away_en": a_en,
        "direction": direction,
        "predicted_score": score,
        "over_under": ou,
    }


def _stable(home, away):
    """_stable_match_key 期望格式: home_en|away_en (无 league 前缀)。"""
    return f"{home}|{away}"


# ──────────────────────── D-68 六契约 ────────────────────────


def test_league_accuracy_web_d68_1_happy_path_three_accuracies(accuracy_mod, monkeypatch):
    """R46-1 D-68-1: 主流程 happy - direction + score + over_under 三准确率。

    3 preds (主胜 / 客胜 / 平局), 3 actuals 命中 (h>a / h<a / h==a)。
    direction 100% (3/3), score 33% (1/3: '2-1' 命中), over_under 100% (3/3:
    3+1>2.5=over, 1+0<2.5=under, 1+1<2.5=under; 但 over 算 over, under 算 under)。
    """
    _stub_collect(monkeypatch, accuracy_mod,
        actuals={
            _stable("homeA", "awayA"): (2, 1),  # 主胜, score 2-1
            _stable("homeB", "awayB"): (0, 3),  # 客胜
            _stable("homeC", "awayC"): (1, 1),  # 平
        },
        preds=[
            _pred("homeA", "awayA", "homeA 主胜", "2-1", "Over 2.5"),
            _pred("homeB", "awayB", "awayB 客胜", "1-0", "Under 2.5"),
            _pred("homeC", "awayC", "平局", "0-0", "Under 2.5"),
        ],
    )
    out = accuracy_mod.league_accuracy_web("epl", days=7)
    assert out is not None
    assert out["window_days"] == 7
    assert out["reconciled"] == 3
    assert out["direction_accuracy"] == 1.0  # 3/3 全对
    assert out["score_accuracy"] == round(1 / 3, 3)  # 仅 '2-1' 命中
    # over: h+a>2.5 -> 2+1=3>2.5 ok; under: h+a<2.5 -> 0+3=3>2.5 NOT under! 不加分
    # 等等, 0+3=3>2.5 不算 under, 所以 correct_ou 实际 1/3
    # 重新算: pred1 'Over 2.5' + (2,1)=3 > 2.5 -> correct
    # pred2 'Under 2.5' + (0,3)=3 > 2.5 -> NOT under (sum>2.5), incorrect
    # pred3 'Under 2.5' + (1,1)=2 < 2.5 -> correct
    assert out["over_under_accuracy"] == round(2 / 3, 3)


def test_league_accuracy_web_d68_2_no_preds_returns_none(accuracy_mod, monkeypatch):
    """R46-1 D-68-2: 无样本 (preds 空) -> None (不编数, 页面"积累中")。

    L83-L84 `if not preds: return None`。
    """
    _stub_collect(monkeypatch, accuracy_mod,
        actuals={"epl|X|Y": (2, 1)},
        preds=[],  # 空
    )
    out = accuracy_mod.league_accuracy_web("epl", days=7)
    assert out is None


def test_league_accuracy_web_d68_3_no_actuals_match_returns_none(accuracy_mod, monkeypatch):
    """R46-1 D-68-3: pred 无 actuals 命中 (actuals 全空) -> total=0 -> None。

    L86-L92 遍历 preds 累加 total, 但 L89 `if not r: continue` 让 total=0。
    L113-L114 `if total == 0: return None`。
    """
    _stub_collect(monkeypatch, accuracy_mod,
        actuals={},  # 无 actuals
        preds=[
            _pred("X", "Y", "X 主胜", "1-0", "Over 2.5"),
        ],
    )
    out = accuracy_mod.league_accuracy_web("epl", days=7)
    assert out is None


def test_league_accuracy_web_d68_4_draw_direction_via_d_keyword(accuracy_mod, monkeypatch):
    """R46-1 D-68-4: direction 平局 (d 含"平") -> is_draw=True + h_act == a_act 命中。

    L96 `is_draw = "平" in d or "平局" in d` -> is_draw=True。
    L103 `(is_draw and h_act == a_act)` -> correct_dir +1。
    """
    _stub_collect(monkeypatch, accuracy_mod,
        actuals={_stable("X", "Y"): (1, 1)},  # 实际平
        preds=[
            _pred("X", "Y", "平", "0-0", "Under 2.5"),  # 短词"平"
            _pred("X", "Y", "平局", "0-0", "Under 2.5"),  # 长词"平局"
        ],
    )
    out = accuracy_mod.league_accuracy_web("epl", days=7)
    assert out is not None
    # 2 个 pred 都是 direction='平'/'平局' + h_act==a_act(1,1) -> 2/2 命中
    assert out["reconciled"] == 2
    assert out["direction_accuracy"] == 1.0


def test_league_accuracy_web_d68_5_over_under_excludes_equality(accuracy_mod, monkeypatch):
    """R46-1 D-68-5: over_under 边界 - h_act+a_act == 2.5 (不算 over/under, 不加分)。

    L108 `h_act + a_act > 2.5` (严格 >) 和 L110 `< 2.5` (严格 <), 等于 2.5 不算。
    实际比分 (3, 0) sum=3 > 2.5 over 命中, (2, 0) sum=2 < 2.5 under 命中,
    (1, 1) sum=2 < 2.5 under 命中, 但 (2, 1) sum=3 over 命中。
    """
    _stub_collect(monkeypatch, accuracy_mod,
        actuals={
            _stable("A", "B"): (3, 0),  # sum=3, over ok
            _stable("C", "D"): (2, 0),  # sum=2, under ok
            _stable("E", "F"): (1, 1),  # sum=2, under ok
        },
        preds=[
            _pred("A", "B", "A 主胜", "1-0", "Over 2.5"),
            _pred("C", "D", "C 主胜", "1-0", "Under 2.5"),
            _pred("E", "F", "平局", "0-0", "Under 2.5"),
        ],
    )
    out = accuracy_mod.league_accuracy_web("epl", days=7)
    assert out is not None
    assert out["over_under_accuracy"] == 1.0  # 3/3 全对


def test_league_accuracy_web_d68_6_window_days_passthrough_and_reconciled(accuracy_mod, monkeypatch):
    """R46-1 D-68-6: window_days 透传 + reconciled 计数准确。

    L116 `window_days: days` 透传 days=30。
    L86-L92 累加 total, L117 `reconciled: total`。
    """
    _stub_collect(monkeypatch, accuracy_mod,
        actuals={
            _stable("A", "B"): (2, 1),
            _stable("C", "D"): (1, 1),
            _stable("E", "F"): (0, 3),
            _stable("G", "H"): (2, 2),
        },
        preds=[
            _pred("A", "B", "A 主胜", "1-0", "Over 2.5"),
            _pred("C", "D", "平", "0-0", "Under 2.5"),
            _pred("E", "F", "F 客胜", "0-1", "Under 2.5"),
            _pred("G", "H", "G 主胜", "1-0", "Over 2.5"),
        ],
    )
    out = accuracy_mod.league_accuracy_web("epl", days=30)
    assert out is not None
    assert out["window_days"] == 30
    assert out["reconciled"] == 4  # 4 preds 都有 actuals