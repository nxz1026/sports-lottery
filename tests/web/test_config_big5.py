"""P2-15（intra-web 去重）单测：web.config.BIG5 与 web.config.big5_with_temp。

原 web/app/jc.py 与 web/routers/jc.py 各持一份 ``_BIG5`` 与 ``_big5_with_temp``
（实现完全相同）。本批合并到 web.config（单一真源），本测试守住"config
端就是权威"，并验证 app/router 端的 alias 行为一致。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def test_big5_is_frozenset_with_five_leagues():
    """BIG5 = frozenset，5 项，中文全名与历史一致。"""
    import web.config as config

    assert isinstance(config.BIG5, frozenset)
    assert len(config.BIG5) == 5
    assert config.BIG5 == frozenset({
        "英格兰超级联赛", "西班牙甲级联赛", "意大利甲级联赛",
        "德国甲级联赛", "法国甲级联赛",
    })


def test_big5_with_temp_returns_set_with_at_least_five(monkeypatch):
    """默认（env 空）→ 集合含 5 大联赛 + 欧冠（模型正式覆盖），返回 set 类型。"""
    monkeypatch.setenv("LEAGUE_TEMP_LEAGUES", "")
    import web.config as config
    importlib.reload(config)

    s = config.big5_with_temp()
    assert isinstance(s, set)
    assert config.BIG5.issubset(s)
    assert config.MODEL_LEAGUES.issubset(s)
    assert len(s) == 6  # 五大联赛 + 欧冠


def test_big5_with_temp_unions_env_csv(monkeypatch):
    """env CSV 配临时赛事 → 集合 = 5 大 + 欧冠 + 临时（去重）。"""
    monkeypatch.setenv("LEAGUE_TEMP_LEAGUES", "亚运会男足,亚运会女足,欧罗巴联赛")
    import web.config as config
    importlib.reload(config)

    s = config.big5_with_temp()
    assert "亚运会男足" in s and "亚运会女足" in s and "欧罗巴联赛" in s
    assert config.BIG5.issubset(s)
    assert len(s) == 9  # 5 大 + 欧冠 + 3 临时


def test_big5_with_temp_dedup_repeated_env_value(monkeypatch):
    """env 里重复五大联赛名 → set 去重，长度 = 5 + 欧冠 + 临时数 - 重复数。"""
    monkeypatch.setenv("LEAGUE_TEMP_LEAGUES", "英格兰超级联赛,亚运会男足,英格兰超级联赛")
    import web.config as config
    importlib.reload(config)

    s = config.big5_with_temp()
    assert len(s) == 7  # 5 大（含 1 重复）+ 欧冠 + 1 亚运


def test_app_jc_and_router_jc_share_same_helper(monkeypatch):
    """``web.app.jc._big5_with_temp`` 与 ``web.routers.jc._big5_with_temp``
    返回同一集合（同一底层真源 web.config.big5_with_temp）。

    旧两份各自实现的"漂移风险"用这个测试永久锁住。
    """
    monkeypatch.setenv("LEAGUE_TEMP_LEAGUES", "")
    import web.config as config
    importlib.reload(config)
    from web.app import jc as app_jc
    from web.routers import jc as router_jc
    importlib.reload(app_jc)
    importlib.reload(router_jc)

    s_app = app_jc._big5_with_temp()
    s_router = router_jc._big5_with_temp()
    assert s_app == s_router == config.big5_with_temp()


def test_app_jc_and_router_jc_share_big5_constant(monkeypatch):
    """``web.app.jc._BIG5`` 与 ``web.routers.jc._BIG5`` 都 alias 到 ``web.config.BIG5``。"""
    monkeypatch.setenv("LEAGUE_TEMP_LEAGUES", "")
    import web.config as config
    importlib.reload(config)
    from web.app import jc as app_jc
    from web.routers import jc as router_jc
    importlib.reload(app_jc)
    importlib.reload(router_jc)

    assert app_jc._BIG5 == router_jc._BIG5 == config.BIG5
