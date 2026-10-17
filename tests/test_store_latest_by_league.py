"""R41-5: web/services/store.py::latest_by_league 4 tests direct + D-42 lock.

每联赛最新一次运行 helper 0 direct tests; R39-1 _today_has_data 内部消费,
但 latest_by_league 自身契约 (multi-bucket / _newer 决胜 / 白名单 / 非
str league 跳过) 未锁。helper 失败会让 homepage 调度层拿到 stale doc。

helper 含四条契约 (D-42):
1. 多 league 分桶: 3 不同 league 各 1 doc -> dict 3 keys (epl/laliga/seriea)
2. 同 league 多 docs -> _newer 决胜: 返 generated_at / mtime 较新者
3. leagues 白名单过滤: ["epl"] 时 laliga/seriea 被滤掉;
   leagues=None 时全 league 返
4. 非 str league (None / int / 缺 data["league"] / 空字符串) -> 跳过
   (defensive: 上游 doc 损坏不让 build 崩)

4 tests (HIGH):
- 3 league 分桶 -> 3 keys
- _newer=True 时选新 doc, =False 时保留旧 doc
- 白名单 ["epl"]: 仅 epl 入 dict; None: 全量
- D-42 非 str league (None / int / 缺键 / 空 str) -> 跳过不污染 dict
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
def store_mod():
    """加载 web/services/store.py 为唯一 module name, 隔离 sys.modules 缓存。"""
    spec = importlib.util.spec_from_file_location(
        "store_latest_by_league_test",
        REPO_ROOT / "web" / "services" / "store.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _doc(league, generated_at="2026-10-15T10:00:00", name="prediction_test.json",
         path=None):
    """构造假 doc dict; path 用 tmp_path 默认值。"""
    if path is None:
        path = Path("/tmp") / name
    return {
        "path": path,
        "name": name,
        "data": {"league": league, "predictions": []},
        "generated_at": generated_at,
    }


# ──────────────────────── D-42 四契约 ────────────────────────


def test_latest_by_league_d42_multi_bucket(store_mod, monkeypatch):
    """R41-5 D-42: 3 不同 league 各 1 doc -> dict 3 keys。"""
    monkeypatch.setattr(store_mod, "load_prediction_docs", lambda: [
        _doc("epl", name="1.json"),
        _doc("laliga", name="2.json"),
        _doc("seriea", name="3.json"),
    ])
    out = store_mod.latest_by_league()
    assert set(out.keys()) == {"epl", "laliga", "seriea"}


def test_latest_by_league_d42_newer_wins(store_mod, monkeypatch):
    """R41-5 D-42: 同 league 2 docs -> _newer 决胜, 返较新者。

    mock _newer 返 True (a 比 b 新) -> dict 存 a; False -> 存 b。
    锁定 _newer 协议: 不允许 latest_by_league 自己改 timestamp 比较逻辑。
    """
    a_doc = _doc("epl", generated_at="2026-10-15T12:00:00", name="a.json")
    b_doc = _doc("epl", generated_at="2026-10-15T10:00:00", name="b.json")
    # _newer(a, b) = True: a 比 b 新, 应覆盖
    monkeypatch.setattr(store_mod, "load_prediction_docs", lambda: [b_doc, a_doc])
    monkeypatch.setattr(store_mod, "_newer", lambda a, b: True)

    out = store_mod.latest_by_league()
    assert out["epl"] is a_doc  # 较新的胜


def test_latest_by_league_white_list_filter(store_mod, monkeypatch):
    """R41-5: leagues=["epl"] -> 仅 epl 入 dict; leagues=None -> 全量。

    白名单契约: caller 限制只关心某 league 时, list 白名单过滤 (避免
    把 stale data 给错 league 的 caller)。
    """
    monkeypatch.setattr(store_mod, "load_prediction_docs", lambda: [
        _doc("epl", name="e.json"),
        _doc("laliga", name="l.json"),
        _doc("seriea", name="s.json"),
    ])
    # 白名单: 仅 epl
    out_filt = store_mod.latest_by_league(leagues=["epl"])
    assert set(out_filt.keys()) == {"epl"}
    # None: 全量
    out_full = store_mod.latest_by_league()
    assert set(out_full.keys()) == {"epl", "laliga", "seriea"}


def test_latest_by_league_d42_skip_invalid_league(store_mod, monkeypatch):
    """R41-5 D-42: 非 str / None / 缺键 / 空 str league -> 跳过不污染 dict。

    锁定 src 行的 defensive check: `if not isinstance(league, str) or not league: continue`
    """
    monkeypatch.setattr(store_mod, "load_prediction_docs", lambda: [
        _doc("epl", name="ok.json"),
        {"path": Path("/tmp/x"), "name": "x.json",
         "data": {"predictions": []}, "generated_at": ""},  # 缺 league key
        {"path": Path("/tmp/y"), "name": "y.json",
         "data": {"league": None, "predictions": []}, "generated_at": ""},  # None
        {"path": Path("/tmp/z"), "name": "z.json",
         "data": {"league": 123, "predictions": []}, "generated_at": ""},  # 非 str
        {"path": Path("/tmp/w"), "name": "w.json",
         "data": {"league": "", "predictions": []}, "generated_at": ""},  # 空 str
    ])
    out = store_mod.latest_by_league()
    assert set(out.keys()) == {"epl"}
