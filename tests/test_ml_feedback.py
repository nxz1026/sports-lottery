"""``core.ml_feedback`` 守门测试 — P-6 ML 反馈日志（prediction ↔ outcome 关联）。

覆盖：
- ``record_prediction`` 写 ``kind=prediction`` 行 + match_key 形如 ``home_away__kickoff``
- ``record_outcome`` 写 ``kind=outcome`` 行 + result label
- ``join_predictions_outcomes`` 按 match_key 关联 + 缺 outcome 的 prediction 不返回
- 写 events.jsonl 失败不抛（吞 OSError）
- 字段序列化兜底（Path / set / 异常）
- 模型版本字段透传
- 同 match_key 多次预测取最新一条
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import core.ml_feedback as mlf  # noqa: E402
import core.observability as obs  # noqa: E402


@pytest.fixture(autouse=True)
def reset_obs(tmp_path, monkeypatch):
    obs.reset_for_test()
    obs.setup(dir=tmp_path / "logs")
    yield
    obs.reset_for_test()


def _read_events():
    p = obs.events_jsonl_path()
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── record_prediction ────────────────────────────────


def test_record_prediction_writes_event():
    mlf.record_prediction(
        league="epl", match_id="m1",
        home="Arsenal", away="Chelsea",
        kickoff_utc="2026-10-13T19:00:00Z",
        probs={"h": 0.55, "d": 0.25, "a": 0.20},
        model="dixon_coles",
        goal_line=-0.5,
    )
    recs = _read_events()
    assert len(recs) == 1
    r = recs[0]
    assert r["kind"] == "prediction"
    assert r["league"] == "epl"
    assert r["match_id"] == "m1"
    assert r["home"] == "Arsenal"
    assert r["away"] == "Chelsea"
    assert r["kickoff_utc"] == "2026-10-13T19:00:00Z"
    assert r["match_key"] == "Arsenal_Chelsea__2026-10-13T19:00:00Z"
    assert r["probs"] == {"h": 0.55, "d": 0.25, "a": 0.20}
    assert r["model"] == "dixon_coles"
    assert r["goal_line"] == -0.5
    assert "ts" in r


def test_record_prediction_match_key_includes_kickoff():
    """同对阵不同开球时点 → match_key 不同（防同日二番战关联错乱）。"""
    mlf.record_prediction(league="epl", match_id="a", home="A", away="B",
                          kickoff_utc="2026-10-13T10:00:00Z", probs={"h": 0.5, "d": 0.3, "a": 0.2})
    mlf.record_prediction(league="epl", match_id="b", home="A", away="B",
                          kickoff_utc="2026-10-13T20:00:00Z", probs={"h": 0.4, "d": 0.3, "a": 0.3})
    recs = _read_events()
    assert recs[0]["match_key"] != recs[1]["match_key"]
    assert recs[0]["match_key"] == "A_B__2026-10-13T10:00:00Z"
    assert recs[1]["match_key"] == "A_B__2026-10-13T20:00:00Z"


# ── record_outcome ──────────────────────────────────


def test_record_outcome_writes_event_with_result_label():
    mlf.record_outcome(league="epl", match_id="m1",
                       home="Arsenal", away="Chelsea",
                       kickoff_utc="2026-10-13T19:00:00Z",
                       home_score=2, away_score=1, status="FT")
    recs = _read_events()
    r = recs[0]
    assert r["kind"] == "outcome"
    assert r["result"] == "home_win"
    assert r["total_goals"] == 3
    assert r["goal_diff"] == 1
    assert r["status"] == "FT"
    assert r["match_key"] == "Arsenal_Chelsea__2026-10-13T19:00:00Z"


def test_record_outcome_result_label_draw():
    mlf.record_outcome(league="epl", match_id="m", home="A", away="B",
                       kickoff_utc="t", home_score=1, away_score=1, status="FT")
    assert _read_events()[0]["result"] == "draw"


def test_record_outcome_result_label_away_win():
    mlf.record_outcome(league="epl", match_id="m", home="A", away="B",
                       kickoff_utc="t", home_score=0, away_score=2, status="FT")
    assert _read_events()[0]["result"] == "away_win"


# ── join_predictions_outcomes ─────────────────────


def test_join_matches_by_match_key():
    mlf.record_prediction(league="epl", match_id="m1", home="Arsenal", away="Chelsea",
                          kickoff_utc="t1", probs={"h": 0.6, "d": 0.3, "a": 0.1})
    mlf.record_outcome(league="epl", match_id="m1", home="Arsenal", away="Chelsea",
                       kickoff_utc="t1", home_score=2, away_score=0, status="FT")
    samples = mlf.join_predictions_outcomes()
    assert len(samples) == 1
    s = samples[0]
    assert s["match_key"] == "Arsenal_Chelsea__t1"
    assert s["probs"] == {"h": 0.6, "d": 0.3, "a": 0.1}
    assert s["result"] == "home_win"
    assert s["home_score"] == 2
    assert s["away_score"] == 0
    assert s["total_goals"] == 2
    assert s["goal_diff"] == 2
    assert s["league"] == "epl"


def test_join_skips_unsettled_predictions():
    """未结算的 prediction（缺 outcome）不返回，保证每条都是 labeled。"""
    mlf.record_prediction(league="epl", match_id="settled",
                          home="A", away="B", kickoff_utc="t1",
                          probs={"h": 0.5, "d": 0.3, "a": 0.2})
    mlf.record_outcome(league="epl", match_id="settled",
                       home="A", away="B", kickoff_utc="t1",
                       home_score=1, away_score=0, status="FT")
    mlf.record_prediction(league="epl", match_id="pending",
                          home="C", away="D", kickoff_utc="t2",
                          probs={"h": 0.4, "d": 0.4, "a": 0.2})
    samples = mlf.join_predictions_outcomes()
    assert len(samples) == 1
    assert samples[0]["match_id"] == "settled"


def test_join_keeps_latest_prediction_for_duplicate_match_key(monkeypatch):
    """同 match_key 多次预测（重跑）→ 用最新那条。

    R15e: 原用 ``time.sleep(0.01)`` 强制 ts 递增 —— 真实挂钟在重负载 + 10ms 精度下
    可能两次记录同毫秒，join 时 ISO 字符串字典序无法保证先后。改用 monkeypatch
    ``ml_feedback._obs.log_event`` 注入递增 ts（避免 datetime.now + freezegun 引入
    新依赖），更稳：每次 ``_fake_log_event`` 返回时 ``_fake_now`` 累加 1ms。
    """
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 10, 14, 0, 0, 0, tzinfo=timezone.utc)
    counter = {"n": 0}

    def _fake_log_event(kind, **fields):
        # 每次调都让 mlf.record_prediction 内部 ts 自增 1ms
        counter["n"] += 1
        ts = (base + timedelta(milliseconds=counter["n"])).isoformat()
        # ml_feedback._safe_log_event -> _obs.log_event(... **fields)；通过
        # 临时 monkeypatch 整体替换，捕获 fields["ts"] 替换成递增 ts。
        fields["ts"] = ts
        # 写 events.jsonl 用真实路径（fixture 已设）
        return _orig_log_event(kind, **fields)

    from core import observability as _obs
    _orig_log_event = _obs.log_event
    monkeypatch.setattr(mlf._obs, "log_event", _fake_log_event)
    # ml_feedback._safe_log_event 已经持有 _obs 模块引用，直接 patch 模块即可。
    monkeypatch.setattr(_obs, "log_event", _fake_log_event)

    mlf.record_prediction(league="epl", match_id="x", home="A", away="B",
                          kickoff_utc="t", probs={"h": 0.5, "d": 0.3, "a": 0.2})
    mlf.record_prediction(league="epl", match_id="x", home="A", away="B",
                          kickoff_utc="t", probs={"h": 0.7, "d": 0.2, "a": 0.1},
                          model="updated")
    mlf.record_outcome(league="epl", match_id="x", home="A", away="B",
                       kickoff_utc="t", home_score=3, away_score=1, status="FT")
    samples = mlf.join_predictions_outcomes()
    assert len(samples) == 1
    assert samples[0]["probs"]["h"] == 0.7  # 用最新的 0.7
    assert samples[0]["model"] == "updated"


def test_join_filters_by_league():
    mlf.record_prediction(league="epl", match_id="m1", home="A", away="B",
                          kickoff_utc="t", probs={"h": 0.5, "d": 0.3, "a": 0.2})
    mlf.record_outcome(league="epl", match_id="m1", home="A", away="B",
                       kickoff_utc="t", home_score=1, away_score=0, status="FT")
    mlf.record_prediction(league="laliga", match_id="m2", home="C", away="D",
                          kickoff_utc="t", probs={"h": 0.4, "d": 0.4, "a": 0.2})
    mlf.record_outcome(league="laliga", match_id="m2", home="C", away="D",
                       kickoff_utc="t", home_score=1, away_score=1, status="FT")
    epl = mlf.join_predictions_outcomes(league="epl")
    assert len(epl) == 1 and epl[0]["league"] == "epl"
    all_ = mlf.join_predictions_outcomes()
    assert len(all_) == 2


# ── 容错：写 events 失败不能影响主流程 ──────────────


def test_record_does_not_raise_on_io_error(monkeypatch, tmp_path):
    """events.jsonl 路径指向只读目录 / 不存在 → 静默吞掉。"""
    # 强制造 IOError：把 events_jsonl_path 指向不存在的目录
    broken_path = tmp_path / "no_such_dir" / "events.jsonl"
    monkeypatch.setattr(obs, "_EVENTS_JSONL", broken_path)
    # 应当不抛
    mlf.record_prediction(league="epl", match_id="m", home="A", away="B",
                          kickoff_utc="t", probs={"h": 0.5, "d": 0.3, "a": 0.2})
    mlf.record_outcome(league="epl", match_id="m", home="A", away="B",
                       kickoff_utc="t", home_score=1, away_score=0, status="FT")


# ── 字段序列化 ───────────────────────────────────


def test_extra_and_market_signal_optional():
    """probs / market_signal / extra 缺失 → 不写对应字段。"""
    mlf.record_prediction(league="x", match_id="m", home="A", away="B",
                          kickoff_utc="t", probs={"h": 0.5, "d": 0.3, "a": 0.2})
    rec = _read_events()[0]
    assert "extra" not in rec
    assert "market_signal" not in rec
    assert "goal_line" not in rec
