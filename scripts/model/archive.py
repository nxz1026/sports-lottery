"""建议 3 子项 3：把每次 predict.py 的产出**归档**进 model.pred_archive / model.pred_archive_match。

为什么必须另起一套表（不是复用 model.pred_run / model.pred_fixture）：
  - `model.pred_run` 现在只有 10 行，全是 2026-09-15 的一次性赛季回测；
  - `model.pred_fixture.fixture_id` 是 `fact.fixture` 的外键，而**当日 ESPN/竞彩场次不在
    `fact.fixture` 里**（那张表装的是 API-Football 的 2022–2024 赛季训练集）⇒ 复用会插不进。
  ⇒ 「当日归档」与「赛季回测」是两套语义，必须分表。

这个模块是 **CLV 管线的前半段**：没有它，CLV 无从谈起 —— 路线图 §6.5.3 实测：
`analysis.backtest_market`（175,200 行）的 fixture_id 与竞彩 match_id **日期零重叠，join = 0 行**。

幂等：同一 (league_key, generated_at) 只留一行（重跑 predict.py 不产生重复归档）。
事务：调用方持有连接与事务（与 `store/upsert_*` 同一约定）。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from core.bjt import BJT, bjt_date
from core.log import logger
from core.team_alias import teams_match
from psycopg import Connection
from psycopg.types.json import Jsonb

_RUN_SQL = (
    "INSERT INTO model.pred_archive (league_key, run_at, generated_at, commit, data_window,"
    " n_matches, source_file, params) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (league_key, generated_at) DO UPDATE"
    " SET run_at = EXCLUDED.run_at, commit = EXCLUDED.commit, data_window = EXCLUDED.data_window,"
    "     n_matches = EXCLUDED.n_matches, source_file = EXCLUDED.source_file, params = EXCLUDED.params"
    " RETURNING archive_id")

_MATCH_SQL = (
    "INSERT INTO model.pred_archive_match (archive_id, match_key, match_date, kickoff_utc,"
    " home_cn, away_cn, home_en, away_en, direction, stars, confidence_score, low_confidence,"
    " p_home, p_draw, p_away, predicted_score, jc_match_id)"
    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    " ON CONFLICT (archive_id, match_key) DO UPDATE SET"
    " match_date = EXCLUDED.match_date, kickoff_utc = EXCLUDED.kickoff_utc,"
    " home_cn = EXCLUDED.home_cn, away_cn = EXCLUDED.away_cn,"
    " direction = EXCLUDED.direction, stars = EXCLUDED.stars,"
    " confidence_score = EXCLUDED.confidence_score, low_confidence = EXCLUDED.low_confidence,"
    " p_home = EXCLUDED.p_home, p_draw = EXCLUDED.p_draw, p_away = EXCLUDED.p_away,"
    " predicted_score = EXCLUDED.predicted_score, jc_match_id = EXCLUDED.jc_match_id")

# 竞彩场次（用于把归档行关联到竞彩官方 match_id）
_JC_SQL = ("SELECT match_id, home_cn, away_cn, kickoff_bj FROM fact.jc_match"
           " WHERE business_date BETWEEN %s AND %s")


def match_date_bjt(kickoff_utc: str | None) -> str | None:
    """开球时刻 → **BJT 日历日**（'YYYY-MM-DD'）。

    口径与 web 层一致（`web/services/store.py::bjt_today` 用 `Asia/Shanghai`）⇒
    「今天/明天」这类日口径在引擎侧与 web 侧不会差一天。
    解析不了就返回 None（**不猜**），由调用方决定跳过。实现见 `core/bjt.py`。
    """
    return bjt_date(kickoff_utc)


def stable_match_key(rec: dict) -> str:
    """稳定键：英文原名优先（home_en|away_en），回退中文 match 名。

    与 `scripts/core/backtest._bk_stable_key` / `web/services/store._stable_match_key` 同语义。
    """
    h = str(rec.get("home_en") or "").strip()
    a = str(rec.get("away_en") or "").strip()
    if h and a:
        return f"{h}|{a}"
    return str(rec.get("match") or rec.get("name") or "").strip()


def _as_prob(v: Any) -> float | None:
    """数值校验：bool / 非数值 / 非有限 / 越界 ⇒ None（不猜、不补默认值）。"""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if not math.isfinite(f) or not 0.0 <= f <= 1.0:
        return None
    return f


# probs 的键 → reasoning_factors 里的键前缀
_PROBS_PREFIX = {"h": "home", "d": "draw", "a": "away"}


def _prob(rec: dict, code: str) -> float | None:
    """取**模型自己的**概率（code ∈ 'h'/'d'/'a'）。

    ⚠️⚠️ **绝不能用顶层 `home_true_prob` / `draw_true_prob` / `away_true_prob`**：
    那些是 `predictor.py::_ODDS_PROPAGATION_FIELDS` 从 `match` **透传**过来的
    **市场先验**（ESPN 赔率去水后的概率，不是模型输出）。
    实测对照（2026-10-14 bundesliga 首行）：顶层 true_prob=0.4807，而模型输出 probs.h=0.4700；
    另一行顶层 0.6613 vs probs.h=0.5817 —— 两者确实不同。
    拿市场先验当 p_model 算 CLV ⇒ 等于拿市场价跟市场价比（循环论证），CLV 会退化成
    「开盘到收盘的市场漂移」，测不到模型的任何价值。

    ⇒ 只取两处**模型自己的**输出：
      ① `probs.{h,d,a}` —— 最终方向概率（predictor.py `_assemble_result` 写入，含 ML 融合）；
      ② `reasoning_factors.{home,draw,away}_prob_weighted` —— 加权概率（ML 关闭时与 ① 同值，
         只是 3 位精度；用于 2026-10-14 之前那批没有 `probs` 的历史预测文件）。

    两者都没有 ⇒ 返回 None（该场不参与 CLV，**不拿别的数顶替**）。
    """
    probs = rec.get("probs") or {}
    if isinstance(probs, dict):
        f = _as_prob(probs.get(code))
        if f is not None:
            return f
    rf = rec.get("reasoning_factors") or {}
    if isinstance(rf, dict):
        return _as_prob(rf.get(f"{_PROBS_PREFIX[code]}_prob_weighted"))
    return None


def _jc_index(conn: Connection, day_from: str, day_to: str) -> list[tuple[int, str, str]]:
    """取竞彩场次（match_id, home_cn, away_cn）供按名关联。"""
    try:
        rows = conn.execute(_JC_SQL, (day_from, day_to)).fetchall()
    except Exception as exc:  # noqa: BLE001 - 关联失败不该毁掉归档本身
        logger.warning("archive: 读 fact.jc_match 失败（归档继续，只是 jc_match_id 留空）：%s", exc)
        return []
    return [(r[0], r[1] or "", r[2] or "") for r in rows]


def _match_jc(rows: list[tuple[int, str, str]], home: str, away: str) -> int | None:
    """按名关联竞彩场次：精确优先，再走别名表。

    关联不上就返回 None —— **不硬凑**（错配会污染 CLV 均值）。
    """
    for mid, hc, ac in rows:
        if home and away and home == hc and away == ac:
            return mid
    for mid, hc, ac in rows:
        if teams_match(home or "", away or "", hc, ac):
            return mid
    return None


def archive_output(conn: Connection, league_key: str, output: dict[str, Any], *,
                   commit: str | None = None, source_file: str | None = None) -> dict[str, Any]:
    """把一个联赛的一次预测产出归档。

    返回 ``{"archive_id": int, "n_matches": int, "n_jc_linked": int}``。
    产出里没有预测场次时**不写行**（返回 archive_id=None）—— 空归档没有意义。
    """
    preds = output.get("predictions") or []
    if not preds:
        return {"archive_id": None, "n_matches": 0, "n_jc_linked": 0}

    generated_at = str(output.get("generated_at") or "")
    try:
        gen_dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        gen_dt = datetime.now(BJT)
    if gen_dt.tzinfo is None:
        gen_dt = gen_dt.replace(tzinfo=BJT)

    # 关联窗口：以本次运行的预测场次覆盖的日期为准（±1 天容错，避免边界漏关联）
    days = [d for d in (match_date_bjt(p.get("kickoff_utc")) for p in preds) if d]
    if days:
        day_from = (datetime.fromisoformat(min(days)).date() - timedelta(days=1)).isoformat()
        day_to = (datetime.fromisoformat(max(days)).date() + timedelta(days=1)).isoformat()
    else:
        day_from = (gen_dt.date() - timedelta(days=1)).isoformat()
        day_to = (gen_dt.date() + timedelta(days=1)).isoformat()
    jc_rows = _jc_index(conn, day_from, day_to)

    params = {
        "league_key": league_key,
        "data_window": output.get("data_window"),
        "tournament_type": output.get("tournament_type"),
        "data_source": output.get("data_source"),
        "n_predictions": len(preds),
    }
    archive_id = conn.execute(
        _RUN_SQL, (league_key, gen_dt, gen_dt, commit, output.get("data_window"),
                   len(preds), source_file, Jsonb(params))).fetchone()[0]

    n_linked = 0
    for p in preds:
        home = str(p.get("home") or "")
        away = str(p.get("away") or "")
        key = stable_match_key(p)
        if not key or not home or not away:
            continue
        jc_id = _match_jc(jc_rows, home, away)
        if jc_id is not None:
            n_linked += 1
        conn.execute(_MATCH_SQL, (
            archive_id, key, match_date_bjt(p.get("kickoff_utc")), p.get("kickoff_utc") or None,
            home, away, p.get("home_en") or None, p.get("away_en") or None,
            p.get("direction") or None, p.get("stars") or None,
            p.get("confidence_score"), bool(p.get("low_confidence")),
            _prob(p, "h"), _prob(p, "d"), _prob(p, "a"),
            p.get("predicted_score") or None, jc_id))

    logger.info("archive: %s 归档 %d 场（关联竞彩 %d 场）archive_id=%s",
                league_key, len(preds), n_linked, archive_id)
    return {"archive_id": archive_id, "n_matches": len(preds), "n_jc_linked": n_linked}
