"""R43-6: web/services/store.py::results_by_bjt_date 4 tests direct + D-55 lock.

BJT 比赛日赛果合并 helper 0 直测; 端点 GET /api/v1/results 间接消费, 但
helper 自身契约 (跨 UTC 日去重 + id 兜底指纹) 未锁。

helper 含四条契约 (D-55):
- day 当天 + day-1 (BJT 0-8 = 前 UTC 日) 两个文件按 id 去重合并
- 顺序: day 在前 (今天), day-1 在后 (昨天)
- 无 id 行: 整条记录 fingerprint 兜底 (防止跨两天缺 id 时重复导出)
- 缺文件: 结果空 list (不抛穿)

4 tests (MEDIUM):
- 单一文件: 仅 day 当天一份 -> 该文件全返
- 双文件无重复: 两天各自 match -> 全合并, today 在前 yesterday 在后
- 双文件有 id 重复 -> 只保留首个 (today 优先)
- D-55 双文件都缺 -> 返 [] (不抛)
- bonus: 无 id 行用 fingerprint 兜底去重
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def store_mod(tmp_path, monkeypatch):
    """加载 web/services/store.py + 重定向 OUTPUT_DIR 到 tmp 隔离。"""
    spec = importlib.util.spec_from_file_location(
        "store_results_by_bjt_date_test",
        REPO_ROOT / "web" / "services" / "store.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "OUTPUT_DIR", tmp_path)
    return mod


def _write_results(store_mod, day_str: str, matches: list):
    """在 OUTPUT_DIR/results/result_{day}.json 写 {"matches": [...]} 字典结构。"""
    results_dir = store_mod.OUTPUT_DIR / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"result_{day_str}.json").write_text(
        json.dumps({"matches": matches}, ensure_ascii=False), encoding="utf-8")


# ──────────────────────── D-55 四契约 ────────────────────────


def test_results_by_bjt_date_d55_single_day_only(store_mod):
    """R43-5 (R43-6) D-55: 仅 day 当天文件存在 -> 该文件全返。

    day-1 缺文件, 不抛穿, 仅 today 命中。
    """
    today = date(2026, 10, 15)
    _write_results(store_mod, today.isoformat(), [
        {"id": "m1", "home": "Arsenal", "score": "2-1"},
    ])
    out = store_mod.results_by_bjt_date(today)
    assert out == [{"id": "m1", "home": "Arsenal", "score": "2-1"}]


def test_results_by_bjt_date_d55_two_days_merged_today_first(store_mod):
    """R43-6 D-55: 两天各自 match 全异 -> 全合并, today 在前, day-1 在后。"""
    today = date(2026, 10, 15)
    yesterday = today - timedelta(days=1)
    _write_results(store_mod, today.isoformat(), [
        {"id": "today1", "home": "Arsenal"},
    ])
    _write_results(store_mod, yesterday.isoformat(), [
        {"id": "yest1", "home": "Liverpool"},
    ])
    out = store_mod.results_by_bjt_date(today)
    # today 在前, yesterday 在后
    assert [m["id"] for m in out] == ["today1", "yest1"]


def test_results_by_bjt_date_d55_id_dedup_keeps_today(store_mod):
    """R43-6 D-55: 同 id 在两天都出现 -> 只保留 today 那条 (first-seen wins)。

    锁定 first-seen: today 优先, day-1 重复同 id 被去重。
    """
    today = date(2026, 10, 15)
    yesterday = today - timedelta(days=1)
    _write_results(store_mod, today.isoformat(), [
        {"id": "shared", "score": "today-3-1"},
    ])
    _write_results(store_mod, yesterday.isoformat(), [
        {"id": "shared", "score": "yest-2-0"},
    ])
    out = store_mod.results_by_bjt_date(today)
    assert len(out) == 1
    assert out[0]["score"] == "today-3-1"  # today 版本胜


def test_results_by_bjt_date_d55_missing_files_returns_empty(store_mod):
    """R43-6 D-55: 两天文件都缺 -> 返 [] (不抛穿)。

    锁定 silent fallback: 历史结果缺文件 / 还没跑过预测时, 上游 API
    调用 results_by_bjt_date 不会抛 FileNotFoundError。
    """
    today = date(2026, 10, 15)
    out = store_mod.results_by_bjt_date(today)
    assert out == []
