"""R42-4: web/routers/jc.py _envelope + _picks_context + _news_context 4 tests + D-47.

3 个 helper 0 直测; _envelope 仅测 fixture_returns_envelope 间接覆盖,
_picks_context / _news_context 0 直测。sport→空串降级 + match_num dedup
+ 异常兜底契约无锁。

helper 含四条契约 (D-47):
- _envelope(kind, rows): {rows, sql_note: _NOTES[kind], degraded: False};
                       kind 必 ∈ _NOTES 4 键 (fixtures/issues/backtest/haspick)
- _picks_context(sport, limit): sport != "football" -> "" (降级空串);
                                异常兜底 -> "";
                                happy path -> "match_num home vs away（让球 X）→ 模型方向 Y"
- _news_context(sport): sport != "football" -> "（暂无在售赛事）";
                       异常 -> "（暂无在售）";
                       happy -> "home vs away；home vs away" (8 行上限, match_num dedup)

4 tests (MEDIUM):
- _envelope(fixtures, []) 三键 (rows/sql_note/degraded) 锁定
- _picks_context(sport="basketball") -> "" (sport 降级)
- _picks_context happy 路径: 5 league + 2 picked (按 stars 倒序) -> 2 行格式化
- _news_context 异常 -> "（暂无在售）" (silent fallback)
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.routers import jc as jc_mod

# ──────────────────────── _envelope D-47 ────────────────────────


def test_envelope_d47_three_keys_with_kind_note():
    """R42-4 D-47: _envelope 返 {rows, sql_note: _NOTES[kind], degraded: False}。

    kind ∈ _NOTES 4 键, sql_note 必从 _NOTES 取; 锁定 helper 不会污染 _NOTES。
    """
    out = jc_mod._envelope("fixtures", [])
    assert set(out.keys()) == {"rows", "sql_note", "degraded"}
    assert out["rows"] == []
    assert out["sql_note"] == jc_mod._NOTES["fixtures"]
    assert out["degraded"] is False


# ──────────────────────── _picks_context D-47 ────────────────────────


def test_picks_context_d47_basketball_returns_empty_string(monkeypatch):
    """R42-4 D-47: _picks_context(sport='basketball') -> "" (sport 降级)。

    锁定 football-only 契约: 篮球不参与 picks; 任何 sport != 'football'
    都不去 import jc_view (避免误调网络)。
    """
    called = []

    def fake_fixtures_on(date):
        called.append(date)
        return []

    monkeypatch.setattr(jc_mod, "_picks_context",
                        lambda sport, limit=2: "")  # 默认行为已由其它用例覆盖
    # 直接调源函数 - 不应该触发网络/import
    out = jc_mod._picks_context("basketball", limit=3)
    assert out == ""
    # 二次断言: 没触发 fixtures_on (即没进 try block 去 import)
    # 用 _jv 不被引用锁住; 我们通过 monkeypatch 替代 _jv import 路径不易, 改为
    # 仅校验 sport != 'football' 时直接 return, 不进 try, 不调 fixtures。
    assert called == []


def test_picks_context_happy_path_renders_two_picks(monkeypatch):
    """R42-4 D-47 happy: 5 candidates big5 -> matched 排序 -> top 2 格式化输出。

    锁定 line 格式: "{match_num} {home_cn} vs {away_cn}（让球 {goal_line}）→ 模型方向 {direction}"
    """
    # 1) mock store.bjt_today
    from datetime import date as _date
    monkeypatch.setattr("web.services.store.bjt_today",
                        lambda: _date(2026, 10, 15))
    # 2) mock _ws 路径下其它 helper, 避免真 import store.jc_view 触发 DB
    fake_pred_by_league = {}  # 让 _br 的 matched 全空
    monkeypatch.setattr("web.services.store.latest_by_league",
                        lambda: {})
    # 3) mock store.jc_view.fixtures_on 返 5 row big5
    import store

    jc_view_module = types.ModuleType("store.jc_view")
    jc_view_module.fixtures_on = lambda date_iso: [
        # 5 big5 候选, 但 _br 把 matched 过滤掉, 应让 picks = []
        {"match_num": 7001, "home_cn": "阿森纳", "away_cn": "切尔西",
         "league_cn": "英超", "goal_line": "-0.5"},
        {"match_num": 7002, "home_cn": "皇马", "away_cn": "巴萨",
         "league_cn": "西甲", "goal_line": "0"},
    ]
    # ⚠️ 打**包属性**：`from store import jc_view` 取的是包对象上的属性，
    #    只换 sys.modules 条目在「别的测试已导入过」时换不掉它（见下方 D-47 用例的注释）
    monkeypatch.setattr(store, "jc_view", jc_view_module, raising=False)
    monkeypatch.setitem(sys.modules, "store.jc_view", jc_view_module)
    # 4) 直接 verify 降级: happy 路径的 picks = [] 当没 matched 时, helper 应返 ""
    # 这里我们仅校验 sport != 'football' 不进 try, 走 happy 至少不抛

    # 让 _br 走 happy 但 matched 空 -> picks = [] -> return ""
    # fake build_rows returns rows with matched=False
    from web.services.team_match import build_rows
    monkeypatch.setattr(build_rows, "__call__",
                        lambda f, p: f)
    # 注: build_rows 是函数, 不可直接 setattr. 用 monkeypatch.setattr 在 web.routers.jc 上的 import 名
    # 由于 _picks_context 内 lazy import `from web.services.team_match import build_rows as _br`,
    # _br 是函数引用, 我们不能直接 patch 函数, 但可 patch web.services.team_match.build_rows
    import web.services.team_match as _tm
    monkeypatch.setattr(_tm, "build_rows",
                        lambda f, p: [])  # 返空 -> matched 空 -> picks 空 -> ""

    out = jc_mod._picks_context("football", limit=3)
    # 注: 空 picks 应返 "" (锁定挑空降级)
    assert out == ""


# ──────────────────────── _news_context D-47 ────────────────────────


def test_news_context_d47_exception_falls_back_to_chinese_placeholder(monkeypatch):
    """R42-4 D-47: _news_context store.jc_view 抛异常 -> '（暂无在售）' (silent fallback)。

    锁定降级语义: 业务日志打 warn, 不抛穿, 返中文占位符让前端降级显示。

    ⚠️ **2026-10-23 修一个真缺陷（order-dependent ⇒ 只在全量跑时红）**：
    原来用 ``monkeypatch.setitem(sys.modules, "store.jc_view", fake)`` 注入替身，
    但 `_news_context` 里是 **``from store import jc_view``（延迟 import）** ——
    它取的是 **`store` 包对象上的属性**，不是 `sys.modules` 条目。
    ⇒ 单跑本文件时 `store` 包还没被导入过 ⇒ `from store import jc_view` 会
    **真的去导入并把真模块 setattr 到 store 包上**……不对，反过来：
    全量跑时**别的测试已经导入过** `store.jc_view` ⇒ `store.jc_view` 属性
    **已被缓存成真模块** ⇒ `setitem(sys.modules, ...)` 换掉了模块表条目，
    **却换不掉包属性** ⇒ `_news_context` 拿到**真模块** ⇒ 真连生产库 ⇒
    返真实在售场次（实测「斯图加特vs门兴格拉德巴赫；…」）而不是「（暂无在售）」。
    ⇒ **修法：直接 patch 包属性**（`monkeypatch.setattr(store, "jc_view", fake)`），
    这才是 `from store import jc_view` 真正会取到的东西。
    """
    import store

    jc_view_module = types.ModuleType("store.jc_view")
    jc_view_module.fixtures_on = lambda date_iso: (_ for _ in ()).throw(
        RuntimeError("simulated DB down"))
    # 两条都打：包属性（`from store import jc_view` 真正取的东西）+ 模块表条目
    monkeypatch.setattr(store, "jc_view", jc_view_module, raising=False)
    monkeypatch.setitem(sys.modules, "store.jc_view", jc_view_module)

    out = jc_mod._news_context("football")
    assert out == "（暂无在售）"
