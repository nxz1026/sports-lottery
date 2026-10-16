"""R35-3: scripts/core/model/monte_carlo_worldcup.py::_build_r16_matchups 4 测直测。

现状：8 组配对表（A1vB2..H1vG2）有固定契约，0 直测。
按 seeded[first_idx*2]（组 first_idx 第1名）vs seeded[second_idx*2+1]（组 second_idx 第2名）
配对 8 组淘汰赛首轮。

本轮补 4 测：
- 标准 16 队 → 严格按 8 对标准对阵（A1vB2, C1vD2, E1vF2, G1vH2,
  B1vA2, D1vC2, F1vE2, H1vG2）
- 自定义队伍名 → 索引映射验证（4 个 slot 抽查）
- 长度 < 16 → IndexError（缺数据兜底缺失，锁现状）
- 8-tuple shape + 元素类型锁定（每对 tuple[str, str]）
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def mc_wc_mod():
    spec = importlib.util.spec_from_file_location(
        "mc_wc_r16_test",
        REPO_ROOT / "scripts" / "core" / "model" / "monte_carlo_worldcup.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _build_r16_matchups ────────────────────────


def test_build_r16_matchups_standard_8_pairings(mc_wc_mod):
    """R35-3：标准 16 队 → 严格按 8 对标准对阵表。

    A1vB2, C1vD2, E1vF2, G1vH2, B1vA2, D1vC2, F1vE2, H1vG2。
    """
    seeded = [f"{grp}{i}" for grp in "ABCDEFGH" for i in [1, 2]]
    assert len(seeded) == 16
    out = mc_wc_mod._build_r16_matchups(seeded)
    assert out == [
        ("A1", "B2"),
        ("C1", "D2"),
        ("E1", "F2"),
        ("G1", "H2"),
        ("B1", "A2"),
        ("D1", "C2"),
        ("F1", "E2"),
        ("H1", "G2"),
    ]


def test_build_r16_matchups_index_mapping_with_custom_names(mc_wc_mod):
    """R35-3：自定义队伍名 → 索引映射验证（4 个 slot 抽查）。

    seeded = [A1, A2, B1, B2, ...] 索引 0,1,2,3,...
    seeded[first_idx*2] 取组 first_idx 第 1 名；seeded[second_idx*2+1] 取组 second_idx 第 2 名。
    """
    seeded = [
        "阿尔德", "某甲",   # 组 A: idx 0, 1
        "伯德",  "某乙",   # 组 B: idx 2, 3
        "塞德",  "某丙",   # 组 C: idx 4, 5
        "德德",  "某丁",   # 组 D: idx 6, 7
        "伊德",  "某戊",   # 组 E: idx 8, 9
        "埃夫德","某己",   # 组 F: idx 10, 11
        "吉德",  "某庚",   # 组 G: idx 12, 13
        "艾尺",  "某辛",   # 组 H: idx 14, 15
    ]
    out = mc_wc_mod._build_r16_matchups(seeded)
    # 0: (0,1) → A1vB2 = seeded[0] vs seeded[1*2+1=3] = "阿尔德" vs "某乙"
    assert out[0] == ("阿尔德", "某乙")
    # 1: (2,3) → C1vD2 = seeded[2*2=4] vs seeded[3*2+1=7] = "塞德" vs "某丁"
    assert out[1] == ("塞德", "某丁")
    # 4: (1,0) → B1vA2 = seeded[1*2=2] vs seeded[0*2+1=1] = "伯德" vs "某甲"
    assert out[4] == ("伯德", "某甲")
    # 7: (7,6) → H1vG2 = seeded[7*2=14] vs seeded[6*2+1=13] = "艾尺" vs "某庚"
    assert out[7] == ("艾尺", "某庚")


def test_build_r16_matchups_too_short_raises_index_error(mc_wc_mod):
    """R35-3：seeded 长度 < 16 → IndexError（缺数据兜底缺失，锁现状）。

    上游 _assemble_knockout 装配时按 16 队输出，少于 16 视为数据错误。
    当前函数无 InputValidation，未来切换为长度校验需重测。
    """
    with pytest.raises(IndexError):
        mc_wc_mod._build_r16_matchups(["A1", "A2", "B1"])  # 仅 3 队


def test_build_r16_matchups_return_shape_8tuple_locked(mc_wc_mod):
    """R35-3：返回 shape 锁定 — 8 个 tuple[str, str]，每对长度 2 元素。"""
    seeded = [f"{grp}{i}" for grp in "ABCDEFGH" for i in [1, 2]]
    out = mc_wc_mod._build_r16_matchups(seeded)
    assert isinstance(out, list)
    assert len(out) == 8
    for pair in out:
        assert isinstance(pair, tuple)
        assert len(pair) == 2
        assert isinstance(pair[0], str) and isinstance(pair[1], str)