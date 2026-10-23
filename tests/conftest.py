"""pytest 根级 conftest —— 收敛跨测试复用的 fixture（+ 一条 autouse 安全边界）。

R17 #5: 之前 tests/test_jc_view_aggregations.py:29 + tests/test_jc_ops_view.py:27
两处重复实现 ``fake_pg`` fixture（jc_view / jc_ops_view 用的 fake read_conn
工厂，行为基本相同）。提取到本 conftest.py 让两测试共享，其它测试按需引用。

注意：本 conftest 的 ``fake_pg`` **不起 autouse**。``reset_obs`` / 临时目录隔离等
autouse fixture 仍由各测试文件本地持有（避免引入跨文件隐式依赖）。

⚠️ **例外：``isolate_i18n_cache`` 这条 autouse fixture 必须起**（2026-10-23 加）——
它不是「测试便利」，是**安全边界**：**测试绝不允许写生产仓库的文件**。
详见该 fixture 的 docstring。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_i18n_cache(tmp_path, monkeypatch):
    """**autouse**：把 i18n 的 LLM 翻译缓存文件重定向到临时目录（每个用例独立）。

    ⚠️ **2026-10-23 加（真实事故）**：Oracle 上跑全量 pytest 后，
    仓库里的 ``scripts/references/team_translations.json`` **被测试改了**
    （多出「阿森纳 vs 切尔西」「Liverpool」「Everton」等测试夹具队名）。

    根因链：
    1. ``scripts/core/data/parse.py::_warm_team_names`` → 真调
       ``core.i18n.warm_translations`` → ``translate_team_names`` → **真调 LLM** →
       ``save_translations`` → **写 ``TRANSLATION_CACHE_FILE``**；
    2. ``TRANSLATION_CACHE_FILE`` 指向**仓库内的生产文件**；
    3. 生产机（Oracle）**有 LLM 凭证** ⇒ 翻译**真的成功** ⇒ 真的落盘；
       开发机（无凭证）翻译失败 ⇒ 不落盘 ⇒ **本机跑不出这个污染**（环境差异）。
    ⇒ 触发路径不止一处：任何调 ``parse_events`` / ``_warm_team_names`` 的用例都可能中招
    （实测 ``tests/test_ucl_league_support.py`` 是其中一个）。

    ⇒ 本 fixture 在**每个用例**开始时把 ``TRANSLATION_CACHE_FILE`` 指到 ``tmp_path``、
    并清掉模块级 ``_CACHE`` ⇒ **测试永远碰不到生产文件**（无论环境有没有 LLM 凭证）。
    """
    try:
        import core.i18n as i18n_mod
    except Exception:  # noqa: BLE001 - 脚本不在 sys.path 时跳过（不阻断其它用例）
        yield
        return
    orig_file = i18n_mod.TRANSLATION_CACHE_FILE
    orig_cache = i18n_mod._CACHE
    try:
        i18n_mod.TRANSLATION_CACHE_FILE = tmp_path / "team_translations.json"
        i18n_mod._CACHE = None
        yield
    finally:
        i18n_mod.TRANSLATION_CACHE_FILE = orig_file
        i18n_mod._CACHE = orig_cache


@pytest.fixture
def fake_pg():
    """返回 fake_read_conn 工厂：queue 按 cursor.execute 调用次数 pop。

    用法::

        def test_x(fake_pg):
            fake_read_conn = fake_pg([_TEAMS_OK, _TOPICS_OK, ...])
            with patch.object(jc_view, "read_conn", fake_read_conn):
                result = jc_view.ops_snapshot(...)
    """
    def _make(rows: list):
        queue = list(rows)

        def fake_read_conn(role: str = "ro"):
            class _Cursor:
                row_factory = None

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

                def execute(self_inner, sql, params=None):
                    self_inner._next = queue.pop(0) if queue else []

                def fetchone(self_inner):
                    """R18 #3: 单列查询走 fetchone() 而非 fetchall()。

                    queue 元素 = list[dict]（fetchall）/ list[tuple]（fetchone）
                    / Exception（raise）。list[tuple] 时取 [0]；空 → None。
                    """
                    nxt = getattr(self_inner, "_next", [])
                    if isinstance(nxt, Exception):
                        raise nxt
                    if isinstance(nxt, list) and nxt:
                        return nxt[0]
                    return None

                def fetchall(self_inner):
                    nxt = getattr(self_inner, "_next", [])
                    if isinstance(nxt, Exception):
                        raise nxt
                    return list(nxt)

            class _Conn:
                def cursor(self_inner):
                    return _Cursor()

            class _Ctx:
                def __enter__(self_inner):
                    return self_inner.conn

                def __exit__(self_inner, *exc):
                    return False

            ctx = _Ctx()
            ctx.conn = _Conn()
            return ctx

        return fake_read_conn

    return _make
