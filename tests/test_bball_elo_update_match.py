"""R39-3: scripts/bball/elo_bball.py::_update_match 5 tests direct + D-31 sum conservation.

篮球 ELO 单元更新 0 direct tests; update_ratings 包装被测但底层函数未锁。
_update_match 是 ELO 系统的核心算子 (类似 football ELO 公式), 有强对称性契约:
- 守恒: ratings[home] + ratings[away] 增减之和恒为 0 (主客变更对称)
- HOME_ADV 隔离: HOME_ADV 加在 expected 计算的 rh 上, 不污染 ratings[home]
  持久化值 (避免 home 字段随场比赛叠加虚拟分)
- 期望胜率: ELO 标准期望公式, 配 SCALE/K 在 config_bball 锁定

D-31 (sum conservation lock):
- 守恒是 zero-sum invariant; 若有人改 away 公式方向 (e.g., k * (expected - actual))
  误写成 k * (actual - expected), 守恒即破, 全联赛 ELO 系统会 drift。
- 锁定 away delta == - home delta 严格测试。

5 tests:
- D-31 sum conservation: ratings[home] + ratings[away] 增量 = 0
- D-31 HOME_ADV isolation: ratings[home] 不含 HOME_ADV (距初始值 delta 不偏置 100)
- home_win=True: home +delta, away -delta, 镜像对称
- home_win=False: home -delta, away +delta, 镜像对称
- Unseeded team: ratings.get(home, INITIAL) fallback 不爆 KeyError
"""
from __future__ import annotations

import importlib.util
import math
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ── 预先注册 bball 包 + config_bball 子模块 ──
_bball_pkg = types.ModuleType("bball")
_bball_pkg.__path__ = [str(REPO_ROOT / "scripts" / "bball")]
sys.modules.setdefault("bball", _bball_pkg)
_config_bball = types.ModuleType("bball.config_bball")
_config_bball.ELO = {
    "INITIAL": 1500.0, "SCALE": 400, "K": 20, "HOME_ADV": 100,
}
_config_bball.TEAM_CN = {"Atlanta Hawks": "ATL", "Boston Celtics": "BOS"}
_config_bball.ELO_FILE = Path("unused")
_config_bball.TEAMS_SEED_FILE = Path("unused")
sys.modules.setdefault("bball.config_bball", _config_bball)


@pytest.fixture
def bball_elo_mod():
    """加载 bball.elo_bball 模块; 复用 R37-5 模式 (spec 让 bball 成为父包)。"""
    spec = importlib.util.spec_from_file_location(
        "bball.elo_bball",
        REPO_ROOT / "scripts" / "bball" / "elo_bball.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-31 invariants ────────────────────────


def _expected(rh: float, ra: float, scale: int = 400) -> float:
    """ELO 期望胜率公式 (与 src 的 expectation 一致; 锁定计算路径)。"""
    return 1 / (1 + 10 ** ((ra - rh) / scale))


def test_update_match_d31_sum_conservation(bball_elo_mod):
    """R39-3 D-31: 守恒 — ratings[home] + ratings[away] 总和更新后不变。

    若 away 公式方向写反 (e.g., k*(expected - actual) 误写成 k*(actual - expected)),
    守恒即破, 全联赛 ELO 系统会 drift。本测锁定 home delta == -away delta。
    """
    ELO = bball_elo_mod.ELO
    ratings = {"Lakers": 1500.0, "Celtics": 1500.0}
    pre_total = sum(ratings.values())
    pre_home, pre_away = ratings["Lakers"], ratings["Celtics"]

    bball_elo_mod._update_match(ratings, "Lakers", "Celtics", home_win=True)

    # 主客增量镜像对称
    home_delta = ratings["Lakers"] - pre_home
    away_delta = ratings["Celtics"] - pre_away
    assert home_delta == pytest.approx(-away_delta, rel=1e-9)
    # 总和不变 (精确等, 非 approx)
    assert ratings["Lakers"] + ratings["Celtics"] == pytest.approx(pre_total)
    # K 倍 (actual-expected) 与 (expected-actual) 严格相反
    k = ELO["K"]
    expected = _expected(ratings["Lakers"] - pre_home + 1500 + ELO["HOME_ADV"],
                         1500, ELO["SCALE"])
    # 注: 上面 expected 用的是更新后的 rh, 实际 src 中 rh 是用 update *前* 的 ratings
    # 但因为 rh 只用于 expected 计算 (不写入 ratings), home_delta 与 pre_home 无关
    # 重新推导 (更清晰的方式):
    pre_rh = pre_home + ELO["HOME_ADV"]  # = 1600
    pre_ra = pre_away                    # = 1500
    src_expected = 1 / (1 + 10 ** ((pre_ra - pre_rh) / ELO["SCALE"]))
    assert home_delta == pytest.approx(k * (1.0 - src_expected))
    assert away_delta == pytest.approx(k * (src_expected - 1.0))


def test_update_match_d31_home_adv_isolation(bball_elo_mod):
    """R39-3 D-31: HOME_ADV 不污染 ratings[home] 持久化字段。

    HOME_ADV 仅用于 expected 计算 (提升 rh), 写入 ratings[home] 时只用
    ratings.get(home, INITIAL) 不叠加 HOME_ADV。验证方法: home delta 应
    仅由 k*(actual-expected) 决定, 增量幅值 <= k (即 |delta| <= 20)。
    """
    ELO = bball_elo_mod.ELO
    ratings = {"Lakers": 1500.0, "Celtics": 1500.0}
    pre_home = ratings["Lakers"]

    bball_elo_mod._update_match(ratings, "Lakers", "Celtics", home_win=True)

    delta = ratings["Lakers"] - pre_home
    # 增量幅值严格 <= K (因 (actual - expected) ∈ [-1, 1])
    assert abs(delta) <= ELO["K"]
    # 不应加成 HOME_ADV (即不应该 delta == K*(actual-expected) + HOME_ADV)
    pre_rh = pre_home + ELO["HOME_ADV"]
    pre_ra = 1500.0
    expected = 1 / (1 + 10 ** ((pre_ra - pre_rh) / ELO["SCALE"]))
    assert delta == pytest.approx(ELO["K"] * (1.0 - expected))


def test_update_match_home_win_true_increases_home(bball_elo_mod):
    """R39-3: home_win=True 主场期望高 -> 实际胜 -> home +, away -。

    对称队 (1500, 1500) 配 HOME_ADV=100, home expected > 0.5; 实际胜,
    实际超出期望的部分为正 -> home delta > 0。
    """
    ratings = {"Lakers": 1500.0, "Celtics": 1500.0}
    bball_elo_mod._update_match(ratings, "Lakers", "Celtics", home_win=True)

    assert ratings["Lakers"] > 1500.0
    assert ratings["Celtics"] < 1500.0


def test_update_match_home_win_false_decreases_home(bball_elo_mod):
    """R39-3: home_win=False 主场期望高 -> 实际负 -> 实际低于期望 -> home -。

    镜像对称: home_loss 等价于 home_win 反转, home -, away +。
    """
    ratings = {"Lakers": 1500.0, "Celtics": 1500.0}
    bball_elo_mod._update_match(ratings, "Lakers", "Celtics", home_win=False)

    assert ratings["Lakers"] < 1500.0
    assert ratings["Celtics"] > 1500.0


def test_update_match_unseeded_team_uses_initial(bball_elo_mod):
    """R39-3: 新队 (unseeded, ratings 缺该队) -> ratings.get(., INITIAL) 不爆 KeyError。

    fallback INITIAL=1500.0 走完完整公式 (含 HOME_ADV 加在 expected 计算侧)。
    """
    ELO = bball_elo_mod.ELO
    # 只放 Celtics, 不放新队 "Expansion"
    ratings = {"Celtics": 1500.0}

    bball_elo_mod._update_match(ratings, "Expansion", "Celtics", home_win=True)

    # 新队写入 INITIAL 处 (1500), 按 home_win=True 推导 delta
    pre_rh = ELO["INITIAL"] + ELO["HOME_ADV"]
    pre_ra = 1500.0
    expected = 1 / (1 + 10 ** ((pre_ra - pre_rh) / ELO["SCALE"]))
    expected_delta = ELO["K"] * (1.0 - expected)
    assert ratings["Expansion"] == pytest.approx(ELO["INITIAL"] + expected_delta)
    assert "Celtics" in ratings
    # Celtics 仍存在 (fallback 也是 INITIAL 起步, 然后扣 away delta)
    assert ratings["Celtics"] < ELO["INITIAL"]
