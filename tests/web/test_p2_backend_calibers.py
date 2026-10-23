"""P2-B（2026-10-23）：后端口径修正的回归锁（数字口径 + 时区口径 + 荐单口径）。

逐条对应 2026-10-23 三路审计（静态 + 生产动态运行 + web 逐元素）出的 24 条 P2 里，
分给「后端」这一组的 7 条：

  B1 · CLV 的「n=31 天」其实是 9 天 —— ``summary()`` 把（日期 × 联赛）的**行数**
        当成了「天数」⇒ 生产实测「31 行」被印成「31 天」。
  B2 · 「近 7 日命中率」取**第一个联赛**的值、联赛没数据时**回落 30d**，
        而页面副标签写「最近 7 场」⇒ 联赛范围 / 窗口 / 样本量三方全不一致。
  B3 · 「当日额度」按 **UTC 日**统计 ⇒ BJT 00:00~08:00 读到的还是昨天。
  B4 · 荐单候选池用 ``max(business_date)``，与路线图「``>= 今日业务日``」不一致
        ⇒ 库里一出现 D+1 的卡，今天还在售的场次被整批踢出池子。
  B5 · EV 阈值 5% 远低于实测水位 **12.9%**（几乎不筛），
        而用户可见文案写「水位 25~33%」（写错约 2 倍）。
  B6 · ``made_at`` 取归档的 ``run_at`` ⇒ 当天 09:30 定时跑**之前**手动出单会写进
        **昨天**的桶、覆盖昨天那一行。
  B7 · EV 最高侧若模型概率 < 10% 则**整场丢弃**；高 SP 冷门又可绕过「不要长尾彩票」
        ⇒ 两条规则互相打架。

测试写法照抄仓库既有范式：**假连接 / 假数据 / monkeypatch**
（``tests/test_suggestion4_decision.py`` 的 tuple 行假连接、
``tests/web/test_degraded_reads.py`` 的 read_conn 打桩），
⇒ **零真库、零真网络、零真锁**（B3 用 tmp_path 下的独立缓存目录）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from decision import recommend as rec  # noqa: E402

import web.services.clv as wsc  # noqa: E402
import web.services.recommend as wsr  # noqa: E402

# ══════════════════════════════════════════════════════════════════════
# B1 · CLV：「天数」必须按去重后的日期数算，行数是另一个量
# ══════════════════════════════════════════════════════════════════════


def _clv_row(date: str, league: str, n: int = 2, mean: float = 0.05) -> dict:
    return {"date": date, "league_key": league, "n_matches": n, "n_options": 6,
            "mean_clv": mean, "median_clv": mean, "min_clv": -0.1, "max_clv": 0.2,
            "insufficient": True}


def test_b1_clv_summary_n_days_is_distinct_dates_not_row_count():
    """9 个日期 × 若干联赛 ⇒ ``n_days`` = 9（**不是**行数）。

    旧实现 ``len(rows)`` ⇒ 生产实测「31 行」被印成「31 天」。
    """
    rows = [_clv_row("2026-10-20", "epl"), _clv_row("2026-10-20", "laliga"),
            _clv_row("2026-10-20", "bundesliga"), _clv_row("2026-10-21", "epl"),
            _clv_row("2026-10-21", "laliga"), _clv_row("2026-10-22", "epl"),
            _clv_row("2026-10-23", "epl"), _clv_row("2026-10-24", "epl"),
            _clv_row("2026-10-25", "epl"), _clv_row("2026-10-26", "epl"),
            _clv_row("2026-10-27", "epl"), _clv_row("2026-10-28", "epl")]
    out = wsc.summary(rows)
    assert out["n_days"] == 9, "天数必须按去重后的日期数算"
    assert out["n_rows"] == len(rows), "行数（日期 × 联赛 单元数）是另一个量，必须单独给"
    assert out["n_matches"] == 2 * len(rows)
    # 既有键语义不变：均值按总场次加权、insufficient 按总场次判
    assert out["mean_clv"] == pytest.approx(0.05, abs=1e-4)
    assert out["insufficient"] is False


def test_b1_clv_summary_empty_has_zero_days_and_rows():
    """空行 ⇒ ``n_days`` / ``n_rows`` 都是 0（两个量都要在）。"""
    out = wsc.summary([])
    assert out["n_days"] == 0
    assert out["n_rows"] == 0
    assert out["n_matches"] == 0
    assert out["mean_clv"] is None
    assert out["insufficient"] is True


# ══════════════════════════════════════════════════════════════════════
# B2 · 命中率：范围（全部联赛）、窗口（7d/30d）、样本量都必须能被读到
# ══════════════════════════════════════════════════════════════════════


def test_b2_aggregate_accuracy_covers_all_leagues_with_weighted_mean():
    """聚合口径 = **全部联赛** + 按样本量加权；范围与样本量显式给出。"""
    from web.routers.predictions import _aggregate_accuracy

    leagues = {
        "epl": {"7d": {"direction_accuracy": 0.50, "reconciled": 10}},
        "laliga": {"7d": {"direction_accuracy": 0.25, "reconciled": 30}},
    }
    out = _aggregate_accuracy(leagues)
    assert out["scope"] == "all_leagues"
    assert out["window"] == "7d"
    assert out["window_days"] == 7
    assert out["fallback"] is False
    assert out["n"] == 40
    assert out["n_leagues"] == 2
    assert out["leagues"] == ["epl", "laliga"]
    assert out["direction_accuracy"] == pytest.approx((0.50 * 10 + 0.25 * 30) / 40, abs=1e-4)


def test_b2_aggregate_accuracy_fallback_to_30d_is_explicit():
    """7d 无样本 ⇒ 改用 30d，但**必须显式标出**（window=30d + fallback=true）。"""
    from web.routers.predictions import _aggregate_accuracy

    leagues = {"epl": {"30d": {"direction_accuracy": 0.40, "reconciled": 20}}}
    out = _aggregate_accuracy(leagues)
    assert out["window"] == "30d"
    assert out["window_days"] == 30
    assert out["fallback"] is True, "回落 30d 必须显式标出，不许静默"
    assert out["n"] == 20
    assert out["direction_accuracy"] == pytest.approx(0.40, abs=1e-4)
    # 7d 窗口本身也要可读（n=0 ⇒ 调用方能看出「7d 没样本」）
    assert out["windows"]["7d"]["n"] == 0
    assert out["windows"]["7d"]["direction_accuracy"] is None


def test_b2_aggregate_accuracy_no_sample_returns_no_number():
    """一个样本都没有 ⇒ 不编数（window=None、命中率=None）。"""
    from web.routers.predictions import _aggregate_accuracy

    out = _aggregate_accuracy({"epl": {"7d": {"direction_accuracy": 0.5, "reconciled": 0}}})
    assert out["window"] is None
    assert out["direction_accuracy"] is None
    assert out["n"] == 0


def test_b2_accuracy_endpoint_exposes_overall_without_dropping_existing_keys(monkeypatch):
    """``/accuracy`` 新增 ``overall``；**既有键一个不少**（leagues / pending）。"""
    from web.routers import predictions as pr

    docs = {
        "epl": {"data": {"generated_at": "2026-10-23T09:30:00+08:00",
                         "data_window": "20261010-20261023",
                         "accuracy_summary": {"7d": {"direction_accuracy": 0.5, "reconciled": 10},
                                              "30d": {"direction_accuracy": 0.45, "reconciled": 40}}}},
        "laliga": {"data": {"accuracy_summary": {"7d": {"direction_accuracy": 0.25, "reconciled": 30}}}},
    }
    monkeypatch.setattr(pr.store, "latest_by_league", lambda: docs)
    monkeypatch.setattr(pr.store, "pending_predictions_by_league", lambda: {"epl": 3})

    out = pr.accuracy(request=None, _=None)
    assert set(out) >= {"leagues", "pending", "overall"}
    assert set(out["leagues"]) == {"epl", "laliga"}
    assert out["pending"] == {"epl": 3}
    assert out["overall"]["scope"] == "all_leagues"
    assert out["overall"]["window"] == "7d"
    assert out["overall"]["n"] == 40
    assert out["overall"]["direction_accuracy"] == pytest.approx((0.5 * 10 + 0.25 * 30) / 40, abs=1e-4)


def test_b2_accuracy_breakdown_endpoint_exposes_overall(monkeypatch):
    """``/accuracy/breakdown`` 同样带 ``overall``（前端 KPI 现在读它）。"""
    from web.routers import predictions as pr

    docs = {"epl": {"data": {"accuracy_summary": {"7d": {"direction_accuracy": 0.5, "reconciled": 10}}}}}
    monkeypatch.setattr(pr.store, "latest_by_league", lambda: docs)
    out = pr.accuracy_breakdown(request=None, _=None)
    assert "overall" in out
    assert out["overall"]["window"] == "7d"
    assert out["overall"]["n"] == 10
    assert out["leagues"]["epl"]["7d"]["direction_accuracy"] == 0.5   # 既有键没变


# ══════════════════════════════════════════════════════════════════════
# B3 · 「当日额度」按 BJT 自然日切（不是 UTC 日）
# ══════════════════════════════════════════════════════════════════════


def test_b3_api_football_quota_day_is_bjt_natural_day():
    """BJT 2026-10-24 00:30 ⇒ 当日额度记在 **10-24**（UTC 那天还是 10-23）。"""
    from web.services.apifootball import ApiFootballService

    # 2026-10-23T16:30:00Z == 2026-10-24 00:30 BJT（UTC 日仍是 10-23）
    ts = datetime(2026, 10, 23, 16, 30, tzinfo=timezone.utc).timestamp()
    assert datetime.fromtimestamp(ts, timezone.utc).date().isoformat() == "2026-10-23"
    svc = ApiFootballService(now=lambda: ts, cache_dir=Path("."))
    assert svc._bjt_day(ts) == "2026-10-24"


def test_b3_api_football_quota_file_uses_bjt_day_key(tmp_path):
    """额度账本里的 ``day`` 键 = BJT 日（BJT 00:00~08:00 不再读到昨天）。"""
    from web.services.apifootball import ApiFootballService

    ts = datetime(2026, 10, 23, 16, 30, tzinfo=timezone.utc).timestamp()  # BJT 10-24 00:30
    svc = ApiFootballService(now=lambda: ts, cache_dir=tmp_path)
    svc._guard_quota()
    state = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
    assert state["day"] == "2026-10-24"
    assert state["daily"] == 1


# ══════════════════════════════════════════════════════════════════════
# B4 · 候选池 = 在售场次（business_date >= 今日业务日）
# ══════════════════════════════════════════════════════════════════════


def test_b4_sale_sql_uses_today_business_date_not_max():
    """候选池 SQL 不许再用 ``max(business_date)``（会把今天在售的场次踢出池子）。"""
    sql = " ".join(rec._SALE_SQL.split())
    assert "max(business_date)" not in sql
    assert "m.business_date >= %s::date" in sql
    assert "m.match_status = 'Selling'" in sql


def test_b4_load_sale_matches_passes_bjt_today():
    """``load_sale_matches`` 把 **BJT 自然日** 当参数传下去（不依赖会话时区）。"""
    from core.bjt import now_bjt

    captured: dict = {}

    class _Cur:
        def fetchall(self):
            return []

    class _Conn:
        def execute(self, sql, params=None):
            captured["sql"] = str(sql)
            captured["params"] = params
            return _Cur()

    rec.load_sale_matches(_Conn())
    assert captured["params"] == (now_bjt().date().isoformat(),)

    captured.clear()
    rec.load_sale_matches(_Conn(), today="2026-10-23")
    assert captured["params"] == ("2026-10-23",)


# ══════════════════════════════════════════════════════════════════════
# B5 · EV 阈值与实测水位对齐（12.9%）；用户可见文案不许再写 25~33%
# ══════════════════════════════════════════════════════════════════════


def test_b5_min_ev_threshold_stays_a_deliberate_policy_not_the_water_level():
    """EV 阈值 = **政策值 0.05**，**不是**水位 12.9%。

    ⚠️ **2026-10-23 复核后的决定**（代数推导 + 生产实测）：
    设 m = 1+水位 = 三项隐含概率和，q = 去水后市场概率，SP = 1/(m·q)：
        单位 EV = p·SP − 1 = p/(m·q) − 1  ⇒  **EV > 0 ⟺ p/q > m**
    ⇒ 对**单边**投注，「扛掉这笔水」= **EV > 0**，**不是** EV ≥ 12.9%
      （EV ≥ 12.9% 等价于 p/q ≥ m² ≈ 1.273，要求模型比市场高出 27%）。
    ⇒ 且生产实测（analysis.ev_report，8 天全量）达标荐单 **EV 最低 0.0608**
      ⇒ 5% 门槛实际并没有放进负 EV 的荐单。
    ⇒ 把门槛抬到 12.9% 会把**正 EV 的好单**砍掉 50~70%（10-23：3 条 → 1 条），
      属**产品阈值政策**变更（路线图 §6.6.5.1 明确「阈值暂不动，等 n ≥ 100 再校准」）
      ⇒ **本轮不改阈值**，只订正**用户可见文案**（见 test_b5_pricing_note_*）。
    ⇒ 本测试锁住这个决定：阈值就是 0.05，且模块里必须**写着**这段口径
      （否则下一个改阈值的人又会以为「水位 = 门槛」）。
    """
    assert rec.MIN_EV == pytest.approx(0.05, abs=1e-9)
    _src = Path(rec.__file__).read_text(encoding="utf-8")
    assert "EV > 0" in _src, "模块里必须写清「单边扛水 = EV>0」的口径（防止再被当成 EV≥水位）"
    assert "12.9" in _src, "模块里必须记下实测水位 12.9%（出处）"
    assert "0.129" not in _src, "阈值不许偷偷变成 12.9%（那是另一个门槛）"


def test_b5_pricing_note_states_measured_water_level(monkeypatch):
    """用户可见文案：写实测值 12.9%，**不许**再写「25~33%」。"""
    monkeypatch.setattr("store.pg.read_conn", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("db down")))
    note = wsr.snapshot()["pricing_note"]
    assert "12.9%" in note
    assert "25~33%" not in note


def test_b5_write_reports_note_records_effective_thresholds():
    """荐单 note 里写清当时生效的门槛（EV / 模型概率下限）⇒ 可复算。"""
    captured: dict = {}

    class _Ret:
        def fetchone(self):
            return (1,)

    class _Conn:
        def execute(self, sql, params=None):
            captured["params"] = params
            return _Ret()

    picks = [{
        "match_id": 2042111, "option_code": "h", "sp": 3.0, "p": 0.55, "ev": 0.65,
        "kelly": 0.35, "stake": 8.75, "home_cn": "甲", "away_cn": "乙",
        "match_date": "2026-10-23", "league_key": "epl", "source": "archive",
        "sp_snapshot_ts": "2026-10-23 09:00:00+00", "decided_at": None,
        "min_ev": 0.129, "min_p": 0.10,
    }]
    rec.write_reports(_Conn(), picks)
    note = captured["params"][6]
    assert "0.129" in note
    assert "0.1" in note
    assert "长尾彩票" in note


# ══════════════════════════════════════════════════════════════════════
# B6 · made_at = 这条荐单真正被生成的时刻（BJT 墙钟）
# ══════════════════════════════════════════════════════════════════════


def _pick(decided_at: datetime | None, made_at: datetime | None = None) -> dict:
    pick = {
        "match_id": 2042111, "option_code": "h", "sp": 3.0, "p": 0.55, "ev": 0.65,
        "kelly": 0.35, "stake": 8.75, "home_cn": "甲", "away_cn": "乙",
        "match_date": "2026-10-23", "league_key": "epl", "source": "archive",
        "sp_snapshot_ts": "2026-10-23 09:00:00+00", "decided_at": decided_at,
        "made_at": made_at,
    }
    return pick


def test_b6_write_reports_made_at_is_now_not_archive_run_at():
    """归档 run_at 落在昨天 ⇒ ``made_at`` 仍必须是**今天**（不许落进昨天的桶）。"""
    from core.bjt import now_bjt

    captured: dict = {}

    class _Ret:
        def fetchone(self):
            return (1,)

    class _Conn:
        def execute(self, sql, params=None):
            captured["params"] = params
            return _Ret()

    # 昨天 09:30 BJT 的定时跑（= 昨天 01:30 UTC）
    run_at = datetime(2026, 10, 22, 1, 30, tzinfo=timezone.utc)
    rec.write_reports(_Conn(), [_pick(decided_at=run_at)])
    params = captured["params"]
    made_at = params[0]
    assert made_at.date() == now_bjt().date(), "made_at 必须落在今天（BJT），不能是昨天"
    assert made_at != run_at
    # decided_at（模型意见时刻）语义不变：仍是归档 run_at
    assert params[13] == run_at


def test_b6_insert_upsert_buckets_by_bjt_day():
    """唯一键按 **BJT 日** 分桶（同日覆盖同日，不跨日覆盖）。"""
    sql = " ".join(rec._INSERT_SQL.split())
    assert "(made_at AT TIME ZONE 'Asia/Shanghai')::date" in sql
    assert "ON CONFLICT (match_id, option_code," in sql


def test_b6_backfill_candidate_keeps_historical_day_bucket():
    """回溯模式：``made_at`` = 归档 run_at（历史重建按历史日分桶，不污染今天）。"""
    run_at = datetime(2026, 10, 20, 1, 30, tzinfo=timezone.utc)

    class _Cur:
        def fetchall(self):
            return [(2042111, 0.55, 0.25, 0.20, "3-star", 0.72, False,
                     "2026-10-21", "laliga", run_at, "甲", "乙",
                     {"h": "3.00", "d": "3.70", "a": "3.12"}, "2026-10-20 01:00:00+00")]

    class _Conn:
        def execute(self, sql, params=None):
            return _Cur()

    cands, _stats = rec.build_candidates(_Conn(), backfill=True)
    assert len(cands) == 1
    assert cands[0].made_at == run_at
    assert cands[0].decided_at == run_at


# ══════════════════════════════════════════════════════════════════════
# B7 · 荐单筛选：方向级合格 + 明确的长尾排除
# ══════════════════════════════════════════════════════════════════════


def _cand(p_home=0.45, p_draw=0.20, p_away=0.05, sp=(2.5, 4.0, 30.0)) -> rec.Candidate:
    return rec.Candidate(
        match_id=2042111, home_cn="甲", away_cn="乙", match_date="2026-10-23",
        sp={"h": sp[0], "d": sp[1], "a": sp[2]},
        p_model={"h": p_home, "d": p_draw, "a": p_away},
        league_key="epl", stars="3-star", confidence=0.7,
    )


def test_b7_long_tail_side_does_not_kill_the_whole_match():
    """EV 最高的一侧是长尾（p=0.05）⇒ **不许**把整场丢掉；合格的主胜照样推。

    三侧 EV：h=0.45×2.5−1=0.125 / d=0.20×4.0−1=−0.20 / a=0.05×30−1=0.50（最高，但 p<0.10）。
    旧实现：取 a ⇒ p<0.10 ⇒ **整场丢弃**（主胜根本没机会被评估）。
    """
    pick = rec.pick_recommendation(_cand(), min_ev=0.05, min_p=0.10)
    assert pick is not None
    assert pick["option_code"] == "h"
    assert pick["ev"] == pytest.approx(0.125, abs=1e-6)
    assert pick["sp"] == 2.5
    assert pick["p"] == pytest.approx(0.45)


def test_b7_long_tail_side_is_never_selected():
    """长尾（p < 下限）永远不进推荐：哪怕它的 EV 最大、SP 最高。"""
    # h: 0.30×2.0−1=−0.40 / d: 0.20×4.0−1=−0.20 / a: 0.05×30−1=0.50（EV 最高但 p=0.05）
    pick = rec.pick_recommendation(_cand(p_home=0.30, p_draw=0.20, p_away=0.05,
                                          sp=(2.0, 4.0, 30.0)),
                                    min_ev=0.05, min_p=0.10)
    assert pick is None


def test_b7_picked_side_odds_and_probability_are_consistent():
    """选中的方向与展示的赔率/概率口径一致（都取被选中的那一侧）。"""
    cand = _cand()
    pick = rec.pick_recommendation(cand, min_ev=0.05, min_p=0.10)
    assert pick is not None
    code = pick["option_code"]
    assert pick["sp"] == cand.sp[code]
    assert pick["p"] == cand.p_model[code]
    assert pick["ev"] == pytest.approx(pick["p"] * pick["sp"] - 1, abs=1e-6)
    # all_ev 给出三侧 EV（含未入选侧），便于复算
    assert set(pick["all_ev"]) == {"h", "d", "a"}


def test_b7_candidate_carries_thresholds_for_traceability():
    """推荐单带上当时生效的门槛（EV / 概率下限）⇒ 库里可复算。"""
    # 主胜 EV = 0.45×2.6−1 = 0.17 ≥ 0.129 ✔（用真实默认门槛验一遍）
    pick = rec.pick_recommendation(_cand(sp=(2.6, 4.0, 30.0)), min_ev=0.129, min_p=0.10)
    assert pick is not None
    assert pick["min_ev"] == pytest.approx(0.129)
    assert pick["min_p"] == pytest.approx(0.10)
    assert pick["made_at"] is None      # 今日模式：made_at 落库时取当下


def test_b7_default_threshold_is_the_policy_value():
    """默认门槛 = 政策值 ``MIN_EV``（0.05），**不是**实测水位 12.9%。

    ⚠️ 理由见 ``test_b5_min_ev_threshold_stays_a_deliberate_policy_not_the_water_level``：
    对单边投注「扛水」= EV > 0（代数：EV>0 ⟺ p/q > m），不是 EV ≥ 12.9%。
    """
    import inspect

    sig = inspect.signature(rec.pick_recommendation)
    assert sig.parameters["min_ev"].default == pytest.approx(rec.MIN_EV)
    assert sig.parameters["min_ev"].default == pytest.approx(0.05)


# ── 附加：候选装配在今日模式下不预设 made_at（落库时取当下）──────────


def test_b6_today_mode_candidate_has_no_preset_made_at():
    """今日模式：候选**不**带 made_at ⇒ 落库时取当下（今天）。"""
    from datetime import datetime as _dt

    fresh_snap = (_dt.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return list(self._rows)

    class _Conn:
        def execute(self, sql, params=None):
            text = " ".join(str(sql).split())
            if "fact.jc_match m" in text and "business_date" in text:
                return _Cur([(2042111, "2026-10-23", "甲", "乙", "西甲",
                              {"h": "3.00", "d": "3.70", "a": "3.12"}, fresh_snap)])
            if "model.pred_archive_match am" in text:
                return _Cur([(2042111, 0.55, 0.25, 0.20, "3-star", 0.72, False,
                              "2026-10-23", "laliga", "2026-10-23 01:30:02+00")])
            return _Cur([])

    orig = rec.latest_pred_rows
    rec.latest_pred_rows = lambda directory=None: {}
    try:
        cands, _stats = rec.build_candidates(_Conn(), backfill=False)
    finally:
        rec.latest_pred_rows = orig
    assert len(cands) == 1
    assert cands[0].made_at is None
    assert cands[0].decided_at is not None    # 模型意见时刻仍记归档 run_at
