"""R39-1: web/routers/jobs.py::_today_has_data 6 tests direct + D-31 all-of-active lock.

惰性自动刷新判定 0 direct tests; HTTP /jobs/auto/refresh endpoint 内调用,
endpoint-level 仅做 wrapper, helper 本身未锁。错误判定会导致:
- 假阳性 (今日有数据但 helper 返 False) -> 白白触发 GHA, 配额/IO 浪费
- 假阴性 (今日无数据但 helper 返 True) -> 仪表盘永远 stale

helper 内含关键契约 (D-31):
- "all-of-active" 单调性: **每个** active league 都必须同时满足
  (1) `league in docs` 且 (2) `covers_date(docs[league]["data"], today) == True`
  缺一即 False; 反过来 active 集合为空时 vacuous-True。
- inactive league 完全不参与判定 (即使不在 docs)。
- 强依赖 store.latest_by_league 永远返 dict[str, dict] (永不 None) 才能
  安全 `docs[league].get("data", {})`。

6 tests:
- 无 active league (全 inactive) -> True (vacuous truth)
- 全部 active + 全部覆盖 -> True
- 部分 active league 缺 docs -> False
- 全在 docs 但 covers_date 返 False -> False
- active 存在 + inactive 缺失 docs -> True (inactive 滤掉)
- 空 docs + active 存在 -> False

测试通过 monkeypatch 隔离 LEAGUES / store.{bjt_today, latest_by_league,
covers_date}, 不影响真实环境。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from web import config  # noqa: F401  pre-import; ensure package init
from web.services import store
from web.routers import jobs as router_jobs
from web.routers.jobs import _today_has_data


FIXED_TODAY = __import__("datetime").date(2026, 7, 15)


@pytest.fixture
def fixed_today(monkeypatch):
    """固定 store.bjt_today() 返 2026-07-15，抹掉 'now' drift。"""
    monkeypatch.setattr(store, "bjt_today", lambda: FIXED_TODAY)


def _patch_leagues(monkeypatch, leagues: dict):
    """monkeypatch web.routers.jobs.LEAGUES (from import 后是模块本地 binding)。"""
    monkeypatch.setattr(router_jobs, "LEAGUES", leagues)


def _patch_latest(monkeypatch, docs: dict):
    """monkeypatch store.latest_by_league 返受控 docs。"""
    monkeypatch.setattr(store, "latest_by_league", lambda: docs)


def _patch_covers(monkeypatch, mapping: dict):
    """covers_date 返 mapping[league]; 没有 explicit mapping 的 league 返 False。"""
    monkeypatch.setattr(
        store, "covers_date",
        lambda data, day: bool(mapping.get(league_of_call(data, mapping), False)))


def league_of_call(data, mapping):
    """covers_date 是 (data, day) -> bool; 没有 league 参数。从映射中按 data identity 反查。"""
    for league, payload in mapping.items():
        if payload is data:
            return league
    return None


# ──────────────────────── D-31 all-of-active ────────────────────────


def test_today_has_data_no_active_leagues_is_vacuous_true(
    fixed_today, monkeypatch,
):
    """R39-1 D-31: 全部 league 都 inactive -> True (vacuous all on empty).

    inactive league 不参与判定；all() 空集 -> True。
    """
    _patch_leagues(monkeypatch, {
        "a": {"active": False},
        "b": {"active": False},
    })
    _patch_latest(monkeypatch, {})  # docs 空都不影响

    assert _today_has_data() is True


def test_today_has_data_all_active_and_covered_is_true(
    fixed_today, monkeypatch,
):
    """R39-1: 全 active + 全 covers_date=True -> True."""
    league_data_payload = {"_key_": "epl_payload"}
    _patch_leagues(monkeypatch, {
        "a": {"active": True},
        "b": {"active": True},
    })
    _patch_latest(monkeypatch, {
        "a": {"data": league_data_payload},
        "b": {"data": {"_key_": "b_payload"}},
    })
    # covers_date 通过 identity 比较 data 决定返 True
    def fake_covers(data, day):
        return data is league_data_payload or data.get("_key_") == "b_payload"
    monkeypatch.setattr(store, "covers_date", fake_covers)

    assert _today_has_data() is True


def test_today_has_data_one_active_league_missing_in_docs_is_false(
    fixed_today, monkeypatch,
):
    """R39-1: active 'b' league 缺 docs['b'] -> False (all-of-active 失败一处即 False)。"""
    _patch_leagues(monkeypatch, {
        "a": {"active": True},
        "b": {"active": True},
    })
    _patch_latest(monkeypatch, {
        "a": {"data": {"_key_": "a"}},
        # 故意缺 "b"
    })
    monkeypatch.setattr(store, "covers_date",
                        lambda data, day: data.get("_key_") == "a")

    assert _today_has_data() is False


def test_today_has_data_all_in_docs_but_covers_date_false(
    fixed_today, monkeypatch,
):
    """R39-1: 所有 active 都 in docs, 但 covers_date 返 False -> False (第 2 条件失败)。"""
    _patch_leagues(monkeypatch, {"a": {"active": True}})
    _patch_latest(monkeypatch, {
        "a": {"data": {"_key_": "a"}},
    })
    monkeypatch.setattr(store, "covers_date", lambda data, day: False)

    assert _today_has_data() is False


def test_today_has_data_inactive_league_absent_ignored(
    fixed_today, monkeypatch,
):
    """R39-1: 'b' inactive 且缺 docs['b'] -> True (inactive 完全跳过, 不影响判定)。

    `if LEAGUES[league].get("active", True)` 过滤掉 inactive。
    """
    _patch_leagues(monkeypatch, {
        "a": {"active": True},
        "b": {"active": False},
    })
    _patch_latest(monkeypatch, {
        "a": {"data": {"_key_": "a"}},
        # "b" 故意缺
    })
    monkeypatch.setattr(store, "covers_date",
                        lambda data, day: data.get("_key_") == "a")

    assert _today_has_data() is True


def test_today_has_data_empty_docs_with_active_league_is_false(
    fixed_today, monkeypatch,
):
    """R39-1: docs 空 + active 'a' 存在 -> False (`a` 不在 docs 即不满足第 1 条件)。"""
    _patch_leagues(monkeypatch, {"a": {"active": True}})
    _patch_latest(monkeypatch, {})

    monkeypatch.setattr(store, "covers_date", lambda data, day: True)

    assert _today_has_data() is False
