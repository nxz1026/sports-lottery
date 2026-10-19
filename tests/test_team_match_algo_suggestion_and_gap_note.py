"""R47-4: web/services/team_match.py::algo_suggestion + jc_gap_note 7 cases direct + D-76/D-77 lock.

algo_suggestion (L119-L160, 42 行) 从预测条目抽算法层关键信息:
- pick (主胜/平/客胜, 由 direction 决定)
- stars (从 "N-star" 解析 N, 缺/不匹配 -> 0)
- score (predicted_score 字符串)
- rating (confidence_score 0-1 -> 0-100 round, 缺 -> None)
- top2 (poisson_top3 前 2 个 score 字符串, 缺 -> [])
- algo_weighted (reasoning_factors 三项最高概率, 缺 -> None)

jc_gap_note (L243-L250, 10 行) 错位方向一句话: 同一侧上模型概率与官方隐含
的差异,按 diff 区间分 3 档文案。

D-60/D-62/D-74/D-75 已锁 accuracy / combo / norm_name / odd_suggestion,
但 algo_suggestion / jc_gap_note 自身核心契约 (direction 倾向 + stars 解析
+ rating 转换 + top2 截断 + 3 档 diff 文案) 0 直测。

D-76 锁定 4 契约 (algo_suggestion):
- D-76-1: 主流程 happy (含 direction + 2-star + score + confidence + top3 + rf)
- D-76-2: 平局 (direction 含"平" -> pick=平)
- D-76-3: confidence_score 缺 -> rating=None
- D-76-4: stars 缺或非标准格式 -> stars=0

D-77 锁定 3 契约 (jc_gap_note):
- D-77-1: diff >= 0.15 -> "模型较市场更看好{worst}"
- D-77-2: diff <= -0.15 -> "官方隐含更看好{worst}"
- D-77-3: -0.15 < diff < 0.15 -> "{worst}分歧居前"
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
        "team_match_algo_gap_note_test",
        REPO_ROOT / "web" / "services" / "team_match.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-76 algo_suggestion ────────────────────────


def test_algo_suggestion_d76_1_happy_path_full_pred(tm_mod):
    """R47-4 D-76-1: 主流程 happy - 全字段填充。

    direction="博洛尼亚 胜(接近)" -> home="博洛尼亚" 含 direction.split('(')[0]
    -> side="主胜"; stars="2-star" -> 2; confidence=0.55 -> rating=55 (round 0.55*100);
    poisson_top3 3 项 -> top2 取前 2 字符串。
    """
    out = tm_mod.algo_suggestion({
        "home": "博洛尼亚",
        "away": "卡利亚里",
        "direction": "博洛尼亚 胜(接近)",
        "stars": "2-star",
        "predicted_score": "1-0",
        "confidence_score": 0.55,
        "poisson_top3": [
            {"score": "1-0", "prob": 0.20},
            {"score": "2-0", "prob": 0.15},
            {"score": "0-0", "prob": 0.10},
        ],
    })
    assert out["pick"] == "主胜"
    assert out["stars"] == 2
    assert out["score"] == "1-0"
    assert out["rating"] == 55  # round(0.55 * 100) = 55
    assert out["top2"] == ["1-0", "2-0"]
    assert out["direction"] == "博洛尼亚 胜(接近)"


def test_algo_suggestion_d76_2_draw_via_direction_keyword(tm_mod):
    """R47-4 D-76-2: 平局检测 - direction 含"平" -> pick=平。

    L134 `if "平" in direction: side = "平"` 优先于 home 检查。
    """
    out = tm_mod.algo_suggestion({
        "home": "阿森纳",
        "away": "切尔西",
        "direction": "平局(高概率)",
        "stars": "1-star",
    })
    assert out["pick"] == "平"
    assert out["stars"] == 1


def test_algo_suggestion_d76_3_missing_confidence_returns_none_rating(tm_mod):
    """R47-4 D-76-3: confidence_score 缺 -> rating=None。

    L148 `round(conf * 100) if isinstance(conf, (int, float)) else None`。
    缺键 / None / 字符串 都走 else None。
    """
    # 缺键
    out = tm_mod.algo_suggestion({"home": "A", "away": "B",
                                    "direction": "A 胜", "stars": "2-star"})
    assert out["rating"] is None
    # None
    out_none = tm_mod.algo_suggestion({"home": "A", "direction": "A 胜",
                                         "confidence_score": None})
    assert out_none["rating"] is None
    # 字符串
    out_str = tm_mod.algo_suggestion({"home": "A", "direction": "A 胜",
                                       "confidence_score": "0.55"})
    assert out_str["rating"] is None


def test_algo_suggestion_d76_4_invalid_stars_returns_zero(tm_mod):
    r"""R47-4 D-76-4: stars 缺或非标准格式 -> stars=0。

    L127-130 `m = re.search(r"(\d+)-star", stars); if m: n_star = int(m.group(1))`。
    缺 stars / "无星" / "star" (无数字) / None 都 -> 0。
    """
    # 缺 stars
    out = tm_mod.algo_suggestion({"home": "A", "direction": "A 胜"})
    assert out["stars"] == 0
    # "无星" 不匹配 regex
    out_no = tm_mod.algo_suggestion({"home": "A", "direction": "A 胜", "stars": "无星"})
    assert out_no["stars"] == 0
    # "star" (无数字) 不匹配
    out_word = tm_mod.algo_suggestion({"home": "A", "direction": "A 胜", "stars": "star"})
    assert out_word["stars"] == 0
    # None
    out_none = tm_mod.algo_suggestion({"home": "A", "direction": "A 胜", "stars": None})
    assert out_none["stars"] == 0


# ──────────────────────── D-77 jc_gap_note ────────────────────────


def test_jc_gap_note_d77_1_model_bullish_when_diff_high(tm_mod):
    """R47-4 D-77-1: diff >= 0.15 -> "模型较市场更看好{worst}"。

    L246-247: model_p 比 official_p 高 >= 15 个百分点 -> 模型更看好 worst 侧。
    """
    assert tm_mod.jc_gap_note("主胜", 0.40, 0.60) == "模型较市场更看好主胜"
    assert tm_mod.jc_gap_note("客胜", 0.30, 0.50) == "模型较市场更看好客胜"
    # 边界 diff = 0.15
    assert tm_mod.jc_gap_note("主胜", 0.35, 0.50) == "模型较市场更看好主胜"


def test_jc_gap_note_d77_2_official_bullish_when_diff_low(tm_mod):
    """R47-4 D-77-2: diff <= -0.15 -> "官方隐含更看好{worst}"。

    L248-249: 官方隐含更高 (model_p 低) >= 15 个百分点 -> 官方更看好。
    """
    assert tm_mod.jc_gap_note("主胜", 0.60, 0.40) == "官方隐含更看好主胜"
    assert tm_mod.jc_gap_note("客胜", 0.50, 0.30) == "官方隐含更看好客胜"
    # 边界 diff = -0.15
    assert tm_mod.jc_gap_note("主胜", 0.50, 0.35) == "官方隐含更看好主胜"


def test_jc_gap_note_d77_3_neutral_when_diff_small(tm_mod):
    """R47-4 D-77-3: -0.15 < diff < 0.15 -> "{worst}分歧居前"。

    L250: 中性文案, 模型与官方差异 < 15%, 仍标 worst 为分歧居前。
    """
    assert tm_mod.jc_gap_note("主胜", 0.50, 0.55) == "主胜分歧居前"
    assert tm_mod.jc_gap_note("客胜", 0.30, 0.20) == "客胜分歧居前"
    # diff = 0
    assert tm_mod.jc_gap_note("平", 0.33, 0.33) == "平分歧居前"
    # 边界 diff = 0.14 (略小于 0.15)
    assert tm_mod.jc_gap_note("主胜", 0.40, 0.54) == "主胜分歧居前"
