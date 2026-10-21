"""建议 3（命中率改分层 + 样本量，主指标换 CLV）的契约单测。

分三组：
  A. 纯函数（无 DB）：Wilson 区间、BJT 比赛日换算、稳定键、分层逻辑；
  B. 写手（mock 连接）：归档写入的字段是否正确（含 jc_match_id 关联）；
  C. 真库（无库则 skip）：DDL 就位时跑一遍「归档 → 收盘 → CLV → 日汇总」全链。

⚠️ C 组需要 `docs/db/suggestion3_schema.sql` 已执行；表不存在时**跳过**（不 fail），
并在 skip 原因里写明缺哪张表 —— 免得「没跑」被误读成「跑过了」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.bjt import bjt_date  # noqa: E402
from core.team_alias import alias_table, canon_names, teams_match  # noqa: E402
from model.archive import match_date_bjt, stable_match_key  # noqa: E402

from web.services.accuracy import (  # noqa: E402
    _conf_band,
    _direction_hit,
    _stars_of,
    league_accuracy_stratified,
    wilson_interval,
)

# ── A. 纯函数 ───────────────────────────────────────────────────────────

def test_wilson_n0_returns_full_range():
    """无样本 ⇒ (0,1)：不给假区间，直接说「不知道」。"""
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_known_values():
    """对照 Wilson 公式的已知结果（95%）。"""
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0
    assert 0.27 < hi < 0.29           # 经典值 0.2775
    lo, hi = wilson_interval(10, 10)
    assert hi == pytest.approx(1.0)
    assert 0.71 < lo < 0.73           # 经典值 0.7225
    lo, hi = wilson_interval(5, 10)
    assert 0.23 < lo < 0.25 and 0.75 < hi < 0.77


def test_wilson_always_inside_unit_interval():
    """任意 hits/n 都不得越界（正态近似在小样本会越界，Wilson 不会）。"""
    for n in range(1, 40):
        for hits in range(0, n + 1):
            lo, hi = wilson_interval(hits, n)
            assert 0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0
            assert lo <= hi


def test_wilson_narrows_as_n_grows():
    """n 变大 ⇒ 区间收窄（同命中率下）。"""
    w_small = wilson_interval(5, 10)[1] - wilson_interval(5, 10)[0]
    w_big = wilson_interval(500, 1000)[1] - wilson_interval(500, 1000)[0]
    assert w_big < w_small


def test_bjt_date_crosses_utc_day_boundary():
    """UTC 16:45 = BJT 次日 00:45 ⇒ 比赛日必须跨到次日。"""
    assert bjt_date("2026-10-20T16:45Z") == "2026-10-21"
    assert bjt_date("2026-10-20T15:59Z") == "2026-10-20"
    assert bjt_date("2026-10-20T16:00:00+00:00") == "2026-10-21"


def test_bjt_date_rejects_garbage_without_guessing():
    """解析不了 ⇒ None（不猜）。"""
    assert bjt_date(None) is None
    assert bjt_date("") is None
    assert bjt_date("not-a-date") is None
    assert match_date_bjt("not-a-date") is None


def test_stable_match_key_prefers_english_names():
    """稳定键 = home_en|away_en；缺英文名时回退中文 match 名。"""
    assert stable_match_key({"home_en": "A FC", "away_en": "B FC", "match": "甲 vs 乙"}) == "A FC|B FC"
    assert stable_match_key({"match": "甲 vs 乙"}) == "甲 vs 乙"
    assert stable_match_key({}) == ""


def test_stars_and_conf_bands():
    """星级解析与置信度分档。"""
    assert _stars_of({"stars": "2-star"}) == 2
    assert _stars_of({"stars": "3 star"}) == 3
    assert _stars_of({"stars": ""}) == 0
    assert _stars_of({}) == 0
    assert _conf_band({"confidence_score": 0.2}) == "<0.4"
    assert _conf_band({"confidence_score": 0.5}) == "0.4-0.6"
    assert _conf_band({"confidence_score": 0.9}) == ">0.6"
    assert _conf_band({"confidence_score": "x"}) == "未知"
    assert _conf_band({}) == "未知"


def test_direction_hit_matches_legacy_semantics():
    """方向判定与老实现同义（主胜/平/客胜）。"""
    assert _direction_hit({"direction": "主队 胜", "home": "主队", "away": "客队"}, 2, 1) is True
    assert _direction_hit({"direction": "主队 胜", "home": "主队", "away": "客队"}, 1, 2) is False
    assert _direction_hit({"direction": "平局", "home": "主队", "away": "客队"}, 1, 1) is True
    assert _direction_hit({"direction": "客队 胜", "home": "主队", "away": "客队"}, 0, 1) is True


def test_team_alias_covers_known_gaps():
    """别名表能覆盖「通用叫法 ↔ 竞彩官方叫法」这几处实测差异。"""
    assert "曼联" in canon_names("曼彻斯特联")
    assert teams_match("曼联", "利物浦", "曼彻斯特联", "利物浦")
    assert teams_match("曼彻斯特城", "埃弗顿", "曼城", "埃弗顿")
    assert not teams_match("利物浦", "切尔西", "曼彻斯特联", "埃弗顿")


def test_team_alias_table_is_not_empty():
    assert len(alias_table()) >= 5


def test_before_kickoff_handles_naive_kickoff_and_aware_close():
    """⚠️ 时区陷阱单测（实测踩过）：`kickoff_bj` 是 naive（BJT 墙上时钟）、
    `close_ts` 是 aware（timestamptz）⇒ 直接比会抛 TypeError，
    在 SQL 里比又会按会话时区凭空多算/少算 8 小时。

    这里锁住：naive 的 kickoff 必须按 **BJT** 解释。
    """
    from datetime import datetime, timezone

    from market.clv_calc import _before_kickoff

    # 开球 2026-10-22 02:00 **BJT**（naive）= 2026-10-21 18:00 UTC
    kickoff_naive = datetime(2026, 10, 22, 2, 0, 0)
    # 收盘 2026-10-21 17:00 UTC = 2026-10-22 01:00 BJT ⇒ 收盘在开球前 1 小时
    close_before = datetime(2026, 10, 21, 17, 0, 0, tzinfo=timezone.utc)
    # 收盘 2026-10-21 19:00 UTC = 2026-10-22 03:00 BJT ⇒ 收盘在开球后 1 小时
    close_after = datetime(2026, 10, 21, 19, 0, 0, tzinfo=timezone.utc)

    assert _before_kickoff(close_before, kickoff_naive) is True
    assert _before_kickoff(close_after, kickoff_naive) is False
    # 缺任一时刻 ⇒ 不拦（不凭空否掉一场）
    assert _before_kickoff(None, kickoff_naive) is True
    assert _before_kickoff(close_before, None) is True


def test_clv_sql_selects_league_key_from_archive_not_match():
    """`model.pred_archive_match` **没有** league_key 列（它在 `pred_archive` 上）。

    SQL 若写成 `am.league_key` ⇒ 运行时 column does not exist ⇒ 整个 CLV 管线起不来。
    """
    from market import clv_calc

    src_sql = " ".join(clv_calc._SRC_SQL.split())
    assert "am.league_key" not in src_sql
    assert "ar.league_key" in src_sql


# ── B. 分层命中率（mock 预测文件）──────────────────────────────────────

def _doc(league: str, preds: list[dict], scores: list[str] | None = None) -> dict:
    """造一份「已结算」文档：``past_matches`` 带比分 ⇒ 与 predictions 按稳定键对上。

    ``scores[i]`` 是第 i 场的比分（'2-1'）；缺省按「主队赢」造。
    """
    past = []
    for i, p in enumerate(preds):
        score = scores[i] if scores and i < len(scores) else "2-1"
        past.append({"home": p["home"], "away": p["away"], "home_en": p["home_en"],
                     "away_en": p["away_en"], "score": score})
    return {"path": Path(f"prediction_x_{league}.json"), "data": {
        "league": league, "predictions": preds, "past_matches": past}, "mtime": 9e9}


def _pred(home: str, away: str, direction: str, stars: str, conf: float) -> dict:
    return {"home": home, "away": away, "home_en": f"{home}EN", "away_en": f"{away}EN",
            "direction": direction, "stars": stars, "confidence_score": conf}


def _patch_docs(monkeypatch, docs):
    import web.services.accuracy as acc
    monkeypatch.setattr(acc, "load_prediction_docs", lambda: docs)


def test_stratified_groups_by_stars_and_confidence(monkeypatch):
    """分层：按星级与置信度各出一组，每组带 n 与区间。"""
    preds = [
        _pred("A", "B", "A 胜", "3-star", 0.8),
        _pred("C", "D", "C 胜", "3-star", 0.9),
        _pred("E", "F", "E 胜", "1-star", 0.2),
    ]
    # 比分：前两场主队赢（命中），第三场主队输（未命中）
    _patch_docs(monkeypatch, [_doc("epl", preds, ["2-1", "3-0", "0-1"])])
    out = league_accuracy_stratified("epl", days=30)
    assert out is not None
    assert out["reconciled"] == 3
    assert out["direction_accuracy"] == pytest.approx(2 / 3, abs=0.001)
    assert set(out["by_stars"]) == {"1", "3"}
    assert out["by_stars"]["3"]["n"] == 2
    assert out["by_stars"]["3"]["rate"] == 1.0
    assert out["by_stars"]["1"]["n"] == 1
    assert out["by_stars"]["1"]["rate"] == 0.0
    assert out["by_stars"]["1"]["insufficient"] is True      # n < 5
    assert out["by_stars"]["3"]["insufficient"] is True        # n = 2 也 < 5
    assert set(out["by_confidence"]) == {"<0.4", ">0.6"}
    lo, hi = out["direction_ci95"]
    assert 0.0 <= lo <= hi <= 1.0


def test_stratified_returns_none_without_samples(monkeypatch):
    """没有已结算样本 ⇒ None（页面显示「数据积累中」，不编数）。"""
    _patch_docs(monkeypatch, [_doc("epl", [])])
    assert league_accuracy_stratified("epl", days=30) is None


# ── C. 写手（mock 连接）───────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, sink): self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append((" ".join(sql.split()), params))
        return _FakeResult()


class _FakeResult:
    def fetchone(self): return (999,)
    def fetchall(self): return []


class _FakeConn:
    def __init__(self): self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return _FakeResult()

    def cursor(self): return _FakeCursor(self.calls)


def test_archive_writes_expected_fields():
    """归档写入的字段：比赛日（BJT）、模型概率、竞彩关联 id。"""
    from model.archive import archive_output

    conn = _FakeConn()
    out = {
        "generated_at": "2026-10-20T09:30:00+08:00",
        "data_window": "20260920-20261103",
        "predictions": [{
            "home": "曼彻斯特城", "away": "阿森纳", "home_en": "Man City", "away_en": "Arsenal",
            "kickoff_utc": "2026-10-21T12:00Z", "direction": "曼彻斯特城 胜", "stars": "3-star",
            "confidence_score": 0.72, "low_confidence": False,
            # 模型最终概率（p_model 的正确来源）
            "probs": {"h": 0.6, "d": 0.25, "a": 0.15},
            # 顶层 *_true_prob 是**市场先验**（ESPN 赔率去水），不得被采用 —— 数值故意不同
            "home_true_prob": 0.4, "draw_true_prob": 0.3, "away_true_prob": 0.3,
            "predicted_score": "2-1",
        }],
    }
    res = archive_output(conn, "epl", out, commit="abc123")
    assert res["n_matches"] == 1
    match_sql = [c for c in conn.calls if "pred_archive_match" in c[0] and c[0].startswith("INSERT")]
    assert match_sql, "应当写一条 pred_archive_match"
    params = match_sql[0][1]
    # match_date = BJT 日历日（UTC 12:00 → 当日 20:00 BJT，仍是 10-21）
    assert params[2] == "2026-10-21"
    assert params[4] == "曼彻斯特城" and params[5] == "阿森纳"
    assert params[12] == 0.6 and params[13] == 0.25 and params[14] == 0.15
    assert params[10] == 0.72 and params[11] is False


def test_archive_uses_probs_not_market_prior():
    """⚠️ 核心不变量：`p_model` 只能是**模型自己的输出**。

    顶层 `*_true_prob` 是 `predictor.py::_ODDS_PROPAGATION_FIELDS` 从 `match` 透传的
    **市场先验**（ESPN 赔率去水）⇒ 拿它当 p_model 算 CLV 就是「拿市场价比市场价」。
    实测对照：顶层 0.4807/0.6613 vs `probs.h` 0.4700/0.5817 —— 确实不同。
    """
    from model.archive import archive_output

    conn = _FakeConn()
    out = {"generated_at": "2026-10-20T09:30:00+08:00", "predictions": [{
        "home": "A", "away": "B", "home_en": "AEN", "away_en": "BEN",
        "kickoff_utc": "2026-10-21T12:00Z",
        "probs": {"h": 0.6, "d": 0.25, "a": 0.15},
        "home_true_prob": 0.4, "draw_true_prob": 0.3, "away_true_prob": 0.3,
    }]}
    archive_output(conn, "epl", out)
    params = [c for c in conn.calls if "pred_archive_match" in c[0] and c[0].startswith("INSERT")][0][1]
    assert (params[12], params[13], params[14]) == (0.6, 0.25, 0.15)   # 取 probs，不取市场先验


def test_archive_falls_back_to_reasoning_factors_prob_weighted():
    """2026-10-14 之前的历史预测文件没有 `probs`，但有 `reasoning_factors.*_prob_weighted`
    （ML 关闭时与 `probs` 同值、3 位精度）⇒ 允许回退，别把样本白白丢掉。"""
    from model.archive import archive_output

    conn = _FakeConn()
    out = {"generated_at": "2026-10-20T09:30:00+08:00", "predictions": [{
        "home": "A", "away": "B", "home_en": "AEN", "away_en": "BEN",
        "kickoff_utc": "2026-10-21T12:00Z",
        "reasoning_factors": {"home_prob_weighted": 0.5, "draw_prob_weighted": 0.3,
                              "away_prob_weighted": 0.2},
    }]}
    archive_output(conn, "epl", out)
    params = [c for c in conn.calls if "pred_archive_match" in c[0] and c[0].startswith("INSERT")][0][1]
    assert (params[12], params[13], params[14]) == (0.5, 0.3, 0.2)


def test_archive_skips_rows_without_probabilities():
    """模型概率缺失 ⇒ 概率列留 NULL（不拿市场隐含概率顶替）。"""
    from model.archive import archive_output

    conn = _FakeConn()
    out = {"generated_at": "2026-10-20T09:30:00+08:00",
           "predictions": [{"home": "A", "away": "B", "home_en": "AEN", "away_en": "BEN",
                            "kickoff_utc": "2026-10-21T12:00Z",
                            "home_ml_implied": 0.5, "draw_implied": 0.3}]}
    archive_output(conn, "epl", out)
    match_sql = [c for c in conn.calls if "pred_archive_match" in c[0] and c[0].startswith("INSERT")]
    params = match_sql[0][1]
    assert params[12] is None and params[13] is None and params[14] is None


def test_archive_ignores_market_prior_only_rows():
    """只有市场先验、没有模型输出 ⇒ 概率列必须留 NULL（宁可该场不参与 CLV）。"""
    from model.archive import archive_output

    conn = _FakeConn()
    out = {"generated_at": "2026-10-20T09:30:00+08:00", "predictions": [{
        "home": "A", "away": "B", "home_en": "AEN", "away_en": "BEN",
        "kickoff_utc": "2026-10-21T12:00Z",
        "home_true_prob": 0.4, "draw_true_prob": 0.3, "away_true_prob": 0.3,
    }]}
    archive_output(conn, "epl", out)
    params = [c for c in conn.calls if "pred_archive_match" in c[0] and c[0].startswith("INSERT")][0][1]
    assert params[12] is None and params[13] is None and params[14] is None


def test_archive_no_predictions_writes_nothing():
    """空产出不写行（空归档没有意义）。"""
    from model.archive import archive_output

    conn = _FakeConn()
    res = archive_output(conn, "epl", {"generated_at": "x", "predictions": []})
    assert res["archive_id"] is None
    assert conn.calls == []


def test_clv_calc_cli_has_entry_point():
    """⚠️ CLI 入口不能少（2026-10-21 生产踩过）。

    少了 `if __name__ == "__main__"` ⇒ `python scripts/market/clv_calc.py` 会**静默什么都不做**：
    退出码 0、零输出、零写入 —— 看起来像「跑了但没数据」，实际上一行代码都没执行。
    ⇒ 用 `--help` 做行为级断言：必须打印 usage。
    """
    import os
    import subprocess

    repo = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo / "scripts")
    proc = subprocess.run(
        [sys.executable, str(repo / "scripts" / "market" / "clv_calc.py"), "--help"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "usage" in (proc.stdout or "").lower(), f"没有 usage 输出：{proc.stdout!r}"


def test_clv_summary_aggregates_across_days_despite_small_rows():
    """⚠️ 逐日 n<5（每行都带 `insufficient`）**不能**把整体均值一起抹掉。

    实测（2026-10-21 生产首跑）：31 个逐日单元的 n 都是 1~4 ⇒ 每行 `insufficient=True`；
    若按逐行标记过滤，整体均值变 null（**总样本 71 场却不显示**）⇒ 看板永远一片空白。
    ⇒ 整体口径按**总场次**判不足。
    """
    from web.services.clv import summary

    rows = [{"date": f"2026-10-{d:02d}", "league_key": "epl", "n_matches": 2, "n_options": 6,
             "mean_clv": 0.05, "median_clv": 0.05, "min_clv": -0.1, "max_clv": 0.2,
             "insufficient": True}
            for d in range(1, 6)]
    out = summary(rows)
    assert out["n_days"] == 5
    assert out["n_matches"] == 10
    assert out["mean_clv"] == pytest.approx(0.05, abs=1e-4)
    assert out["insufficient"] is False


def test_clv_summary_withholds_mean_when_total_n_too_small():
    """总场次不足 ⇒ 不给均值（宁可空白不误导）。"""
    from web.services.clv import summary

    rows = [{"date": "2026-10-01", "league_key": "epl", "n_matches": 2, "n_options": 6,
             "mean_clv": 0.05, "median_clv": 0.05, "min_clv": -0.1, "max_clv": 0.2,
             "insufficient": True}]
    out = summary(rows)
    assert out["n_matches"] == 2
    assert out["mean_clv"] is not None      # 值仍算出来，但 insufficient=True
    assert out["insufficient"] is True      # 页面据此不显示


# ── D. 真库全链（表未建则 skip）────────────────────────────────────────

class _FakeClvConn:
    """`compute_clv` 的假连接：按 SQL 关键字分派，覆盖「候选 → 收盘 → 落明细 → 日汇总」全链。

    存在的意义：2026-10-21 首次在生产跑这条管线时炸了 ``KeyError: 'p_home'``
    （选项码与概率键对不上）—— 纯函数单测看不见它，只有**把整条链跑一遍**才炸得出来。
    """

    def __init__(self, src_rows, close_rows, daily_count=1):
        self.src_rows = src_rows
        self.close_rows = close_rows
        self.daily_count = daily_count
        self.inserted = []
        self.daily_writes = 0

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        if "FROM model.pred_archive_match" in flat:
            return _FakeRows(self.src_rows)
        if "FROM fact.jc_odds_history" in flat:
            return _FakeRows(self.close_rows)
        if flat.startswith("INSERT INTO analysis.clv_match"):
            self.inserted.append(params)
            return _FakeRows([])
        if flat.startswith("INSERT INTO analysis.clv_daily"):
            self.daily_writes += 1
            return _FakeRows([])
        if "SELECT count(*) FROM analysis.clv_daily" in flat:
            return _FakeRows([(self.daily_count,)])
        return _FakeRows([])


class _FakeRows:
    def __init__(self, rows): self.rows = rows

    def fetchall(self): return self.rows

    def fetchone(self): return self.rows[0] if self.rows else None


def _clv_src_row(*, run_at, kickoff_bj, jc_match_id=2041853, p=(0.5, 0.3, 0.2)):
    """`model.pred_archive_match` + `model.pred_archive` join 出来的一行（顺序同 `_SRC_SQL`）。"""
    return (9, "A FC|B FC", "2026-10-21", "epl", jc_match_id,
            p[0], p[1], p[2], run_at, kickoff_bj)


def test_compute_clv_writes_one_row_per_option():
    """一条归档 + 一份收盘 ⇒ 落 3 行明细（h/d/a），CLV 数值 = ln(p_model/p_close)。"""
    import math
    from datetime import datetime, timezone

    from market import clv_calc

    run_at = datetime(2026, 10, 21, 1, 0, 0, tzinfo=timezone.utc)
    kickoff = datetime(2026, 10, 22, 2, 0, 0)          # naive，BJT 墙上时钟
    close_ts = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)  # = BJT 10-21 18:00
    odds = {"h": "2.00", "d": "3.50", "a": "4.00"}
    conn = _FakeClvConn(
        src_rows=[_clv_src_row(run_at=run_at, kickoff_bj=kickoff)],
        close_rows=[(2041853, 8, close_ts, odds)])

    stats = clv_calc.compute_clv(conn)
    assert stats["n_candidates"] == 1
    assert stats["n_matched"] == 1
    assert stats["n_rows"] == 3
    assert len(conn.inserted) == 3
    codes = {p[5] for p in conn.inserted}
    assert codes == {"h", "d", "a"}
    # 去水概率：1/2 + 1/3.5 + 1/4 = 0.5+0.2857+0.25 = 1.0357 ⇒ p_close(h) = 0.4828
    raw = {"h": 1 / 2.0, "d": 1 / 3.5, "a": 1 / 4.0}
    total = sum(raw.values())
    for p in conn.inserted:
        code, p_model, p_close = p[5], float(p[6]), float(p[7])
        assert p_model == pytest.approx({"h": 0.5, "d": 0.3, "a": 0.2}[code])
        assert p_close == pytest.approx(raw[code] / total, abs=1e-6)
        assert float(p[8]) == pytest.approx(math.log(p_model / p_close), abs=1e-9)


def test_compute_clv_skips_when_close_is_after_kickoff():
    """收盘晚于开球 ⇒ 该场跳过（不落明细），n_skipped 计数。"""
    from datetime import datetime, timezone

    from market import clv_calc

    run_at = datetime(2026, 10, 21, 1, 0, 0, tzinfo=timezone.utc)
    kickoff = datetime(2026, 10, 22, 2, 0, 0)               # BJT 10-22 02:00
    close_ts = datetime(2026, 10, 21, 19, 0, 0, tzinfo=timezone.utc)  # BJT 10-22 03:00 ⇒ 晚于开球
    conn = _FakeClvConn(
        src_rows=[_clv_src_row(run_at=run_at, kickoff_bj=kickoff)],
        close_rows=[(2041853, 8, close_ts, {"h": "2.00", "d": "3.50", "a": "4.00"})])

    stats = clv_calc.compute_clv(conn)
    assert stats["n_matched"] == 0
    assert stats["n_rows"] == 0
    assert stats["n_skipped"] == 1
    assert conn.inserted == []


@pytest.fixture()
def db_conn():
    try:
        from store import pg
        connection = pg.connect("ing")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no db: {type(exc).__name__}: {exc}")
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _require_tables(conn, names):
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = ANY(%s) AND table_name = ANY(%s)",
        (["model", "analysis"], list(names))).fetchall()
    have = {r[0] for r in rows}
    missing = [n for n in names if n not in have]
    if missing:
        pytest.skip(f"建议 3 的表未建（先执行 docs/db/suggestion3_schema.sql）：{missing}")


def test_clv_end_to_end_on_real_db(db_conn):
    """全链：造归档行 + 造收盘赔率 → compute_clv → analysis.clv_match/clv_daily 有值且 CLV 数值正确。"""
    _require_tables(db_conn, ["pred_archive", "pred_archive_match", "clv_match", "clv_daily"])
    from market import clv_calc

    tag = "suggestion3-test"
    archive_id = db_conn.execute(
        "INSERT INTO model.pred_archive (league_key, run_at, generated_at, commit, data_window, n_matches)"
        " VALUES (%s, now(), now() - interval '2 days', %s, %s, 1) RETURNING archive_id",
        (tag, tag, "20261001-20261015")).fetchone()[0]
    db_conn.execute(
        "INSERT INTO model.pred_archive_match (archive_id, match_key, match_date, home_cn, away_cn,"
        " p_home, p_draw, p_away, jc_match_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (archive_id, f"{tag}|A", "2026-10-01", "主队", "客队", 0.5, 0.3, 0.2, 999000001))

    stats = clv_calc.compute_clv(db_conn, date_from="2026-10-01", date_to="2026-10-01")
    # 没有收盘赔率 ⇒ 跳过（不编数）
    assert stats["n_candidates"] >= 1
    assert stats["n_skipped"] >= 1
    assert stats["n_rows"] == 0
