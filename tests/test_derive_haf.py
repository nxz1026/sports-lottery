"""P2-12 scripts/derive/haf 直接单测（迁移 web/services/haf.py 后）。

测试覆盖：

1. 9 宫格 Σ == 1（全场 + 上半场联合遍历 + renormalize）
2. 强主队 + 主让球 line=-1 → "胜胜" 占优（主队全场+上半场双胜）
3. 弱主队 + line=0 → 9 宫格分布合理，无负概率
4. 输入非法（None / 0 / "x" / 负数 λ）→ 返回 None
5. line=0 与 line=None 等价
6. line 范围 -10..+10 任意整数都接受（非整数 line 也容忍，浮点也行）
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def test_probs_sums_to_one():
    """9 宫格 probs 之和 = 1（容差 1e-3，对应 round(_, 4) 量化）。"""
    from derive.haf import probs

    out = probs(1.5, 1.2)
    assert out is not None
    assert set(out["probs"]) == set([
        "胜胜", "胜平", "胜负", "平胜", "平平", "平负", "负胜", "负平", "负负",
    ])
    assert abs(sum(out["probs"].values()) - 1.0) < 1e-3


def test_probs_strong_home_with_minus_one_favors_win_win():
    """λh=2.2, λa=0.8, line=-1（主让 1 球）：主胜全场+上半场双胜占优。"""
    from derive.haf import probs

    out = probs(2.2, 0.8, line=-1)
    assert out is not None
    # "胜胜"（主上半场胜 + 主全场让球后胜）应高于 "负负"（主上半场负 + 主全场让球后负）
    assert out["probs"]["胜胜"] > out["probs"]["负负"]
    assert out["pick"] in {"胜胜", "胜平", "平胜"}  # 三种"主队至少半场不输"


def test_probs_zero_line_default_behaves_like_no_line():
    """line=0 与省略 line 应产同一结果。"""
    from derive.haf import probs

    out_zero = probs(1.5, 1.2, line=0)
    out_default = probs(1.5, 1.2)
    assert out_zero == out_default


def test_probs_no_negative_values_and_pick_is_max():
    """所有概率 ≥ 0；pick = argmax of probs。"""
    from derive.haf import probs

    out = probs(1.3, 1.3)
    assert out is not None
    for v in out["probs"].values():
        assert v >= 0
        assert math.isfinite(v)
    assert out["pick"] == max(out["probs"], key=lambda k: out["probs"][k])


def test_probs_invalid_lambda_returns_none():
    """λ 非法 → None，不编数。"""
    from derive.haf import probs

    assert probs(None, 1.0) is None
    assert probs(1.0, None) is None
    assert probs(0.0, 1.0) is None
    assert probs(-1.0, 1.0) is None
    assert probs("x", 1.0) is None
    assert probs(1.0, "x") is None


def test_probs_float_line_accepted():
    """scripts/derive/haf.probs 接受 float line（与 web 端兼容）。

    注意：``str(line)`` 会保留 float 表示（"-1" vs "-1.0"），故断言逐字段比较
    probs / pick，line 字段值相等即可（用 ``float(...)`` 解析）。
    """
    from derive.haf import probs

    out_int = probs(1.5, 1.2, line=-1)
    out_float = probs(1.5, 1.2, line=-1.0)
    assert out_int["probs"] == out_float["probs"]
    assert out_int["pick"] == out_float["pick"]
    assert float(out_int["line"]) == float(out_float["line"]) == -1.0


def test_probs_max_goals_truncation_is_safe():
    """max_goals 截断（默认 10）后 Σ 应 ≈ 1；λ ≤ 3 实战范围下漏概率 < 1e-3。"""
    from derive.haf import probs

    out = probs(2.5, 2.5)
    assert out is not None
    assert abs(sum(out["probs"].values()) - 1.0) < 1e-3


def test_ht_ratio_constant_matches_spec():
    """HT_RATIO = 0.45（公开常量，文档约定）。"""
    from derive.haf import HT_RATIO

    assert HT_RATIO == pytest.approx(0.45, abs=1e-12)


def test_haf_order_has_nine_entries_in_canonical_layout():
    """HAF_ORDER 长度 = 9，顺序 = 上半场 × 全场（胜/平/负 各 3）。"""
    from derive.haf import HAF_ORDER

    assert len(HAF_ORDER) == 9
    assert HAF_ORDER == ["胜胜", "胜平", "胜负", "平胜", "平平", "平负", "负胜", "负平", "负负"]
