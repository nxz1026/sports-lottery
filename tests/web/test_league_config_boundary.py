"""P2-16 web↔scripts 联赛跨边界契约：守住"模型覆盖联赛三套表示必须一一对应"。

三处独立硬编码，必须等势：

  - ``web.config.BIG5`` + ``web.config.MODEL_LEAGUES``
                                   —— 中文全名（用于 jc_view.fixtures_on 的
                                          league_cn 维度过滤）
  - ``web.app.jc.LEAGUE_KEYS``        —— 中文全名 → 英文 league key
                                          （命中率对账 + 预测文件名）
  - ``scripts.core.leagues.LEAGUE_DC_RHO`` / ``LEAGUE_LAMBDA_MULTIPLIER`` /
    ``LEAGUE_CONFIG``              —— 英文 league key → Dixon-Coles ρ /
                                          λ multiplier / 元数据（模型核心）

任一端新加 / 删除一个联赛，三端必须**同日**同步。本测试断言：

1. ``len(BIG5) + len(MODEL_LEAGUES) == len(LEAGUE_CONFIG 足球子集)``
   （2026-10-20 起欧冠进 ``MODEL_LEAGUES`` —— 引擎正式覆盖的联赛，不再是"临时赛事"）
2. 对每个 ``LEAGUE_KEYS.items()``，值必须在 LEAGUE_DC_RHO /
   LEAGUE_LAMBDA_MULTIPLIER / LEAGUE_CONFIG 里存在
3. 反向：LEAGUE_DC_RHO 每个 key 都能被 ``LEAGUE_KEYS`` 反查回中文名
   （且该中文名 ∈ ``BIG5 ∪ MODEL_LEAGUES``）
4. ``BIG5`` 与 ``LEAGUE_CONFIG`` 集合等势（一增一减同步）
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))


_BIG5 = frozenset({
    "英格兰超级联赛", "西班牙甲级联赛", "意大利甲级联赛",
    "德国甲级联赛", "法国甲级联赛",
})
# 引擎正式覆盖、但不属于「五大联赛」的联赛（欧冠，2026-10-20 起）。
_MODEL_LEAGUES = frozenset({"欧洲冠军联赛"})
_ALL_LEAGUES = _BIG5 | _MODEL_LEAGUES
_LEAGUE_KEYS = {
    "英格兰超级联赛": "epl",
    "西班牙甲级联赛": "laliga",
    "意大利甲级联赛": "seriea",
    "德国甲级联赛": "bundesliga",
    "法国甲级联赛": "ligue1",
    "欧洲冠军联赛": "ucl",
}


def _import(name):
    if name not in sys.modules:
        importlib.import_module(name)
    return sys.modules[name]


def _football_league_keys() -> set[str]:
    """LEAGUE_CONFIG 中"足球联赛"子集（排除 nba / 其他运动）。"""
    cfg = _import("core.leagues").LEAGUE_CONFIG
    football = set()
    for k, meta in cfg.items():
        if not isinstance(meta, dict):
            continue
        if k == "nba":
            continue
        football.add(k)
    return football


def test_big5_length_matches_football_league_keys():
    """web 中文联赛名集合（BIG5 + MODEL_LEAGUES）大小 = scripts LEAGUE_CONFIG 足球子集大小。

    LEAGUE_CONFIG 含 nba（篮球，web/app/jc.LEAGUE_KEYS 不映射），过滤掉。
    加新足球联赛需同步改三处。
    """
    football = _football_league_keys()

    assert len(_BIG5) == 5
    assert len(_ALL_LEAGUES) == len(football), (
        f"web.config.BIG5+MODEL_LEAGUES (size {len(_ALL_LEAGUES)}) ≠ scripts LEAGUE_CONFIG 足球子集 "
        f"(size {len(football)}) —— 新增/删除足球联赛需同时改 web.config.BIG5 + "
        f"web.config.MODEL_LEAGUES + web.app.jc.LEAGUE_KEYS + scripts.core.leagues.LEAGUE_CONFIG"
    )


def test_big5_keys_values_are_valid_football_league_keys():
    """LEAGUE_KEYS 的每个 value 必须 ∈ scripts LEAGUE_CONFIG 足球子集。"""
    football = _football_league_keys()
    for cn, eng in _LEAGUE_KEYS.items():
        assert eng in football, (
            f"LEAGUE_KEYS['{cn}'] = '{eng}' 不在 scripts LEAGUE_CONFIG 足球子集中"
        )


def test_big5_keys_keys_are_valid_big5():
    """LEAGUE_KEYS 的每个 key 必须 ∈ BIG5 ∪ MODEL_LEAGUES（防中文联赛名拼写漂移）。"""
    for cn in _LEAGUE_KEYS:
        assert cn in _ALL_LEAGUES, (
            f"LEAGUE_KEYS key '{cn}' 不在 web.config.BIG5∪MODEL_LEAGUES 中（中文全名漂移）"
        )


def test_league_dc_rho_keys_all_have_cn_mapping():
    """scripts.LEAGUE_DC_RHO 每个 key 都能反查回中文全名。

    反查失败 ⇒ 模型在算该联赛 DC ρ 时 web 命中率对账走 LEAGUE_KEYS.values()
    跳过该联赛，命中率面板永远 0% 且无报错（静默事故）。
    """
    rho = _import("core.leagues").LEAGUE_DC_RHO
    rev = {eng: cn for cn, eng in _LEAGUE_KEYS.items()}
    for eng in rho:
        assert eng in rev, (
            f"scripts.LEAGUE_DC_RHO key '{eng}' 缺 LEAGUE_KEYS 反查（web "
            f"命中率对账静默跳过该联赛）"
        )
        assert rev[eng] in _ALL_LEAGUES


def test_league_lambda_multiplier_keys_all_have_cn_mapping():
    """scripts.LEAGUE_LAMBDA_MULTIPLIER 同 LEAGUE_DC_RHO 守门。"""
    lam = _import("core.leagues").LEAGUE_LAMBDA_MULTIPLIER
    rev = {eng: cn for cn, eng in _LEAGUE_KEYS.items()}
    for eng in lam:
        assert eng in rev, (
            f"scripts.LEAGUE_LAMBDA_MULTIPLIER key '{eng}' 缺 LEAGUE_KEYS 反查"
        )


def test_league_config_football_keys_all_have_cn_mapping():
    """scripts.LEAGUE_CONFIG 足球子集每个 key 都能反查回中文全名。"""
    football = _football_league_keys()
    rev = {eng: cn for cn, eng in _LEAGUE_KEYS.items()}
    for eng in football:
        assert eng in rev, (
            f"scripts.LEAGUE_CONFIG 足球 key '{eng}' 缺 LEAGUE_KEYS 反查"
        )


def test_web_app_jc_aliases_match_config():
    """``web.app.jc`` 的白名单/映射与 ``web.config`` 一致。"""
    import web.config as config
    from web.app import jc as app_jc

    assert app_jc._BIG5 == config.BIG5
    assert app_jc.LEAGUE_KEYS == _LEAGUE_KEYS
    assert app_jc._BIG5_KEYS == _LEAGUE_KEYS  # 旧名保留兼容
    assert config.MODEL_LEAGUES == _MODEL_LEAGUES
    assert config.MAIN_LEAGUES == _ALL_LEAGUES
