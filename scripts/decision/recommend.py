"""建议 4 子项 ①②：生成「今日推荐单」→ `analysis.ev_report`。

口径（路线图 §6.6）：
1. **候选池 = 竞彩当前在售场次**（`fact.jc_match` 的当前期次、`match_status='Selling'`）
   —— 不是「模型预测的 14 天窗口」（实测：那些场次当时还没开盘，无价可评）。
2. **定价 = 竞彩官方 SP**（`fact.jc_offer` 的 had 快照 `options.h/d/a`）——
   不是 ESPN 赔率（现有 `ev_per_unit` 用的是 ESPN 赔率，那是另一个市场）。
3. **模型概率 = 模型自己的输出**（`model.pred_archive_match.p_*`；无归档时按名 join
   最新的预测文件兜底）。两处都取模型自己的 `probs`/加权概率，**绝不**取市场先验。
4. **每场最多推一侧**（EV 最高的那侧）：一场推多侧会自相矛盾（对冲），一场也只中一侧。
5. **EV / Kelly 公式唯一真源**：`scripts/market/kelly.py`（与 web 层 combo 共用）。
6. **保守**：默认 EV ≥ 5%、1/4 Kelly；并把「EV 含跨市场定价差」写进 note 与页面。

幂等：唯一键 `(match_id, option_code, made_at::date)`；重跑只更新价格与概率，不重复插行。

用法：
    PYTHONPATH=scripts python scripts/decision/recommend.py                 # 今日推荐单
    PYTHONPATH=scripts python scripts/decision/recommend.py --backfill       # 回溯历史推荐单
    PYTHONPATH=scripts python scripts/decision/recommend.py --dry            # 只报计划
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.bjt import now_bjt
from core.log import logger
from market.kelly import DEFAULT_FRACTION, ev_per_unit, kelly_fraction, suggested_stake
from psycopg import Connection
from psycopg.types.json import Jsonb
from store import pg

# ── 阈值（保守档；路线图 §6.6.3 第 1 条：模型概率是市场锚定的，EV 含跨市场定价差）────
MIN_EV = 0.05          # 单位 EV ≥ 5%
MIN_P = 0.10           # 模型概率 ≥ 10%（不要长尾彩票）
KELLY_FRACTION = DEFAULT_FRACTION   # 1/4 Kelly
BANKROLL = 100.0       # 建议注额的口径基数（与展示的「本钱 100」一致）

PREDICTIONS_DIR = Path(__file__).resolve().parents[1] / "predictions"

# 竞彩 had 的三项 → 模型概率键 / 推荐侧中文
_SIDES = (("h", "主胜"), ("d", "平"), ("a", "客胜"))

# 定价快照：官方 SP 是字符串
def _sp(v: Any) -> float | None:
    """竞彩 SP（字符串）→ float；非数值/<=1 ⇒ None。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 1.0 else None


@dataclass
class Candidate:
    """一个可推荐的场次（已配齐 SP 与模型概率）。"""

    match_id: int
    home_cn: str
    away_cn: str
    match_date: str | None
    sp: dict[str, float]
    p_model: dict[str, float]
    sp_snapshot_ts: str | None = None
    league_key: str | None = None
    stars: str | None = None
    confidence: float | None = None
    low_confidence: bool = False
    source: str = "archive"          # archive | predfile
    decided_at: datetime | None = None   # 决策时刻（回溯模式 = 归档 run_at）
    notes: list[str] = field(default_factory=list)


# ── 纯逻辑：候选 → 推荐单 ────────────────────────────────────────────
def pick_recommendation(cand: Candidate, *, min_ev: float = MIN_EV,
                        min_p: float = MIN_P,
                        fraction: float = KELLY_FRACTION,
                        bankroll: float = BANKROLL) -> dict[str, Any] | None:
    """从一场里挑出**该推的那一侧**；不够阈值 ⇒ None（不推）。

    「每场最多推一侧」：三侧里取单位 EV 最高的；EV 最高仍不过阈值 ⇒ 整场不推。
    """
    best: dict[str, Any] | None = None
    all_ev: dict[str, float | None] = {}
    for code, _cn in _SIDES:
        sp = cand.sp.get(code)
        p = cand.p_model.get(code)
        if sp is None or p is None:
            all_ev[code] = None
            continue
        e = ev_per_unit(p, sp)
        all_ev[code] = round(e, 6) if e is not None else None
        if e is None:
            continue
        if best is None or e > best["ev"]:
            best = {"code": code, "ev": e, "sp": sp, "p": p}
    if best is None:
        return None
    if best["ev"] < min_ev or best["p"] < min_p:
        return None
    k = kelly_fraction(best["p"], best["sp"])
    return {
        "match_id": cand.match_id,
        "option_code": best["code"],
        "sp": best["sp"],
        "p": best["p"],
        "ev": round(best["ev"], 6),
        "kelly": round(k, 6),
        "stake": suggested_stake(best["p"], best["sp"], fraction=fraction, bankroll=bankroll),
        "all_ev": all_ev,
        "home_cn": cand.home_cn,
        "away_cn": cand.away_cn,
        "match_date": cand.match_date,
        "league_key": cand.league_key,
        "stars": cand.stars,
        "confidence": cand.confidence,
        "low_confidence": cand.low_confidence,
        "source": cand.source,
        "sp_snapshot_ts": cand.sp_snapshot_ts,
        "decided_at": cand.decided_at,
        "notes": list(cand.notes),
    }


# ── DB：候选装配 ────────────────────────────────────────────────────
_SALE_SQL = """
SELECT m.match_id, m.business_date, m.home_cn, m.away_cn, m.league_cn,
       o.options, o.snap_ts
  FROM fact.jc_match m
  JOIN LATERAL (SELECT options, snap_ts FROM fact.jc_offer x
                  WHERE x.match_id = m.match_id AND x.play_type = 'had'
                  ORDER BY x.snap_ts DESC LIMIT 1) o ON TRUE
 WHERE m.business_date = (SELECT max(business_date) FROM fact.jc_match)
   AND m.match_status = 'Selling'
 ORDER BY m.match_id
"""

_ARCHIVE_PROBS_SQL = """
SELECT DISTINCT ON (am.jc_match_id)
       am.jc_match_id, am.p_home, am.p_draw, am.p_away, am.stars,
       am.confidence_score, am.low_confidence, am.match_date,
       ar.league_key, ar.run_at
  FROM model.pred_archive_match am
  JOIN model.pred_archive ar ON ar.archive_id = am.archive_id
 WHERE am.jc_match_id = ANY(%s)
   AND am.p_home IS NOT NULL AND am.p_draw IS NOT NULL AND am.p_away IS NOT NULL
 ORDER BY am.jc_match_id, ar.run_at DESC
"""

# 回溯模式：每场在**自己的决策时刻**（归档 run_at）之前可见的最后一版 SP
_BACKFILL_SQL = """
SELECT DISTINCT ON (am.jc_match_id)
       am.jc_match_id, am.p_home, am.p_draw, am.p_away, am.stars,
       am.confidence_score, am.low_confidence, am.match_date,
       ar.league_key, ar.run_at, m.home_cn, m.away_cn, o.options, o.snap_ts
  FROM model.pred_archive_match am
  JOIN model.pred_archive ar ON ar.archive_id = am.archive_id
  JOIN fact.jc_match m ON m.match_id = am.jc_match_id
  LEFT JOIN LATERAL (SELECT x.options, x.snap_ts FROM fact.jc_offer x
                       WHERE x.match_id = am.jc_match_id AND x.play_type = 'had'
                         AND x.snap_ts <= ar.run_at
                      ORDER BY x.snap_ts DESC LIMIT 1) o ON TRUE
 WHERE am.jc_match_id IS NOT NULL
   AND am.p_home IS NOT NULL AND am.p_draw IS NOT NULL AND am.p_away IS NOT NULL
   AND (%s::date IS NULL OR am.match_date <= %s::date)
 ORDER BY am.jc_match_id, ar.run_at DESC
"""


def load_sale_matches(conn: Connection) -> list[dict[str, Any]]:
    """竞彩当前期次在售场次（带 had 最新快照的原始 options）。"""
    return [dict(r) for r in conn.execute(_SALE_SQL).fetchall()]


def load_archive_probs(conn: Connection, match_ids: list[int]) -> dict[int, dict[str, Any]]:
    """每场**最新一次归档**的模型概率（按 jc_match_id 精确匹配，不做模糊 join）。"""
    if not match_ids:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for r in conn.execute(_ARCHIVE_PROBS_SQL, (match_ids,)).fetchall():
        out[int(r[0])] = {
            "p": {"h": float(r[1]), "d": float(r[2]), "a": float(r[3])},
            "stars": r[4], "confidence": float(r[5]) if r[5] is not None else None,
            "low_confidence": bool(r[6]), "match_date": str(r[7]) if r[7] else None,
            "league_key": r[8], "run_at": r[9],
        }
    return out


def load_sp_at(conn: Connection, match_ids: list[int], at: datetime) -> dict[int, dict[str, Any]]:
    """回溯辅助：每场在时刻 ``at`` 之前（含）可见的**最后一版** SP。

    ⚠️ 回溯主流程走 `_BACKFILL_SQL`（**每场按自己的 run_at** 取价），
    本函数只在「统一时刻取价」的场景用（例如按某个业务日回看全量）。
    """
    if not match_ids:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for r in conn.execute(
            "SELECT DISTINCT ON (match_id) match_id, options, snap_ts FROM fact.jc_offer "
            " WHERE play_type='had' AND match_id = ANY(%s) AND snap_ts <= %s "
            " ORDER BY match_id, snap_ts DESC", (match_ids, at)).fetchall():
        out[int(r[0])] = {"options": r[1] or {}, "snap_ts": r[2]}
    return out


# ── 预测文件回退（无归档时按名 join）────────────────────────────────
def latest_pred_rows(directory: Path = PREDICTIONS_DIR) -> dict[str, list[dict[str, Any]]]:
    """读预测文件 → ``{league_key: [最新一次的预测行…]}``（每联赛只取最新一次运行）。

    与 `model/archive.py::_prob` 同口径取模型概率（`probs` → `reasoning_factors.*_prob_weighted`）。
    """
    from model.archive import _prob  # 延迟 import：scripts/market/archive 依赖 psycopg 类型

    best: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for path in sorted(glob.glob(str(directory / "prediction_*.json"))):
        name = os.path.basename(path)
        stem = name[:-len(".json")] if name.endswith(".json") else name
        if "_" not in stem:
            continue
        tail = stem.rsplit("_", 1)[-1]
        league = tail if tail and not tail[0].isdigit() else None
        if league is None:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001 - 坏文件跳过
            continue
        if not isinstance(data, dict) or not data.get("predictions"):
            continue
        gen = str(data.get("generated_at") or "")
        if league not in best or gen > best[league][0]:
            best[league] = (gen, data.get("predictions") or [])
    out: dict[str, list[dict[str, Any]]] = {}
    for league, (_gen, preds) in best.items():
        rows = []
        for p in preds:
            probs = {"h": _prob(p, "h"), "d": _prob(p, "d"), "a": _prob(p, "a")}
            if any(v is None for v in probs.values()):
                continue
            rows.append({
                "home": str(p.get("home") or ""), "away": str(p.get("away") or ""),
                "probs": probs, "stars": p.get("stars"),
                "confidence": p.get("confidence_score"),
                "low_confidence": bool(p.get("low_confidence")),
                "kickoff_utc": p.get("kickoff_utc"),
            })
        out[league] = rows
    return out


def match_by_name(cand_home: str, cand_away: str,
                   rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按名匹配预测行：精确优先，别名兜底（`core/team_alias.py` 同语义）。"""
    from core.team_alias import teams_match

    for r in rows:
        if r["home"] == cand_home and r["away"] == cand_away:
            return r
    for r in rows:
        if teams_match(cand_home, cand_away, r["home"], r["away"]):
            return r
    return None


# ── 落库 ────────────────────────────────────────────────────────────
_INSERT_SQL = """
INSERT INTO analysis.ev_report (made_at, legs, p_parlay, ev, kelly, corr_adj, note,
                                 match_id, option_code, sp_decide, p_model,
                                 match_date, league_key, decided_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (match_id, option_code, ((made_at AT TIME ZONE 'Asia/Shanghai')::date)) DO UPDATE SET
  legs = EXCLUDED.legs, p_parlay = EXCLUDED.p_parlay, ev = EXCLUDED.ev,
  kelly = EXCLUDED.kelly, note = EXCLUDED.note, sp_decide = EXCLUDED.sp_decide,
  p_model = EXCLUDED.p_model, match_date = EXCLUDED.match_date,
  league_key = EXCLUDED.league_key, decided_at = EXCLUDED.decided_at
RETURNING report_id
"""

_NOTE_TMPL = ("推荐 {side}：竞彩官方 SP {sp} · 模型概率 {p} · 单位 EV {ev} · "
               "分数 Kelly {kelly}（1/4 Kelly 建议注额 {stake}）；"
               "定价=竞彩官方 SP（决策时刻 {ts}）；"
               "⚠️ 本模型概率为「市场锚定」，EV 含 ESPN↔竞彩 跨市场定价差，"
               "不等于模型有同等真实优势；{extra}")


def write_reports(conn: Connection, picks: list[dict[str, Any]]) -> list[int]:
    """写 `analysis.ev_report`（幂等：同场同侧同天只一行，重跑更新）。"""
    ids: list[int] = []
    for p in picks:
        side_cn = dict(_SIDES)[p["option_code"]]
        extra = []
        if p.get("league_key") == "ucl":
            extra.append("欧冠为冷启动联赛（置信上限 0.5、最高 2-star），低置信")
        if p.get("low_confidence"):
            extra.append("该场标记 low_confidence")
        if p.get("source") == "predfile":
            extra.append("概率来自最新预测文件按名匹配（无当日归档）")
        note = _NOTE_TMPL.format(
            side=side_cn, sp=p["sp"], p=round(p["p"], 4), ev=round(p["ev"], 4),
            kelly=round(p["kelly"], 4), stake=p["stake"],
            ts=p.get("sp_snapshot_ts") or "-", extra="；".join(extra) or "无",
        )
        legs = [{
            "jc_match_id": p["match_id"], "play_type": "had", "option": p["option_code"],
            "sp": p["sp"], "p": p["p"], "ev": p["ev"], "kelly": p["kelly"],
            "all_ev": p.get("all_ev"), "home_cn": p["home_cn"], "away_cn": p["away_cn"],
            "source": p.get("source"), "sp_snapshot_ts": p.get("sp_snapshot_ts"),
        }]
        decided = p.get("decided_at") or now_bjt()
        made_at = decided
        row = conn.execute(_INSERT_SQL, (
            made_at, Jsonb(legs), p["p"], p["ev"], p["kelly"], None, note,
            p["match_id"], p["option_code"], p["sp"], p["p"],
            p.get("match_date"), p.get("league_key"), decided,
        )).fetchone()
        ids.append(int(row[0]))
    return ids


# ── 主流程 ──────────────────────────────────────────────────────────
def build_candidates(conn: Connection, *, backfill: bool = False,
                     to_date: str | None = None) -> tuple[list[Candidate], dict[str, int]]:
    """装配候选场次。``backfill=True`` 走「历史归档 + 决策时刻可见价」回溯模式。

    今日模式：竞彩当前期次在售场次 × 模型概率（归档优先，预测文件按名兜底）。
    回溯模式：历史归档里关联上竞彩的场次 × 决策时刻（run_at）可见的 SP。
    """
    stats = {"sale": 0, "with_sp": 0, "with_prob": 0, "backfill_src": 0}
    cands: list[Candidate] = []

    if backfill:
        # 候选 = 归档里 linked 且有 p 的场次；定价 = **该场自己决策时刻**可见的最后一版 SP
        rows = conn.execute(_BACKFILL_SQL, (to_date, to_date)).fetchall()
        for r in rows:
            opts = r[12] or {}
            sp = {code: _sp(opts.get(code)) for code, _ in _SIDES}
            if any(v is None for v in sp.values()):
                continue     # 决策时刻还没有盘口（未开盘）或 SP 不全 ⇒ 无价可评
            stats["sale"] += 1
            stats["with_sp"] += 1
            cands.append(Candidate(
                match_id=int(r[0]), home_cn=r[10], away_cn=r[11],
                match_date=str(r[7]) if r[7] else None,
                sp={k: v for k, v in sp.items() if v is not None},
                p_model={"h": float(r[1]), "d": float(r[2]), "a": float(r[3])},
                sp_snapshot_ts=str(r[13]), league_key=r[8],
                stars=r[4], confidence=float(r[5]) if r[5] is not None else None,
                low_confidence=bool(r[6]), source="archive", decided_at=r[9],
                notes=["回溯模式：定价 = 决策时刻可见的最后一版竞彩 SP"],
            ))
            stats["with_prob"] += 1
        stats["backfill_src"] = len(cands)
        return cands, stats

    # ── 今日模式 ──
    sale = load_sale_matches(conn)
    stats["sale"] = len(sale)
    rows: list[dict[str, Any]] = []
    for r in sale:
        opts = r["options"] or {}
        sp = {code: _sp(opts.get(code)) for code, _ in _SIDES}
        if any(v is None for v in sp.values()):
            continue     # SP 不全（如 options={}）⇒ 跳过（不猜、不用半个选项）
        stats["with_sp"] += 1
        rows.append({
            "match_id": int(r["match_id"]), "home_cn": r["home_cn"], "away_cn": r["away_cn"],
            "match_date": str(r["business_date"]), "sp": {k: v for k, v in sp.items() if v is not None},
            "sp_snapshot_ts": str(r["snap_ts"]), "league_cn": r["league_cn"],
        })
    if not rows:
        return [], stats

    mids = [r["match_id"] for r in rows]
    arch = load_archive_probs(conn, mids)
    pred_rows = latest_pred_rows()

    for r in rows:
        mid = r["match_id"]
        a = arch.get(mid)
        if a is not None:
            cands.append(Candidate(
                match_id=mid, home_cn=r["home_cn"], away_cn=r["away_cn"],
                match_date=r["match_date"], sp=r["sp"], p_model=a["p"],
                sp_snapshot_ts=r["sp_snapshot_ts"], league_key=a["league_key"],
                stars=a["stars"], confidence=a["confidence"],
                low_confidence=a["low_confidence"], source="archive",
                decided_at=a["run_at"],
                notes=["概率来自当日归档（jc_match_id 精确匹配）"],
            ))
            stats["with_prob"] += 1
            continue
        # 回退：按名匹配最新预测文件
        hit = None
        for _league, prows in pred_rows.items():
            hit = match_by_name(r["home_cn"], r["away_cn"], prows)
            if hit:
                break
        if hit is None:
            continue
        cands.append(Candidate(
            match_id=mid, home_cn=r["home_cn"], away_cn=r["away_cn"],
            match_date=r["match_date"], sp=r["sp"], p_model=hit["probs"],
            sp_snapshot_ts=r["sp_snapshot_ts"], league_key=None,
            stars=hit["stars"], confidence=hit["confidence"],
            low_confidence=hit["low_confidence"], source="predfile",
            decided_at=now_bjt(),
            notes=["概率来自最新预测文件按名匹配（无当日归档）"],
        ))
        stats["with_prob"] += 1
    return cands, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成今日推荐单（建议 4 子项 ①②）")
    ap.add_argument("--backfill", action="store_true",
                    help="回溯模式：历史归档 + 决策时刻可见价（用于攒首批样本）")
    ap.add_argument("--to", dest="to_date", help="回溯模式的截止比赛日 YYYY-MM-DD")
    ap.add_argument("--min-ev", type=float, default=MIN_EV, help="单位 EV 阈值（默认 0.05）")
    ap.add_argument("--min-p", type=float, default=MIN_P, help="模型概率下限（默认 0.10）")
    ap.add_argument("--dry", action="store_true", help="只报计划，不写库")
    args = ap.parse_args(argv)

    with pg.write_conn("app") as conn:
        cands, stats = build_candidates(conn, backfill=args.backfill, to_date=args.to_date)
        picks = [p for p in (pick_recommendation(c, min_ev=args.min_ev, min_p=args.min_p)
                             for c in cands) if p]
        logger.info("recommend: 候选 %d（SP 齐 %d / 有模型概率 %d）→ 达标 %d 条",
                    stats["sale"], stats["with_sp"], stats["with_prob"], len(picks))
        for p in picks:
            logger.info("  · %s vs %s [%s] %s SP=%.2f p=%.3f EV=%+.3f Kelly=%.3f",
                        p["home_cn"], p["away_cn"], p["league_key"] or "-", p["option_code"],
                        p["sp"], p["p"], p["ev"], p["kelly"])
        if args.dry:
            logger.info("（只读模式，加 --execute 才写库）")
            return 0
        ids = write_reports(conn, picks)
        logger.info("recommend: 已写 analysis.ev_report %d 行（report_id %s）",
                    len(ids), ids[:5])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
