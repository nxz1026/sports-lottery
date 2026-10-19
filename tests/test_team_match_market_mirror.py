"""P2-13 镜像对账：web/services/team_match.odd_suggestion / jc_gap
vs scripts/market/devig.devig。

两份实现都基于"十进制赔率 → 隐含概率"公式（``(1/o)/Σ(1/o)``）：
- ``odd_suggestion``：had 三项中"赔率最低"项 ⇒ 隐含概率最高（方向倾向）
- ``jc_gap``：had 三项 vs algo.probs 的 TVD/2（错位榜）

scripts/market/devig.devig 是同一公式的纯函数包装。本测试断言 web 端
输出与 scripts 端在 1e-9 内一致，把"赔率 → 概率"这条核心公式的
双向漂移风险锁住。

差异守门：
- scripts 端是 strict（非法 → ValueError），web 端是 lenient（非法 →
  跳过该场 / 返回空）。本测试只在**有效输入**下比对。
- odd_suggestion 的"pick"是中文键（主胜/平/客胜），scripts 是英文键
  （home/draw/away），比对时映射。
- jc_gap 还多算 TVD/2 与 worst 侧，这是 web 独有的派生；公式与官方
  隐含概率的"骨架"一致即可。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


# 几组实战 had 赔率（h=主胜 d=平 a=客胜）
_HAD_FIXTURES = [
    # (label, had dict)
    ("典型主胜", {"h": 1.85, "d": 3.40, "a": 4.20}),
    ("典型平", {"h": 2.62, "d": 3.20, "a": 2.62}),
    ("强客让", {"h": 3.05, "d": 3.20, "a": 2.06}),
    ("均衡高水", {"h": 2.40, "d": 3.10, "a": 2.90}),
]


# web had 三侧键位 → scripts market.devig 英文键
_WEB_HAD_TO_SCRIPTS = {"h": "home", "d": "draw", "a": "away"}

# 中文 → 英文（用于 odd_suggestion 输出 ↔ scripts 端 argmax 对齐）
_ZH_TO_EN = {"主胜": "home", "平": "draw", "客胜": "away"}


def _devig_for_had(had: dict[str, float]) -> dict[str, float]:
    """scripts 端：用 market.devig.devig 算 had 三项隐含概率（英文键）。

    ``had`` 是 web 端 ``r["had"]`` 形状：{"h": 1.85, "d": 3.40, "a": 4.20}。
    scripts market.devig 期望 {"home": 1.85, ...}。先做键映射。
    """
    from market.devig import devig
    en = {}
    for k, v in had.items():
        en_key = _WEB_HAD_TO_SCRIPTS.get(k)
        if en_key is not None:
            en[en_key] = float(v)
    return devig(en)


def test_odd_suggestion_pick_matches_lowest_implied_prob():
    """``odd_suggestion`` 的 pick = 赔率最低项 ⇔ ``market.devig`` 隐含概率最高项。"""
    from web.services.team_match import odd_suggestion

    for label, had in _HAD_FIXTURES:
        # had 内部是 str 或 numeric 都支持，先归一 float
        had_f = {k: float(v) for k, v in had.items()}
        web_out = odd_suggestion(had_f)
        assert web_out, f"{label}: odd_suggestion 返空"

        scripts_probs = _devig_for_had(had_f)
        # scripts 端：argmax of implied prob = pick of lowest odds
        scripts_pick_en = max(scripts_probs, key=lambda k: scripts_probs[k])
        scripts_pick_zh = {v: k for k, v in _ZH_TO_EN.items()}[scripts_pick_en]

        assert web_out["pick"] == scripts_pick_zh, (
            f"{label}: web pick={web_out['pick']} scripts pick={scripts_pick_zh}"
        )
        # 数值上：赔率应一致
        assert float(web_out["odds"]) == pytest.approx(min(had_f.values()), abs=1e-9)


def test_odd_suggestion_implied_probs_match_market_devig():
    """``odd_suggestion`` 没直接暴露 implied_probs，但 web 端走的是同一公式。

    把 had 三项套到 scripts 端 devig，得到的 home/draw/away 三概率应与
    手算 ``(1/o)/Σ(1/o)`` 完全相等。这是公式等价的最强断言。
    """
    from market.devig import devig

    for label, had in _HAD_FIXTURES:
        had_f = {k: float(v) for k, v in had.items()}
        en = {}
        for k, v in had_f.items():
            en_key = _WEB_HAD_TO_SCRIPTS.get(k)
            if en_key is not None:
                en[en_key] = v
        scripts_probs = devig(en)

        # 手算（odd_suggestion / jc_gap 用的公式）
        s = sum(1 / v for v in had_f.values())
        manual = {"主胜": (1 / had_f["h"]) / s,
                  "平": (1 / had_f["d"]) / s,
                  "客胜": (1 / had_f["a"]) / s}
        for key in ("主胜", "平", "客胜"):
            en_key = _ZH_TO_EN[key]
            assert manual[key] == pytest.approx(scripts_probs[en_key], abs=1e-12), (
                f"{label} {key}: 手算={manual[key]} scripts={scripts_probs[en_key]}"
            )


def test_jc_gap_official_probs_match_market_devig():
    """``jc_gap`` 输出 ``official`` 字段：每场三项隐含概率 = scripts.devig.devig。

    构造一条带 had 三项 + algo.probs 的 row，跑 jc_gap，断言 official
    字段三项与 scripts 端 devig 在 1e-9 内一致。
    """
    from market.devig import devig

    from web.services.team_match import jc_gap

    had = {"h": 1.85, "d": 3.40, "a": 4.20}
    probs = {"主胜": 0.55, "平": 0.25, "客胜": 0.20}
    rows = [{"match_num": 8001, "league_cn": "英超", "had": had,
             "algo": {"probs": probs}}]

    out = jc_gap(rows)
    assert len(out) == 1
    it = out[0]
    assert set(it["official"]) == {"主胜", "平", "客胜"}

    # 与 scripts 端 devig 对账（web 在官方字段上 round(_, 3) ⇒ 与 scripts 精度差 ≤ 5e-4）
    en = {}
    for k, v in had.items():
        en_key = _WEB_HAD_TO_SCRIPTS.get(k)
        if en_key is not None:
            en[en_key] = float(v)
    scripts_probs = devig(en)
    for zh_key, en_key in _ZH_TO_EN.items():
        assert it["official"][zh_key] == pytest.approx(scripts_probs[en_key], abs=5e-4), (
            f"{zh_key}: web={it['official'][zh_key]} scripts={scripts_probs[en_key]}"
        )


def test_jc_gap_gap_value_is_tvd_half():
    """``gap`` = Σ|official - algo| / 2 = TVD/2（Total Variation Distance 一半）。

    TVD = max over partitions of |P-Q|；本公式是 1D 离散版的常见定义。
    与 scripts 端 devig + 手算 TVD 对账。
    """
    from market.devig import devig

    from web.services.team_match import jc_gap

    had = {"h": 2.62, "d": 3.20, "a": 2.62}
    probs = {"主胜": 0.40, "平": 0.35, "客胜": 0.25}
    rows = [{"match_num": 9001, "league_cn": "西甲", "had": had,
             "algo": {"probs": probs}}]

    out = jc_gap(rows)
    it = out[0]

    en = {}
    for k, v in had.items():
        en_key = _WEB_HAD_TO_SCRIPTS.get(k)
        if en_key is not None:
            en[en_key] = float(v)
    scripts_probs = devig(en)
    expected_gap = sum(abs(scripts_probs[_ZH_TO_EN[zh]] - probs[zh]) for zh in probs) / 2
    # web 在 gap 字段也 round(_, 3)，与手算精度差 ≤ 5e-4
    assert it["gap"] == pytest.approx(expected_gap, abs=5e-4)


def test_jc_gap_worst_side_matches_max_abs_diff():
    """``worst`` 字段 = max|official - algo| 一侧。

    与"手算差异绝对值最大键"对账。
    """
    from market.devig import devig

    from web.services.team_match import jc_gap

    had = {"h": 1.85, "d": 3.40, "a": 4.20}
    probs = {"主胜": 0.10, "平": 0.50, "客胜": 0.40}  # 主胜差异最大
    rows = [{"match_num": 8002, "league_cn": "意甲", "had": had,
             "algo": {"probs": probs}}]

    out = jc_gap(rows)
    it = out[0]
    assert it["worst"] == "主胜"

    # 验算
    en = {}
    for k, v in had.items():
        en_key = _WEB_HAD_TO_SCRIPTS.get(k)
        if en_key is not None:
            en[en_key] = float(v)
    scripts_probs = devig(en)
    diffs = {zh: abs(scripts_probs[_ZH_TO_EN[zh]] - probs[zh]) for zh in probs}
    assert max(diffs, key=diffs.get) == "主胜"


def test_jc_gap_filters_invalid_had():
    """``jc_gap`` 对 had 非法（<=1 / 非数）应跳过该场，对账 scripts 端
    ``devig`` 抛 ValueError 的同类输入。

    注："缺 a" 不属于 scripts 端的非法输入（``devig`` 接受 len=2），web 端
    额外要求 ``h/d/a 三项齐``，是 web 的额外约束。脚本侧 len < 2 才抛 ValueError。
    """
    from market.devig import devig

    from web.services.team_match import jc_gap

    probs = {"主胜": 0.5, "平": 0.3, "客胜": 0.2}

    # a == 1（scripts v <= 1.0 ⇒ ValueError；web 跳过）
    rows = [{"match_num": 2, "had": {"h": 1.85, "d": 3.40, "a": 1.0}, "algo": {"probs": probs}}]
    assert jc_gap(rows) == []
    with pytest.raises(ValueError):
        devig({"home": 1.85, "draw": 3.40, "away": 1.0})

    # 非数（web 跳过；scripts TypeError/ValueError）
    rows = [{"match_num": 3, "had": {"h": "x", "d": 3.40, "a": 4.20}, "algo": {"probs": probs}}]
    assert jc_gap(rows) == []
    with pytest.raises((ValueError, TypeError)):
        devig({"home": "x", "draw": 3.40, "away": 4.20})

    # 缺 a 是 web 额外约束，scripts len=2 是合法的（2-way devig），
    # web 跳过该场是契约差异，不算漂移
    rows = [{"match_num": 1, "had": {"h": 1.85, "d": 3.40}, "algo": {"probs": probs}}]
    assert jc_gap(rows) == []
    # scripts 不报错，返 2-way
    s = devig({"home": 1.85, "draw": 3.40})
    assert set(s) == {"home", "draw"}
    assert abs(sum(s.values()) - 1.0) < 1e-9
