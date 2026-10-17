"""R47-2: web/services/team_match.py::norm_name + _canon 4 cases direct + D-74 lock.

norm_name (L28-L35) + _canon (L55-L61) 是 team_match 模块的规范化层:
- norm_name: 去空白 + 转小写, fuzzy match 上游
- _canon: 主体 + 别名 (中文别名表 _ALIASES) 合并为可匹配名集

R19 #G1 / R44-2 D-57 / R45-1 D-62 已锁 teams_match_safe / teams_match /
_assemble_legs 行为, 但 norm_name / _canon 自身核心契约 (空白折叠 + 别名扩展
+ 单元素兜底) 0 直测。

D-74 锁定 4 契约:
- D-74-1: norm_name 主流程 - 去空白 + lowercase ("  Arsenal  " -> "arsenal")
- D-74-2: norm_name 空/None/非字符串 -> "" (L33-34 早返)
- D-74-3: _canon 主体 + 别名 - "曼彻斯特联" -> {曼联, 曼城, 曼联1} 扩展
- D-74-4: _canon 未知队名 - 单元素 set {norm_name(s)}

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
        "team_match_norm_canon_test",
        REPO_ROOT / "web" / "services" / "team_match.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-74 四契约 ────────────────────────


def test_norm_name_d74_1_strips_whitespace_and_lowercases(tm_mod):
    """R47-2 D-74-1: norm_name 主流程 - 去空白 + lowercase。

    L35 `_WS.sub('', str(s)).strip().lower()`: 多个空白合并为无, strip,
    转小写。"  Arsenal  " -> "arsenal"; "  曼 彻 斯 特 联" -> "曼彻斯特联"。
    """
    assert tm_mod.norm_name("Arsenal") == "arsenal"
    assert tm_mod.norm_name("  Arsenal  ") == "arsenal"
    assert tm_mod.norm_name("ARSENAL") == "arsenal"
    assert tm_mod.norm_name("\n\tArsenal \n") == "arsenal"
    # 中文 (空白折叠无影响)
    assert tm_mod.norm_name("  曼彻斯特联  ") == "曼彻斯特联"
    # 多空白合并
    assert tm_mod.norm_name("Ar  se  nal") == "arsenal"


def test_norm_name_d74_2_empty_or_none_returns_empty_string(tm_mod):
    """R47-2 D-74-2: 空/None/非字符串 -> "" (L33-34 早返)。

    L33 `if not s: return ""` 捕获空串/None/0/[]/{}。
    """
    assert tm_mod.norm_name("") == ""
    assert tm_mod.norm_name(None) == ""
    assert tm_mod.norm_name(0) == ""  # falsy
    assert tm_mod.norm_name([]) == ""  # falsy


def test_canon_d74_3_expands_chinese_aliases(tm_mod):
    """R47-2 D-74-3: _canon 主体 + 别名 - "曼彻斯特联" 扩展为 4 元素 set。

    L57-L61: out = {norm_name(s)} 然后遍历 _ALIASES, 匹配 key 加 targets。
    _ALIASES 键是"盘口侧名" -> ("预测侧名", ...)
    _ALIASES["曼彻斯特联"] = ("曼联", "曼城", "曼联1") -> 4 元素
    _ALIASES["阿仙奴"] = ("阿森纳",) -> _canon("阿仙奴") = {"阿仙奴", "阿森纳"}
    """
    # 主体 norm + 3 别名
    canon = tm_mod._canon("曼彻斯特联")
    assert canon == {"曼彻斯特联", "曼联", "曼城", "曼联1"}

    # 反向: _canon("阿仙奴") 包含 "阿森纳" (因为 _ALIASES["阿仙奴"] = ("阿森纳",))
    canon_arsenal = tm_mod._canon("阿仙奴")
    assert canon_arsenal == {"阿仙奴", "阿森纳"}

    # 反向: _canon("阿森纳") 不含 "阿仙奴" (因为 _ALIASES 键是 "阿仙奴" 不是 "阿森纳")
    canon_sa = tm_mod._canon("阿森纳")
    assert canon_sa == {"阿森纳"}  # 单元素, 无别名

    # 验证英文 (无中文别名匹配) -> 单元素
    canon_en = tm_mod._canon("Arsenal")
    assert canon_en == {"arsenal"}


def test_canon_d74_4_unknown_team_returns_singleton_set(tm_mod):
    """R47-2 D-74-4: _canon 未知队名 - 单元素 set {norm_name(s)}。

    无 _ALIASES 匹配 -> 只含本体 norm。
    """
    assert tm_mod._canon("UnknownTeam") == {"unknownteam"}
    assert tm_mod._canon("陌生队") == {"陌生队"}
    # empty/None 也走 norm_name 早返 -> 空字符串 set
    assert tm_mod._canon("") == {""}
    assert tm_mod._canon(None) == {""}