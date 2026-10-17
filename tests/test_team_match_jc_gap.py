"""R47-6: web/services/team_match.py::jc_gap 5 cases direct + D-79 lock.

jc_gap (L201-L240, 40 行) 官方 SP 隐含概率 vs 模型胜平负概率的错位榜。
R44-5 D-60 + R47-4 D-76/D-77 已锁 accuracy / algo_suggestion / jc_gap_note,
但 jc_gap 自身核心契约 (TV/2 gap 计算 + 跳过规则 + 排序 + top_n 截断) 0 直测。

D-79 锁定 5 契约:
- D-79-1: 主流程 happy - 3 rows, gap 不同 -> 按 gap 降序 + 完整字段 (match_num/
  league_cn/home/away/official/model/gap/worst/note)
- D-79-2: 跳过规则 - had 缺 / 缺 h/d/a 键 / odds<=1 / probs 空 / TypeError
- D-79-3: TV/2 gap 计算 (sum(|oi - probs|) / 2, 严格数学)
- D-79-4: top_n 截断 (gap 降序后 [:top_n])
- D-79-5: 全空 rows -> 返 [] (L209-215 全跳过)

依赖: 无, 纯函数 (调 jc_gap_note 真跑, D-77 已锁)。
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
        "team_match_jc_gap_test",
        REPO_ROOT / "web" / "services" / "team_match.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _row(*, match_num=1, home="A", away="B", league="epl",
         h=2.0, d=3.0, a=4.0,
         probs=None) -> dict:
    """构造 jc_gap 输入 row。"""
    had = {}
    if h is not None:
        had["h"] = h
    if d is not None:
        had["d"] = d
    if a is not None:
        had["a"] = a
    r = {
        "match_num": match_num,
        "league_cn": league,
        "home_cn": home,
        "away_cn": away,
        "had": had,
    }
    if probs is not None:
        r["algo"] = {"probs": probs}
    return r


# ──────────────────────── D-79 五契约 ────────────────────────


def test_jc_gap_d79_1_happy_path_sort_by_gap_desc(tm_mod):
    """R47-6 D-79-1: 主流程 happy - 3 rows 按 gap 降序 + 完整字段。

    row1 (h=2,d=3,a=4) oi≈(0.4615,0.3077,0.2308) probs=(0.5,0.3,0.2) gap≈0.038
    row2 (h=3,d=3,a=3) oi=(0.333,0.333,0.333) probs=(0.5,0.3,0.2) gap≈0.167
    row3 (h=2.5,d=3,a=3.5) oi≈(0.393,0.327,0.281) probs=(0.45,0.35,0.20) gap≈0.081
    按 gap 降序: row2 (0.167) > row3 (0.081) > row1 (0.038) = [2, 3, 1]
    """
    rows = [
        _row(match_num=1, h=2.0, d=3.0, a=4.0,
             probs={"主胜": 0.5, "平": 0.3, "客胜": 0.2}),
        _row(match_num=2, h=3.0, d=3.0, a=3.0,
             probs={"主胜": 0.5, "平": 0.3, "客胜": 0.2}),
        _row(match_num=3, h=2.5, d=3.0, a=3.5,
             probs={"主胜": 0.45, "平": 0.35, "客胜": 0.20}),
    ]
    out = tm_mod.jc_gap(rows)
    assert len(out) == 3
    # 验证降序: row2(0.167) > row3(0.081) > row1(0.038)
    assert [it["match_num"] for it in out] == [2, 3, 1]
    # 完整字段 (取第一个, match_num=2)
    first = out[0]
    assert first["league_cn"] == "epl"
    assert first["home"] == "A"
    assert first["away"] == "B"
    assert set(first["official"].keys()) == {"主胜", "平", "客胜"}
    assert first["model"] == {"主胜": 0.5, "平": 0.3, "客胜": 0.2}
    # note 是 jc_gap_note 真跑结果 (D-77 已锁)
    assert first["worst"] in {"主胜", "平", "客胜"}
    assert isinstance(first["gap"], float)


def test_jc_gap_d79_2_skip_rules(tm_mod):
    """R47-6 D-79-2: 跳过规则 - had 缺 / 缺键 / odds<=1 / probs 空 / TypeError。

    5 个异常 row + 1 个正常 row -> 只 1 个入选。
    """
    rows = [
        # 1. 正常入选
        _row(match_num=1, h=2.0, d=3.0, a=4.0,
             probs={"主胜": 0.5, "平": 0.3, "客胜": 0.2}),
        # 2. had 完全缺 (L211 r.get('had') or {} -> {} -> 缺 h 键 -> KeyError)
        {"match_num": 2, "algo": {"probs": {"主胜": 0.5, "平": 0.3, "客胜": 0.2}}},
        # 3. 缺 h 键
        _row(match_num=3, h=None, d=3.0, a=4.0,
             probs={"主胜": 0.5, "平": 0.3, "客胜": 0.2}),
        # 4. odds <= 1 (竞彩规则, h=1.0 不算)
        _row(match_num=4, h=1.0, d=3.0, a=4.0,
             probs={"主胜": 0.5, "平": 0.3, "客胜": 0.2}),
        # 5. probs 空 (L213-215 if not probs: continue)
        _row(match_num=5, h=2.0, d=3.0, a=4.0, probs=None),
        # 6. had 字符串非数字 (TypeError)
        _row(match_num=6, h="abc", d=3.0, a=4.0,
             probs={"主胜": 0.5, "平": 0.3, "客胜": 0.2}),
    ]
    out = tm_mod.jc_gap(rows)
    # 仅 row 1 入选
    assert [it["match_num"] for it in out] == [1]


def test_jc_gap_d79_3_tv_div_2_calculation(tm_mod):
    """R47-6 D-79-3: TV/2 gap 计算 - 严格数学 (sum(|oi - probs|) / 2)。

    h=2,d=3,a=4 -> 1/2+1/3+1/4=0.5+0.333+0.25=1.0833
    oi = (0.5/1.0833, 0.333/1.0833, 0.25/1.0833) ≈ (0.4615, 0.3077, 0.2308)
    probs = (0.5, 0.3, 0.2) (与 oi 接近)
    gap = (|0.4615-0.5| + |0.3077-0.3| + |0.2308-0.2|) / 2
        = (0.0385 + 0.0077 + 0.0308) / 2 ≈ 0.0385
    """
    rows = [_row(match_num=1, h=2.0, d=3.0, a=4.0,
                 probs={"主胜": 0.5, "平": 0.3, "客胜": 0.2})]
    out = tm_mod.jc_gap(rows)
    assert len(out) == 1
    # 验证 gap 在 0.04 附近 (TV/2)
    gap = out[0]["gap"]
    assert 0.03 < gap < 0.05
    # 验证 oi 归一化 (sum 接近 1, 容差 0.005 含 round 误差)
    oi = out[0]["official"]
    assert abs(sum(oi.values()) - 1.0) < 0.005


def test_jc_gap_d79_4_top_n_truncation(tm_mod):
    """R47-6 D-79-4: top_n 截断 - 5 rows 排序后取前 2。

    5 rows + top_n=2 -> 返 2 条 (gap 最大的)。
    """
    rows = [
        _row(match_num=i, h=2.0 + i * 0.1, d=3.0, a=4.0,
             probs={"主胜": 0.4, "平": 0.3, "客胜": 0.3})
        for i in range(1, 6)
    ]
    out_full = tm_mod.jc_gap(rows)  # 5 条
    assert len(out_full) == 5
    out_top2 = tm_mod.jc_gap(rows, top_n=2)  # 2 条
    assert len(out_top2) == 2
    # top2 应该是 full 的前 2 (gap 降序)
    assert out_top2 == out_full[:2]


def test_jc_gap_d79_5_empty_or_all_invalid_returns_empty(tm_mod):
    """R47-6 D-79-5: 全空 rows 或全无效 -> 返 [] (L209-215 全跳过)。

    空 list + 全无 probs 两种。
    """
    # 完全空 list
    assert tm_mod.jc_gap([]) == []
    # 3 rows 全无 algo/probs
    rows = [
        _row(match_num=1, h=2.0, d=3.0, a=4.0, probs=None),
        _row(match_num=2, h=2.0, d=3.0, a=4.0, probs=None),
    ]
    assert tm_mod.jc_gap(rows) == []
    # top_n 在空结果下仍返 []
    assert tm_mod.jc_gap([], top_n=5) == []