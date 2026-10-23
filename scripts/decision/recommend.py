"""建议 4 子项 ①②：生成「今日推荐单」→ `analysis.ev_report`。

口径（路线图 §6.6）：
1. **候选池 = 竞彩当前在售场次**（`fact.jc_match` 的 `business_date >= 今日业务日(BJT)`、
   `match_status='Selling'`）
   —— 不是「模型预测的 14 天窗口」（实测：那些场次当时还没开盘，无价可评）。
   ⚠️ **P2（2026-10-23）**：旧实现取 `business_date = max(business_date)`
   ⇒ 一旦库里出现 D+1 的卡，**今天还在售的场次会被整批踢出候选池**；
   路线图 §6.6.2 ① 的口径是「`business_date >= 今日业务日`」⇒ 现在按此实现。
2. **定价 = 竞彩官方 SP**（`fact.jc_offer` 的 had 快照 `options.h/d/a`）——
   不是 ESPN 赔率（现有 `ev_per_unit` 用的是 ESPN 赔率，那是另一个市场）。
3. **模型概率 = 模型自己的输出**（`model.pred_archive_match.p_*`；无归档时按名 join
   最新的预测文件兜底）。两处都取模型自己的 `probs`/加权概率，**绝不**取市场先验。
4. **每场最多推一侧**（合格方向里 EV 最高的那侧）：一场推多侧会自相矛盾（对冲），一场也只中一侧。
   ⚠️ **P2（2026-10-23）**：合格性**按方向**判定（EV 达标 **且** 模型概率 ≥ 下限），
   不许因为某一侧概率低就把**整场**所有方向一起丢掉（详见 `pick_recommendation`）。
5. **EV / Kelly 公式唯一真源**：`scripts/market/kelly.py`（与 web 层 combo 共用）。
6. **保守**：默认 EV ≥ **5%**（= 正 EV 之外的安全垫）、1/4 Kelly；并把「EV 含跨市场定价差」写进 note 与页面。
   ⚠️ **水位 12.9%（2026-10-23 生产实测）≠ 推荐门槛**：对**单边**投注，
   「扛掉水」等价于 **EV > 0**（推导见 `MIN_EV` 上方的注释）；水位只在**串关**上才需要单独扛。

幂等：唯一键 `(match_id, option_code, (made_at AT TIME ZONE 'Asia/Shanghai')::date)`；
重跑只更新价格与概率，不重复插行。
⚠️ **P2（2026-10-23）**：`made_at` = **这条荐单真正被生成的时刻**（BJT 墙钟），
不是预测归档的 `run_at` ⇒ 当天 09:30 定时跑之前手动出单，**不会**落进昨天的桶、
不会覆盖昨天那一行（`decided_at` 仍记模型意见时刻 = 归档 `run_at`，两者语义不同）。

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── 阈值（保守档；路线图 §6.6.3 第 1 条：模型概率是市场锚定的，EV 含跨市场定价差）────
#
# ⚠️ **P2（2026-10-23）：水位口径订正 —— 「EV 必须不低于水位」这句对**单边**是错的。**
#
# 实测水位（**生产实测**，2026-10-23 审计）：竞彩 had 三项 SP 的隐含概率和 − 1 = **12.9%**
#   （交叉验证：路线图 §6.6.1 当日实测 SP 1.86/3.70/3.12 ⇒ 1/1.86+1/3.70+1/3.12 = 1.1284 ⇒ 12.84%，
#     与 12.9% 互相印证；旧文案里那个「每元回报 0.75」的推导不成立，
#     正确值 ≈ 0.89 元/元 ⇒ 水位 ≈ 11~13%）。
#
# 代数（m = 1+水位 = 三项隐含概率和，q = 去水后的市场概率，SP = 1/(m·q)）：
#   单位 EV = p·SP − 1 = p/(m·q) − 1
#   ⇒ **EV > 0 ⟺ p > m·q ⟺ p/q > m**（模型概率相对市场去水概率高出**整个水位**）
#   ⇒ 对**单边**投注，「扛掉这笔水」= **EV > 0**，**不是** EV ≥ 12.9%。
#   （EV ≥ 12.9% 等价于 p/q ≥ m² ≈ 1.273 —— 要求模型比市场高出 27%，是**另一个**门槛。）
#   水位只在**串关**上才需要单独「扛」：串关 EV = ∏(p_i/q_i)/mⁿ − 1 ⇒ 门槛是 mⁿ。
#
# 生产实测（`analysis.ev_report`，2026-10-23 查）：达标荐单的 EV 分布
#   日        条数  EV最小   EV中位   EV最大   ≥5%  ≥10%  ≥12.9%
#   2026-10-23   3  0.0903  0.1082  0.2668    3     2      1
#   2026-10-22   4  0.0777  0.1356  0.4482    4     3      2
#   2026-10-21   7  0.0973  0.1417  1.3321    7     5      4
#   2026-10-18  10  0.1001  0.2641  0.8470   10    10      7
#   ⇒ 历史最低 EV = **0.0608**（8 天全量）⇒ 5% 门槛实际并没有放进负 EV 的荐单；
#   ⇒ 把门槛抬到 12.9% 会把**正 EV 的好单**砍掉 50~70%（10-23：3 条 → 1 条），
#     属**产品阈值政策**变更（路线图 §6.6.5.1 明确「阈值暂不动，等 n ≥ 100 再校准」）
#     ⇒ **本轮不改阈值**，只把**用户可见文案**订正成实测值（见 web/services/recommend.py）。
#     需要更保守/更激进时用 CLI `--min-ev` 覆盖（note 里会记下当时生效的门槛）。
#
# ⚠️ **P2-I（2026-10-23）**：阈值数字搬去 **`core/betting_policy.py`**（唯一真源）——
#   因为「用户可见文案里的数字」必须**从常量派生**（P2-H4 事故：文案手打
#   「阈值已对齐为 EV ≥ 12.9%」而代码是 5% ⇒ 漂移成**假话** ⇒ 静态审计/单测/守门
#   全都没抓到）。本模块的 `MIN_EV` / `MIN_P` 现在是**导入名**（值不变）。
from core.betting_policy import MIN_EV, MIN_P, WATER_LEVEL_TEXT, threshold_text
from core.bjt import now_bjt
from core.log import logger
from market.kelly import DEFAULT_FRACTION, ev_per_unit, kelly_fraction, suggested_stake
from psycopg import Connection
from psycopg.types.json import Jsonb
from store import pg

# MIN_EV = 0.05   → 单位 EV ≥ 5%（= 在「正 EV 之外再加一道安全垫」；口径见上面的推导）
# MIN_P = 0.10    → 模型概率 ≥ 10%（不要长尾彩票）——**方向级**排除，见 pick_recommendation
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


# ── 定价赔率新鲜度守门（采集端改手动模式配套，2026-10-22）──────────────
#
# 为什么需要这道闸：采集端从「计划任务自动采」改成「手动跑」之后，
# 「人忘了先采集就直接出荐单」成了新的**静默失败**路径 ——
# 库里最新 SP 可能还是几天前的，照样算得出 EV，页面看上去完全正常
# （不报错、不空态、ROI 照样有数字）。这就是本项目反复治理的
# 「跑完了 ≠ 跑对了」的另一种形态 ⇒ 必须在**生成端**堵住，不能靠人记得。
#
# 两层闸：
#   ① **全局闸**：全库 had 最新快照距今 > 阈值 ⇒ 拒绝出荐单（退出码 3），
#      并打印「先采集」的修复命令。--allow-stale 可显式放行（note 里留痕）。
#   ② **逐场闸**：某一场自己的 had 快照距今 > 阈值 ⇒ 跳过该场
#      （不用旧价给这一场算 EV），计入 stats['stale_skipped']。
#
# ⚠️ 为什么 snap_ts 能当「采集时刻」用：fact.jc_offer 的主键是
# (match_id, play_type, snap_ts) ⇒ **每次采集都新插一行**，
# 不存在「赔率没动就不更新、snap_ts 停在旧值」的情况
# （生产实测：1758 个不同 snap_ts × 5 个 play_type × 471 场）。
DEFAULT_MAX_OFFER_AGE_HOURS = 6.0
STALE_EXIT_CODE = 3          # 与「配额耗尽(3)」区分：文档里写明即可

_FRESH_SQL = "SELECT max(snap_ts) FROM fact.jc_offer WHERE play_type = 'had'"
_ARRIVAL_SQL = "SELECT max(arrived_at) FROM ops.file_arrival WHERE topic = 'jczq_offer'"


def _age_hours(ts: Any) -> float | None:
    """时间戳 → 距今小时数。None = 缺失/不可解析（调用方按最坏情况处理）。"""
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


def offer_freshness(conn: Connection,
                    max_age_hours: float = DEFAULT_MAX_OFFER_AGE_HOURS) -> dict[str, Any]:
    """定价赔率新鲜度（两个口径：库内快照 / 采集到港）。

    返回 {"latest_snap", "age_hours", "stale", "threshold_hours",
          "arrival_ts", "arrival_age_hours", "arrival_stale"}。

    - age_hours 为 None（一条 had 快照都没有）⇒ stale=True（宁可拦，不可拿旧价出单）。
    - arrival_* 走 ops.file_arrival（采集侧到港时间），与 web 的
      /api/v1/jc/freshness 同一数据源 ⇒ 两处口径一致。
    """
    row = conn.execute(_FRESH_SQL).fetchone()
    latest = row[0] if row else None
    age = _age_hours(latest)
    arow = conn.execute(_ARRIVAL_SQL).fetchone()
    arrival = arow[0] if arow else None
    arr_age = _age_hours(arrival)
    return {
        "latest_snap": str(latest) if latest is not None else None,
        "age_hours": round(age, 2) if age is not None else None,
        "stale": age is None or age > max_age_hours,
        "threshold_hours": max_age_hours,
        "arrival_ts": str(arrival) if arrival is not None else None,
        "arrival_age_hours": round(arr_age, 2) if arr_age is not None else None,
        "arrival_stale": arr_age is None or arr_age > max_age_hours,
    }


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
    # ⚠️ P2（2026-10-23）：这条荐单**真正被生成**的时刻（BJT 墙钟）。
    #   None = 落库时取当下（今日模式）；回溯模式显式给归档 run_at（历史重建）。
    #   **不是** decided_at：decided_at 是「模型意见时刻」，made_at 才是「荐单生成时刻」。
    made_at: datetime | None = None
    notes: list[str] = field(default_factory=list)


# ── 纯逻辑：候选 → 推荐单 ────────────────────────────────────────────
def pick_recommendation(cand: Candidate, *, min_ev: float = MIN_EV,
                        min_p: float = MIN_P,
                        fraction: float = KELLY_FRACTION,
                        bankroll: float = BANKROLL) -> dict[str, Any] | None:
    """从一场里挑出**该推的那一侧**；没有合格方向 ⇒ None（不推）。

    ⚠️ **P2（2026-10-23）：合格性按「方向」判定，不按「整场」判定。**

    旧实现：先取 **EV 最高**的那一侧，再看它的模型概率是否 ≥ 10%，
    不过就把**整场**丢掉 ⇒ 两条规则互相打架：
      ① 「不要长尾彩票」本意是**排除长尾方向**，旧实现却变成**整场连坐**；
      ② 高 SP 冷门（模型概率很低、SP 很高）EV 可能最大 ⇒ 旧实现里它一旦概率 < 10%
         就把整场否掉，而**别的方向根本没有机会被评估**（产品约定被绕过）。

    现在的口径（自洽且可解释）：
      · **方向级合格**：单位 EV ≥ ``min_ev`` **且** 模型概率 p ≥ ``min_p``；
      · **「不要长尾彩票」= 方向级排除**：p < ``min_p`` 的方向（高 SP 冷门就在此列）
        **一律不进推荐**，哪怕它的 EV 再大；
      · 合格方向里取 **EV 最高**的一侧（每场最多推一侧：一场推多侧会自相矛盾）；
      · 一个方向都不合格 ⇒ 这场不推（**不是**被某一条长尾连坐掉的）。
    选中的方向与展示口径一致：返回的 ``sp`` / ``p`` / ``ev`` 就是被选中的那一侧，
    ``all_ev`` 给出三侧 EV（含未入选侧，便于复算）。
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
        if e < min_ev:
            continue          # EV 不达标 ⇒ 该方向不合格
        if p < min_p:
            continue          # 长尾彩票 ⇒ 该方向不合格（**方向级**排除，不是整场连坐）
        if best is None or e > best["ev"]:
            best = {"code": code, "ev": e, "sp": sp, "p": p}
    if best is None:
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
        "made_at": cand.made_at,
        "min_ev": min_ev,
        "min_p": min_p,
        "notes": list(cand.notes),
    }


# ── DB：候选装配 ────────────────────────────────────────────────────
# ⚠️ **P2（2026-10-23）**：候选池口径从 `business_date = max(business_date)`
# 改成 **`business_date >= 今日业务日(BJT)`**（路线图 §6.6.2 ① 的原文口径）。
# 旧口径的坑：库里一旦出现 D+1 的卡，`max(business_date)` 就变成 D+1 ⇒
# **今天还在售的场次被整批踢出候选池**（荐单凭空变少/变空，页面看不出原因）。
# 「今日业务日」= **BJT 自然日**（`core.bjt.now_bjt().date()`，与 `fact.jc_match.business_date`
# 的「营业日（北京）」同口径）；**不用** `current_date`（会话时区，生产 PG 常为 UTC ⇒ 差一天）。
_SALE_SQL = """
SELECT m.match_id, m.business_date, m.home_cn, m.away_cn, m.league_cn,
       o.options, o.snap_ts
  FROM fact.jc_match m
  JOIN LATERAL (SELECT options, snap_ts FROM fact.jc_offer x
                  WHERE x.match_id = m.match_id AND x.play_type = 'had'
                  ORDER BY x.snap_ts DESC LIMIT 1) o ON TRUE
 WHERE m.business_date >= %s::date
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


# _SALE_SQL 的列序（位置索引用，见 load_sale_matches 的注释）
_SALE_COLS = ("match_id", "business_date", "home_cn", "away_cn",
              "league_cn", "options", "snap_ts")


def load_sale_matches(conn: Connection, today: str | None = None) -> list[dict[str, Any]]:
    """竞彩当前期次在售场次（带 had 最新快照的原始 options）。

    候选池 = ``business_date >= 今日业务日(BJT)`` ∩ ``match_status='Selling'``
    （路线图 §6.6.2 ①；旧口径 `= max(business_date)` 会把今天还在售的场次踢出池子）。

    ``today`` 缺省 = **BJT 自然日**（`core.bjt.now_bjt().date()`，唯一真源）；
    可显式传入（测试 / 回溯用）。

    ⚠️ **按位置索引取列，不要写 `dict(r)`**：`store.pg.connect()` 用 psycopg3 默认的
    `tuple_row` ⇒ `conn.execute()` 返回的行是**普通 tuple**，`dict(tuple)` 会抛
    `TypeError: object is not iterable`（生产首跑实测：决策层因此静默失败，
    「今日推荐单」永远是空的）。本仓库里 `dict(row)` 只有在**显式设了
    `cur.row_factory = dict_row`** 的地方才安全（见 `store/jc_view.py`）。
    """
    day = today or now_bjt().date().isoformat()
    out: list[dict[str, Any]] = []
    for r in conn.execute(_SALE_SQL, (day,)).fetchall():
        out.append({
            "match_id": int(r[0]), "business_date": r[1], "home_cn": r[2],
            "away_cn": r[3], "league_cn": r[4], "options": r[5], "snap_ts": r[6],
        })
    return out


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
               "入选门槛：单位 EV ≥ {min_ev}、模型概率 ≥ {min_p}"
               "（低于此即长尾彩票，该方向不进推荐；"
               "水位 {water_level} 是 2026-10-23 生产实测的**市场水位**，"
               "对单边投注等价于 EV>0）；"
               "⚠️ 本模型概率为「市场锚定」，EV 含 ESPN↔竞彩 跨市场定价差，"
               "不等于模型有同等真实优势；{extra}")


def write_reports(conn: Connection, picks: list[dict[str, Any]]) -> list[int]:
    """写 `analysis.ev_report`（幂等：同场同侧同天只一行，重跑更新）。

    ⚠️ **P2（2026-10-23）：`made_at` = 这条荐单**真正被生成的时刻**（BJT 墙钟）。**
    旧实现 `made_at = decided_at`（= 预测归档的 `run_at`）⇒ 当天 09:30 定时跑**之前**
    手动出单时，`made_at` 落在**昨天**的桶里 ⇒ 唯一键
    `(match_id, option_code, (made_at AT TIME ZONE 'Asia/Shanghai')::date)`
    把这一行**写进昨天的桶、覆盖昨天那一行**。
    ⇒ 现在 `made_at = picks[i].made_at or now_bjt()`：
      · 今日模式（`made_at` 为 None）⇒ 取**当下** ⇒ 落进**今天**的桶（同日覆盖同日）；
      · 回溯模式 ⇒ 显式给归档 `run_at`（历史重建，按历史日分桶，不影响今天）。
    `decided_at`（模型意见时刻）语义不变，仍记归档 `run_at`。
    """
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
        if p.get("stale_note"):
            extra.append(p["stale_note"])
        note = _NOTE_TMPL.format(
            side=side_cn, sp=p["sp"], p=round(p["p"], 4), ev=round(p["ev"], 4),
            kelly=round(p["kelly"], 4), stake=p["stake"],
            ts=p.get("sp_snapshot_ts") or "-", extra="；".join(extra) or "无",
            # ⚠️ P2-I：门槛用**双表示**（比例 + 百分比）—— 比例给复算，百分比给人看；
            #   水位从**唯一真源**派生（不许手写 "12.9%"）。
            min_ev=threshold_text(p.get("min_ev", MIN_EV)),
            min_p=threshold_text(p.get("min_p", MIN_P)),
            water_level=WATER_LEVEL_TEXT,
        )
        legs = [{
            "jc_match_id": p["match_id"], "play_type": "had", "option": p["option_code"],
            "sp": p["sp"], "p": p["p"], "ev": p["ev"], "kelly": p["kelly"],
            "all_ev": p.get("all_ev"), "home_cn": p["home_cn"], "away_cn": p["away_cn"],
            "source": p.get("source"), "sp_snapshot_ts": p.get("sp_snapshot_ts"),
        }]
        made_at = p.get("made_at") or now_bjt()   # 荐单**生成时刻**（BJT 墙钟）
        decided = p.get("decided_at") or made_at   # 模型意见时刻（归档 run_at）
        row = conn.execute(_INSERT_SQL, (
            made_at, Jsonb(legs), p["p"], p["ev"], p["kelly"], None, note,
            p["match_id"], p["option_code"], p["sp"], p["p"],
            p.get("match_date"), p.get("league_key"), decided,
        )).fetchone()
        ids.append(int(row[0]))
    return ids


# ── 主流程 ──────────────────────────────────────────────────────────
def build_candidates(conn: Connection, *, backfill: bool = False,
                     to_date: str | None = None,
                     max_offer_age_hours: float | None = DEFAULT_MAX_OFFER_AGE_HOURS,
                     allow_stale: bool = False) -> tuple[list[Candidate], dict[str, Any]]:
    """装配候选场次。``backfill=True`` 走「历史归档 + 决策时刻可见价」回溯模式。

    今日模式：竞彩当前期次在售场次 × 模型概率（归档优先，预测文件按名兜底）。
    回溯模式：历史归档里关联上竞彩的场次 × 决策时刻（run_at）可见的 SP。

    ``max_offer_age_hours``：**逐场**定价快照龄闸（见模块上方口径说明）。
    None = 不限龄（只用于回溯模式与测试）；``allow_stale=True`` 时也放行超龄场次。
    """
    stats: dict[str, Any] = {"sale": 0, "with_sp": 0, "with_prob": 0,
                              "backfill_src": 0, "stale_skipped": 0,
                              "offer_age_hours": None}
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
                # 回溯模式：这条荐单「历史时刻」生成 ⇒ made_at = 归档 run_at（按历史日分桶，
                # 不污染今天的桶）。今日模式留 None ⇒ 落库时取当下（见 write_reports）。
                made_at=r[9],
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
        # 逐场定价快照龄闸：这场自己的价太久没更新 ⇒ 不用旧价算 EV
        age = _age_hours(r["snap_ts"])
        if age is not None:
            prev_age = stats.get("offer_age_hours")
            stats["offer_age_hours"] = age if prev_age is None else max(prev_age, age)
        if (not allow_stale and max_offer_age_hours is not None
                and age is not None and age > max_offer_age_hours):
            stats["stale_skipped"] += 1
            continue
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
    ap.add_argument("--min-ev", type=float, default=MIN_EV,
                    help="单位 EV 阈值（默认 0.05 = 正 EV 之外的安全垫；水位 12.9%% 是**市场水位**，"
                         "对单边投注等价于 EV>0，不是 EV≥12.9%%）")
    ap.add_argument("--min-p", type=float, default=MIN_P,
                    help="模型概率下限（默认 0.10；低于此的方向不进推荐 = 不要长尾彩票）")
    ap.add_argument("--dry", action="store_true", help="只报计划，不写库")
    ap.add_argument("--max-offer-age-hours", type=float, default=DEFAULT_MAX_OFFER_AGE_HOURS,
                    help="定价赔率快照最大年龄（小时）；超龄即拒绝出单（默认 6）")
    ap.add_argument("--allow-stale", action="store_true",
                    help="放行超龄定价快照（会在 note 里标注用的是几小时前的赔率）")
    args = ap.parse_args(argv)

    with pg.write_conn("app") as conn:
        fresh = offer_freshness(conn, args.max_offer_age_hours)
        logger.info("recommend: 定价赔率快照 %s（距今 %s 小时 / 阈值 %s 小时）；采集到港 %s（距今 %s 小时）",
                    fresh["latest_snap"] or "无", fresh["age_hours"],
                    fresh["threshold_hours"], fresh["arrival_ts"] or "无",
                    fresh["arrival_age_hours"])
        # ① 全局闸：定价快照整体超龄 ⇒ 拒绝出单（拿旧价算 EV 是静默失败，不是「没数据」）
        if fresh["stale"] and not args.allow_stale:
            logger.error(
                "recommend: 拒绝出单 —— 定价赔率快照已 %s 小时未更新（阈值 %s 小时）。"
                "采集端改手动后这是新增的静默失败路径：旧价算出来的 EV 页面看着完全正常。"
                "请先跑一次采集（国内机 scripts\\refresh_now.bat，或 collector.py --push-batch <topic>...），"
                "等远端装载完再出单；确需放行请加 --allow-stale（会在 note 里留痕）。",
                fresh["age_hours"] if fresh["age_hours"] is not None else "∞",
                fresh["threshold_hours"])
            return STALE_EXIT_CODE
        cands, stats = build_candidates(conn, backfill=args.backfill, to_date=args.to_date,
                                        max_offer_age_hours=args.max_offer_age_hours,
                                        allow_stale=args.allow_stale)
        picks = [p for p in (pick_recommendation(c, min_ev=args.min_ev, min_p=args.min_p)
                             for c in cands) if p]
        if args.allow_stale and fresh["stale"]:
            stale_note = (f"⚠️ 定价赔率快照已 {fresh['age_hours']} 小时未更新"
                          f"（阈值 {fresh['threshold_hours']} 小时，--allow-stale 放行）")
            for p in picks:
                p["stale_note"] = stale_note
        logger.info("recommend: 候选 %d（SP 齐 %d / 有模型概率 %d / 跳过超龄定价 %d）"
                    "→ 达标 %d 条（门槛：EV ≥ %.3f、模型概率 ≥ %.2f）",
                    stats["sale"], stats["with_sp"], stats["with_prob"],
                    stats["stale_skipped"], len(picks), args.min_ev, args.min_p)
        for p in picks:
            logger.info("  · %s vs %s [%s] %s SP=%.2f p=%.3f EV=%+.3f Kelly=%.3f",
                        p["home_cn"], p["away_cn"], p["league_key"] or "-", p["option_code"],
                        p["sp"], p["p"], p["ev"], p["kelly"])
        if args.dry:
            logger.info("（只读模式：本次未写库）")
            return 0
        ids = write_reports(conn, picks)
        logger.info("recommend: 已写 analysis.ev_report %d 行（report_id %s）",
                    len(ids), ids[:5])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
