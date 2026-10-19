"""R31 #3: web/services/store.py::history_by_league + _newer 三段 fallback 直测。

现状：history_by_league 是 history 视图依赖；tests/web/test_m2_store.py
+ tests/test_web_store_mtime.py 共 0 测直测 history_by_league。
_newer / _file_dt / _as_aware 仅在 latest_by_league 集成测试中间接跑。

本轮补 4 测，覆盖：
- history_by_league groups by league + 降序
- history_by_league filter by leagues arg
- history_by_league missing generated_at 回落到 BJT min（排到队尾）
- _newer 三段 fallback：filename ts → generated_at → mtime
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

BJT = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def store_mod(tmp_path, monkeypatch):
    """加载 store.py 为唯一 module name；OUTPUT_DIR → tmp/predictions。

    store.py 走 OUTPUT_DIR/predictions（顶层常量仅 BASE_DIR/OUTPUT_DIR）。
    同时清掉模块级 cache（_load_prediction_docs_cached / _pred_index_by_pair_cached）
    防止跨 fixture 串数据。
    """
    spec = importlib.util.spec_from_file_location(
        "store_hist_test", REPO_ROOT / "web" / "services" / "store.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    pred_dir = tmp_path / "predictions"
    pred_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mod, "BASE_DIR", tmp_path)
    # 清空模块级缓存防跨 fixture 污染
    if hasattr(mod, "_load_prediction_docs_cached"):
        mod._load_prediction_docs_cached.clear()
    if hasattr(mod, "_pred_index_by_pair_cached"):
        mod._pred_index_by_pair_cached.clear()
    return mod, pred_dir


def _make_doc(pred_dir: Path, *, name: str, league: str,
              generated_at: str | None = "2026-10-15T12:00:00+08:00"):
    """写一份合法 prediction_*.json fixture，返回 path。

    R31 #3 schema fix：production JSON 顶层就是 league/generated_at/predictions，
    store.load_prediction_docs 把整个 JSON 赋给 doc["data"]，doc["data"].get("league")
    才能命中。早期 fixture 错位包了 {"data": {league, ...}}，导致 league 永远 None。
    """
    path = pred_dir / name
    payload = {
        "generated_at": generated_at,
        "data_window": "20261015-20261020",
        "league": league,
        "predictions": [],
    }
    if generated_at is None:
        payload.pop("generated_at", None)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ──────────────────────── history_by_league ────────────────────────


def test_history_by_league_groups_and_sorts_desc(store_mod):
    """R31 #3：3 league × 2 doc 不同 generated_at → 返 3 league, 每 league 2 doc 倒序。"""
    mod, pred_dir = store_mod
    _make_doc(pred_dir, name="prediction_2026-10-14_10_epl.json", league="epl",
              generated_at="2026-10-14T10:00:00+08:00")
    _make_doc(pred_dir, name="prediction_2026-10-15_10_epl.json", league="epl",
              generated_at="2026-10-15T10:00:00+08:00")
    _make_doc(pred_dir, name="prediction_2026-10-14_10_laliga.json", league="laliga",
              generated_at="2026-10-14T10:00:00+08:00")
    _make_doc(pred_dir, name="prediction_2026-10-15_10_laliga.json", league="laliga",
              generated_at="2026-10-15T10:00:00+08:00")
    _make_doc(pred_dir, name="prediction_2026-10-14_10_seriea.json", league="seriea",
              generated_at="2026-10-14T10:00:00+08:00")

    out = mod.history_by_league()
    assert set(out) == {"epl", "laliga", "seriea"}
    # 每 league 2 doc（seriea 仅 1）
    assert len(out["epl"]) == 2
    assert len(out["laliga"]) == 2
    assert len(out["seriea"]) == 1
    # epl 倒序：15 优先 14
    epl_ts = [d.get("generated_at") for d in out["epl"]]
    assert epl_ts[0] > epl_ts[1]
    assert epl_ts[0].year == 2026 and epl_ts[0].month == 10 and epl_ts[0].day == 15
    assert epl_ts[1].year == 2026 and epl_ts[1].month == 10 and epl_ts[1].day == 14


def test_history_by_league_filters_by_leagues_arg(store_mod):
    """R31 #3：leagues=["epl"] 过滤掉 laliga/seriea。"""
    mod, pred_dir = store_mod
    _make_doc(pred_dir, name="prediction_2026-10-15_10_epl.json", league="epl",
              generated_at="2026-10-15T10:00:00+08:00")
    _make_doc(pred_dir, name="prediction_2026-10-15_10_laliga.json", league="laliga",
              generated_at="2026-10-15T10:00:00+08:00")

    out = mod.history_by_league(leagues=["epl"])
    assert set(out) == {"epl"}
    assert len(out["epl"]) == 1


def test_history_by_league_missing_generated_at_sorts_to_end(store_mod):
    """R31 #3：doc 无 generated_at → 仍归入 bucket 但排到队尾（按 BJT min 兜底）。"""
    mod, pred_dir = store_mod
    with_ts = _make_doc(pred_dir, name="prediction_2026-10-15_10_epl.json",
                          league="epl", generated_at="2026-10-15T10:00:00+08:00")
    no_ts = _make_doc(pred_dir, name="prediction_2026-10-14_10_epl.json",
                        league="epl", generated_at=None)

    out = mod.history_by_league()
    # 含 generated_at 排前，无 generated_at 排后（datetime.min 兜底）
    assert out["epl"][0]["path"].name == "prediction_2026-10-15_10_epl.json"
    assert out["epl"][1]["path"].name == "prediction_2026-10-14_10_epl.json"


# ──────────────────────── _newer 三段 fallback ────────────────────────


def test_newer_filename_timestamp_decides(store_mod):
    """R31 #3：filename 时间戳差异 → 直接决胜。

    D-12 bug-shaped 契约锁定：_file_dt 正则 `prediction_(\\d{4}-\\d{2}-\\d{2})_(\\d{2})\\.json`
    不接受带联赛后缀文件名；但所有 _save_output 写出文件均带 _league 后缀
    （scripts/predict.py line 285：``f"prediction_{ts}{suffix}.json"``）。
    生产环境下 _file_dt 永远返 None → filename_ts path 不可达 → _newer
    永远回退到 generated_at/mtime path。本测试用生产文件名格式（带 epl 后缀），
    验证回退路径生效。
    """
    from datetime import datetime as _dt
    mod, pred_dir = store_mod
    # 写两份文件名不同 hour 但 generated_at 也不同的 fixture
    older = _make_doc(pred_dir, name="prediction_2026-10-10_10_epl.json", league="epl",
                      generated_at="2026-10-10T10:00:00+08:00")
    newer = _make_doc(pred_dir, name="prediction_2026-10-15_10_epl.json", league="epl",
                      generated_at="2026-10-15T10:00:00+08:00")

    # filename_ts path 走不到（regex bug）→ 回退 generated_at：10-15 > 10-10 = True
    assert mod._newer(
        {"path": newer, "generated_at": _dt.fromisoformat("2026-10-15T10:00:00+08:00")},
        {"path": older, "generated_at": _dt.fromisoformat("2026-10-10T10:00:00+08:00")},
    ) is True
    # 反向 false
    assert mod._newer(
        {"path": older, "generated_at": _dt.fromisoformat("2026-10-10T10:00:00+08:00")},
        {"path": newer, "generated_at": _dt.fromisoformat("2026-10-15T10:00:00+08:00")},
    ) is False


def test_newer_generated_at_fallback_when_same_hour(store_mod):
    """R31 #3：filename ts 相同（同 10 点钟）→ 回退 generated_at 比较。

    两文件同 hour（10）但 generated_at 不同（11 vs 10）。
    filename_ts path 两 file_dt 都是 2026-10-15 10 BJT（相等）→ 走 generated_at 比较。
    """
    from datetime import datetime as _dt
    mod, pred_dir = store_mod
    older = _make_doc(pred_dir, name="prediction_2026-10-15_10_epl.json", league="epl",
                      generated_at="2026-10-15T10:00:00+08:00")
    newer = _make_doc(pred_dir, name="prediction_2026-10-15_10_epl.json", league="epl",
                      generated_at="2026-10-15T11:00:00+08:00")

    # 同 filename 同 hour（10），generated_at 11 vs 10 → newer = True
    assert mod._newer(
        {"path": newer, "generated_at": _dt.fromisoformat("2026-10-15T11:00:00+08:00")},
        {"path": older, "generated_at": _dt.fromisoformat("2026-10-15T10:00:00+08:00")},
    ) is True
    # 反向 false
    assert mod._newer(
        {"path": older, "generated_at": _dt.fromisoformat("2026-10-15T10:00:00+08:00")},
        {"path": newer, "generated_at": _dt.fromisoformat("2026-10-15T11:00:00+08:00")},
    ) is False
