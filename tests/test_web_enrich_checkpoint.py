"""AI 富化断点续跑（2026-10-28 事故）。

背景：上游是未实名 agnes 账号，RPM≈1 次/分钟；146 条按 5 条/批要 30 批，
旧实现只在**全部跑完**后 save_ai_scores ⇒ 作业被 1800s 超时杀掉时 0 条落盘，
第二天又从头开始。现在每批 checkpoint，重跑只补未完成条目。

锁的行为：
- _write_checkpoint/_load_checkpoint 的日期隔离与损坏容错
- main() 只把未完成条目交给 analyse_batch，并在每批后落盘 + 记账
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture
def enrich(tmp_path, monkeypatch):
    import web.enrich as mod

    monkeypatch.setattr(mod, "CHECKPOINT_FILE", tmp_path / "ckpt.json")
    return mod


def _item(league: str, home: str, away: str) -> dict:
    return {
        "name": f"{home} vs {away}",
        "home_en": home,
        "away_en": away,
        "league": league,
        "direction": "home",
    }


def test_checkpoint_roundtrip(enrich):
    enrich._write_checkpoint(TODAY, {"epl|A|B", "epl|C|D"})
    assert enrich._load_checkpoint(TODAY) == {"epl|A|B", "epl|C|D"}
    assert json.loads(enrich.CHECKPOINT_FILE.read_text())["date"] == TODAY


def test_checkpoint_ignores_other_day(enrich):
    enrich._write_checkpoint("2000-01-01", {"epl|A|B"})
    assert enrich._load_checkpoint(TODAY) == set()


def test_checkpoint_tolerates_corruption(enrich):
    enrich.CHECKPOINT_FILE.write_text("{not json")
    assert enrich._load_checkpoint(TODAY) == set()


def test_main_resumes_and_checkpoints_per_batch(enrich, monkeypatch):
    import ai.batch_pipeline as bp
    import ai.feedback_loop as fl

    done_item = _item("epl", "A", "B")
    pending_item = _item("epl", "C", "D")
    enrich._write_checkpoint(TODAY, {enrich._item_key(done_item)})

    monkeypatch.setattr(enrich, "collect_items", lambda: [done_item, pending_item])

    seen: list[list[dict]] = []
    saved: list[list[dict]] = []

    def fake_analyse_batch(items, context="", preference_prompt="", config=None, on_batch=None):
        seen.append(items)
        scored = [{**items[0], "ai_score": 77}]
        on_batch(1, 1, scored)
        return scored

    monkeypatch.setattr(bp, "analyse_batch", fake_analyse_batch)
    monkeypatch.setattr(fl, "save_ai_scores", lambda items, league_key="": saved.extend(items))

    assert enrich.main() == 0
    # 已完成的条目不重复送 LLM
    assert seen == [[pending_item]]
    # 每批即落盘
    assert saved == [{**pending_item, "ai_score": 77}]
    # checkpoint 累积到当日全部条目
    assert enrich._load_checkpoint(TODAY) == {
        enrich._item_key(done_item), enrich._item_key(pending_item),
    }


def test_main_skips_when_all_done(enrich, monkeypatch):
    import ai.batch_pipeline as bp

    item = _item("epl", "A", "B")
    enrich._write_checkpoint(TODAY, {enrich._item_key(item)})
    monkeypatch.setattr(enrich, "collect_items", lambda: [item])

    def boom(*a, **kw):  # 不应再调 LLM
        raise AssertionError("analyse_batch must not run when checkpoint covers all items")

    monkeypatch.setattr(bp, "analyse_batch", boom)
    assert enrich.main() == 0
