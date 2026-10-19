"""P0-BACKTEST1 汇总用例（真库单事务，收尾 ROLLBACK）：fallback 分组 + SQL 孪生 vs Python 逐值对账 + T4。

脚手架（合成 run、四玩法合成概率、`conn` fixture）从 tests/test_model_backtest_db.py import —— 与
tests/test_upsert_from_raw.py ← tests/test_align.py 的同一先例；拆文件是为顶住 100 行机检上限。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from model import backtest  # noqa: E402
from store import query  # noqa: E402

# ruff: noqa: F401, F811  # conn 必须显式 import 才能被 pytest fixture 发现（无 conftest.py）
from tests.test_model_backtest_db import EPS_LOSS, build_scenario, conn  # noqa: E402


def test_report_splits_fallback_and_matches_python_metrics(conn):
    """⑥ 汇总：fit/兜底 两组四个玩法都在（各 n_fixtures=1），brier/log_loss/hit 与 score_options 逐值相等
    —— 这是"汇总 SQL 孪生 vs Python"的交叉对账；had 的 p=0 中奖把两边的 eps 口径也一起钉住。"""
    run, a, b = build_scenario(conn)
    backtest.run(conn, run)
    report = {(r["play_type"], r["fallback"]): r for r in query.backtest_report(conn, src_run=run)}
    assert len(report) == 8 and {fallback for _, fallback in report} == {True, False}
    had = report[("had", False)]  # fit 组＝2:1 那场：brier = 0.5²+0.5²+1²、log_loss = −ln(1e-6)、hit=0
    assert [had["n_fixtures"], had["brier_mean"], had["log_loss_mean"], had["hit_rate"]] == [
        1, pytest.approx(1.5), pytest.approx(EPS_LOSS), 0.0]
    for (play, fallback), row in report.items():
        rows = conn.execute("SELECT option_code, p_pred::float8, outcome FROM analysis.backtest_market"
                            " WHERE src_run = %s AND play_type = %s AND fixture_id = %s",
                            (run, play, b if fallback else a)).fetchall()
        sides = ("h", "a") if play == "jqc" else ("",)  # jqc 两侧各自计分 ⇒ 均值按侧别组平均
        scores = [backtest.score_options([(p, o) for code, p, o in rows if code.startswith(side)])
                  for side in sides]
        means = [sum(s[key] for s in scores) / len(scores) for key in ("brier", "log_loss", "hit")]
        assert [row["brier_mean"], row["log_loss_mean"], row["hit_rate"]] == pytest.approx(means)


def test_report_scope_is_empty_safe(conn):
    """T4：无行作用域返回 []（不做除零）；缺省＝全量照常返回 list（空库→[]，T5 之后→10 个 run）。"""
    assert query.backtest_report(conn, src_run=-1) == []
    assert isinstance(query.backtest_report(conn), list)
