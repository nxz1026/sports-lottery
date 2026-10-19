"""R47-7: web/services/team_match.py::poisson_hhad 4 cases direct + D-80 lock.

poisson_hhad (L165-L194, 30 行) 让球胜平负 (hhad) 一等预测: λh/λa 独立
泊松在官方让球线下重算三向概率。官方 goalLine 施加于主队 (负=主让球,
正=主受让); 调整后净胜 = 主进球 + line − 客进球; >0 让胜, ==0 让平, <0 让负。

R44-5 D-60 + R47-6 D-79 已锁 accuracy / jc_gap, 但 poisson_hhad 自身核心
契约 (独立泊松 + 让球线调整 + 净胜判定 + 非正 λ 兜底) 0 直测。

D-80 锁定 4 契约:
- D-80-1: 主流程 happy - 强弱 + 让球 (lh=1.5, la=1.0, line=0.5) -> 让胜主导
- D-80-2: line=0 边界 - 退化为非让球, 净胜=主进球-客进球 (max(probs) 仍合理)
- D-80-3: λ 非正 (0 / 负数) -> None (L177-178 兜底)
- D-80-4: 输入非数字 (None / "abc" / 列表) -> None (L171-176 TypeError/ValueError)

依赖: 无, 纯函数 (math.exp + math.factorial stdlib)。
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
        "team_match_poisson_hhad_test",
        REPO_ROOT / "web" / "services" / "team_match.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-80 四契约 ────────────────────────


def test_poisson_hhad_d80_1_strong_team_with_line_dominates_win(tm_mod):
    """R47-7 D-80-1: 主流程 happy - 强主 + 让球 0.5 -> 让胜主导。

    lh=1.5, la=1.0, line=0.5 (主让 0.5 球, 净胜=主+0.5-客 > 0 让胜)。
    probs['让胜'] 应 > probs['让负'] (主强 + 让球)。
    """
    out = tm_mod.poisson_hhad(1.5, 1.0, 0.5)
    assert out is not None
    assert out["line"] == "0.5"
    # 强主 + 让球 -> 让胜主导
    assert out["pick"] == "让胜"
    assert out["probs"]["让胜"] > out["probs"]["让负"]
    # 三向概率和应该接近 1 (round 4 位累加误差 < 0.001)
    s = sum(out["probs"].values())
    assert abs(s - 1.0) < 0.005
    # probs 4 位小数
    for v in out["probs"].values():
        assert isinstance(v, float)


def test_poisson_hhad_d80_2_zero_line_degrades_to_no_handicap(tm_mod):
    """R47-7 D-80-2: line=0 边界 - 退化为非让球 (净胜=主进球-客进球)。

    lh=1.5, la=1.0, line=0 -> 净胜=主-客, 主强 (λ 1.5 > 1.0) -> 让胜仍主导
    (逻辑上让平=平, 让胜=主胜, 让负=客胜, 因为 line=0 时净胜判定退化为
    主胜/平/客胜 通用逻辑)。
    """
    out = tm_mod.poisson_hhad(1.5, 1.0, 0)
    assert out is not None
    assert out["line"] == "0"
    # 主强 line=0 -> 主胜=让胜主导
    assert out["pick"] == "让胜"
    # probs 和 ≈ 1
    assert abs(sum(out["probs"].values()) - 1.0) < 0.005


def test_poisson_hhad_d80_3_non_positive_lambda_returns_none(tm_mod):
    """R47-7 D-80-3: λ 非正 (0 / 负数) -> None (L177-178 兜底)。

    lh=0 或 la=0 或 lh<0 -> 不编数返 None。
    """
    # lh=0
    assert tm_mod.poisson_hhad(0, 1.0, 0.5) is None
    # la=0
    assert tm_mod.poisson_hhad(1.5, 0, 0.5) is None
    # lh 负数
    assert tm_mod.poisson_hhad(-1.0, 1.0, 0.5) is None
    # la 负数
    assert tm_mod.poisson_hhad(1.5, -1.0, 0.5) is None
    # line 不影响 λ 判定 (line 0.5 合法)
    assert tm_mod.poisson_hhad(1.5, 1.0, -0.5) is not None  # line 负数也 OK


def test_poisson_hhad_d80_4_non_numeric_returns_none(tm_mod):
    """R47-7 D-80-4: 输入非数字 (None / "abc" / 列表) -> None。

    L171-176 try/except (TypeError, ValueError) 兜底。
    """
    # 字符串非数字
    assert tm_mod.poisson_hhad("abc", 1.0, 0.5) is None
    assert tm_mod.poisson_hhad(1.5, "abc", 0.5) is None
    assert tm_mod.poisson_hhad(1.5, 1.0, "abc") is None
    # None
    assert tm_mod.poisson_hhad(None, 1.0, 0.5) is None
    # 列表 (TypeError)
    assert tm_mod.poisson_hhad([1.5], 1.0, 0.5) is None
    # 字典 (TypeError)
    assert tm_mod.poisson_hhad({"v": 1.5}, 1.0, 0.5) is None
