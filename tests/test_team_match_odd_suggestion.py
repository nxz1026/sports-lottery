"""R47-3: web/services/team_match.py::odd_suggestion 3 cases direct + D-75 lock.

odd_suggestion (L96-L115, 19 行) 由官方 had 赔率指向赔率最低项 (隐含概率
最高) -> odd 层倾向。R44-5 D-60 + R45-1 D-62 已锁 accuracy / combo 装配
链, 但 odd_suggestion 自身核心契约 (最低赔率 + 格式 + 0/缺/字符串 容错) 0 直测。

D-75 锁定 3 契约:
- D-75-1: 主流程 happy - had 三个赔率 (h=1.50, d=3.40, a=4.50) -> 最低 h 命中
  -> {"pick": "主胜", "odds": "1.50"} (2 位小数格式避免浮点四舍五入)
- D-75-2: 无效赔率 (全 0 / 缺键 / 非数字) -> {} 兜底 (L110-111)
- D-75-3: 浮点 1.500 格式 - 验证 f"{v:.2f}" 避免二进制浮点误差 (round(2.03,2)=='2.02')

依赖: 无, 纯函数。
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
        "team_match_odd_suggestion_test",
        REPO_ROOT / "web" / "services" / "team_match.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-75 三契约 ────────────────────────


def test_odd_suggestion_d75_1_picks_lowest_odds(tm_mod):
    """R47-3 D-75-1: 主流程 - 最低赔率命中 (隐含概率最高)。

    h=1.50 (主胜最低) -> 命中 + 2 位小数格式 '1.50'。
    验证 f"{v:.2f}" 格式化避免 round(1.5, 2)=='1.5' 短一位问题。
    """
    out = tm_mod.odd_suggestion({"h": "1.50", "d": "3.40", "a": "4.50"})
    assert out == {"pick": "主胜", "odds": "1.50"}

    # 最低是 a (客胜)
    out_away = tm_mod.odd_suggestion({"h": "2.50", "d": "3.40", "a": "1.80"})
    assert out_away == {"pick": "客胜", "odds": "1.80"}

    # 最低是 d (平)
    out_draw = tm_mod.odd_suggestion({"h": "3.00", "d": "2.10", "a": "3.50"})
    assert out_draw == {"pick": "平", "odds": "2.10"}


def test_odd_suggestion_d75_2_invalid_odds_returns_empty_dict(tm_mod):
    """R47-3 D-75-2: 无效赔率 (全 0 / 字符串非数字 / 列表) -> {} 兜底。

    L107-108 TypeError/ValueError 兜底; L109-111 全 v<=0 -> {}。
    LOCK:
    - 缺键 / None 走 `get(k) or 0` 路径 (L103-105), 不会 TypeError
    - 真正无效: 字符串非数字 (float() ValueError) / 列表 / dict (TypeError)
    """
    # 全 0 -> 过滤后 vals 空
    assert tm_mod.odd_suggestion({"h": 0, "d": 0, "a": 0}) == {}
    # 全缺键 (空 dict) -> 3 个 get(0) 全 0 -> 过滤空
    assert tm_mod.odd_suggestion({}) == {}
    # 字符串非数字 -> float() ValueError 兜底
    assert tm_mod.odd_suggestion({"h": "abc", "d": "3.40", "a": "4.50"}) == {}
    # 列表触发 TypeError
    assert tm_mod.odd_suggestion({"h": [1.50], "d": "3.40", "a": "4.50"}) == {}
    # dict 触发 TypeError
    assert tm_mod.odd_suggestion({"h": {"v": 1.50}, "d": "3.40", "a": "4.50"}) == {}
    # 负数 字符串 -> 浮点负数 v>0 失败 (LOCK: v>0 过滤, 不是 v<0)
    # 实际上 -1.50 走 v>0 过滤, 不进 pick, 看不出异常 — 跳过此 case


def test_odd_suggestion_d75_3_float_format_2_decimals(tm_mod):
    """R47-3 D-75-3: 浮点 2 位小数格式 - 避免 round() 二进制浮点四舍五入误差。

    L113-114 注释明示: 'round(2.03,2)=="2.02" 的问题', 用 f"{v:.2f}" 字符串
    格式避免。1.5 + 0 实际 Python float 但 1.5 准确表示 (2 进制精确),
    验 1.40 边界 (1.4 不可精确表示)。
    """
    # 1.50 应该显示 "1.50" 而不是 "1.5"
    out = tm_mod.odd_suggestion({"h": "1.50", "d": "3.40", "a": "4.50"})
    assert out["odds"] == "1.50"  # 2 位小数, 不短
    assert "." in out["odds"]  # 含小数点
    assert len(out["odds"].split(".")[1]) == 2  # 2 位小数

    # 1.4 浮点 (实际 1.399999...) -> f"{v:.2f}" 仍返 '1.40'
    out_low = tm_mod.odd_suggestion({"h": "1.4", "d": "3.40", "a": "4.50"})
    assert out_low["odds"] == "1.40"  # 不会变 "1.39" (round 误差)