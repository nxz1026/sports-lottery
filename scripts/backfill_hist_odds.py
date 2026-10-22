"""backfill_hist_odds.py — 给历史比赛样本补齐「赛前赔率派生的真实概率」。

背景
----
``scripts/references/historical_past_matches.json`` 是 ML 训练 / 校准的累积样本，
由 ``core/calibration.py::append_historical_past_matches`` 从「完赛记录」累积而成，
而完赛记录来自 football-data / API-Football 这类**不带赔率**的数据源。
生产实测（Oracle 上核对过）：样本 629 条里 ``home_true_prob`` / ``draw_true_prob`` /
``away_true_prob`` / ``draw_implied`` **全是 null**、``odds_data_available`` **全是 false**
⇒ 「赔率热门准确率」「有赔率的样本」这类指标恒为 0（不是热门没赢，是压根没有赔率样本）。

数据源事实（生产实测，别再自己猜）
------------------------------------
1. **ESPN scoreboard 对已完赛场次不返回赔率**：
   ``https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?dates=20250920``
   实测 ``events[i].competitions[0].odds == [null]`` ⇒ 赔率**不能**从 scoreboard 拿。
   event id 从 scoreboard 的 ``events[i].id`` 取；**比分也在 scoreboard 里**
   （``competitors[i].score``），本脚本用它做「赔率所属赛事 ↔ 样本赛事」的比分一致性校验。
2. ⚠️ **ESPN summary 里 ``odds[]`` 的三侧 ``odds.value`` 是终场/滚球价，不是赛前价** ——
   **这一条是 2026-10-21 复核时推翻的旧结论**（旧实现就是采信了它，才闹出下面的假象）：
   实测「多特蒙德 4-1 毕尔巴鄂竞技」⇒ ``home=1/500(1.002)``、``away=450/1(451.0)``、
   ``draw=50/1(51.0)``；实测「布拉格斯拉维亚 2-3 朗斯」⇒ ``home=500/1(501.0)``、
   ``away=1/66(1.015)``。两场都是**谁赢谁被看好到 500/1** ⇒ 那是赛果已知后的价格。
   用它回填 ⇒ 样本里「概率更高的一方」**281/281 全胜（100%）**，而真实足球约 55%。
   ⇒ **绝不能**用它当赛前 1x2。
3. ✅ **赛前 1x2 在 ``summary["pickcenter"][]``**（ESPN 比赛页赔率表）：
   ``pickcenter[i].homeTeamOdds.moneyLine`` / ``drawOdds.moneyLine`` /
   ``awayTeamOdds.moneyLine`` 是**美式**赔率（正数 +X / 负数 -X）⇒ 需自己换算成十进制。
   实测「布拉格斯拉维亚 vs 朗斯」pickcenter 给 ``home=+180 / draw=+250 / away=+145``
   ⇒ 十进制 2.80 / 3.50 / 2.45，**讲得通**；而同一场 summary 的 ``odds[]`` 给 501 / 21 / 1.015
   ⇒ 讲不通。**同一份 summary 里两个字段给的是同一场比赛的两种价，千万别拿错。**
4. **兜底源**：``summary["odds"][]`` 里带 ``open`` / ``current`` 的块（DraftKings / ESPN BET）
   的块级 ``moneyLine`` 也是美式赛前价；但**"ESPN BET - Live Odds"（provider id 59）是滚球**
   ⇒ 认 provider 名含 ``live`` 的块直接跳过。
5. **实测：ESPN 偶尔给出不自洽的赔率块**。抽水和看着正常不代表可信 —— 平局价 < 2.0
   （足球 1x2 的平局价下限）⇒ 整块丢掉（宁可少一场，也不把垃圾当市场真值）。
6. scoreboard 的 ``dates`` 只吃单日 ``YYYYMMDD``；整日历年 ``YYYY`` 也支持，
   但**必须显式放大 limit**（默认 50 会**静默截断**整年赛程）⇒ 本脚本固定用 400。
   日期跨度大时按**年**分批请求即可。
7. ESPN 公开接口无 key、无配额，但仍然礼貌限速（默认每个请求之间 sleep 0.3s）。

口径（严格照仓库既有实现，不另起炉灶）
--------------------------------------
- 去水复用 ``core.data.parse.remove_vig``（默认 logit 对数比例法），
  隐含概率 = 1 / 十进制赔率 ⇒ 与 ``core/data/odds_enrich.py`` 的富化路径完全一致。
- 队名归一复用 ``core.data.odds_enrich.normalize_team``（含跨语言别名表）。
- 匹配键 = ``(normalize_team(home), normalize_team(away), kickoff_utc[:10])``，
  **精确相等**才算命中；认不上就跳过并计数 ——
  **绝不按模糊相似度瞎配**（错配比不配更糟：错配会污染训练样本且无法察觉）。
- 只填缺的、绝不覆盖：目标字段已有非空值就不动 ⇒ 脚本**幂等**，重跑一次数字不变。
- 只填这 7 个字段：``home_true_prob`` / ``draw_true_prob`` / ``away_true_prob`` /
  ``home_ml_implied`` / ``draw_implied`` / ``away_ml_implied`` / ``odds_data_available``。
- **不碰** ``ml_home_close`` / ``draw_ml``：那两个字段在既有口径里是**美式赔率字符串**，
  而 summary 只给十进制赔率 ⇒ 混填就是口径污染。
- 缺任一侧赔率 ⇒ **整场放弃**（不填一半），只计入「命中但未填」。
- 赔率块还要过三道合理性闸（见 ``_plausible_prematch`` / ``_orientation_ok``）：
  抽水和 ∈ [1.0, 1.5]、平局价 ≥ 2.0、任一侧隐含概率 ≤ 0.97（赛前不可能有 97% 的确定热门）
  —— 任一条不过就**整块丢掉**（脏数据写进样本比不写更糟）。
- **比分一致性**：赔率所属 event 的最终比分必须与样本 ``score`` 一致，不一致 ⇒ 整场不填
  （同队名同日期的不同赛事会骗过纯键匹配）。
- **统计合理性闸**（``validate_filled_sample``）：回填完做**体检**——
  ① 赔率隐含的主胜/平局/客胜分布 vs 实际比分分布，逐格 z 检验（z > 3.5 判刑）；
  ② 热门在非平局场次的胜率 vs **由这批价格自身算出**的期望（真实值约 0.73，
  不是常被引用的 0.55 —— 那个是「含平局」口径）；
  ③ 「隐含概率 ≥ 0.90」的场次占比（真实市场约 6%；终场价会让它接近 100%）。
  **判刑 ⇒ 拒绝写盘、退出码 1**（干跑也会报）。
  ⇒ 这道闸就是为了让「再采错一次字段」当场被抓，而不是像这次一样**靠人眼发现 97.6% 不对劲**。
- 未知 league 值 ⇒ 明确跳过并计数告警（不瞎猜 slug，猜错 = 跨联赛错配）。

用法
----
    python scripts/backfill_hist_odds.py                                  # 干跑（默认，不写盘）
    python scripts/backfill_hist_odds.py --leagues epl,ucl --limit 20      # 小样本调试
    python scripts/backfill_hist_odds.py --execute                         # 真写回文件

退出码：0 = 正常（干跑 / 写回都一样）；
1 = 数据不全（抓取失败率过高）**或统计体检不通过**（拒绝写盘）；
2 = 参数或输入错误（文件不存在 / 不是合法 JSON / 未知联赛参数）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 让 `python scripts/backfill_hist_odds.py` 在仓库根目录直接可跑：
# 本脚本在 scripts/ 下，仓库内模块（core.*）在 scripts/core/ ⇒ 把 scripts/ 挂进 sys.path。
_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _p in (str(_REPO_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.data.odds_enrich import normalize_team  # noqa: E402
from core.data.parse import remove_vig  # noqa: E402

# ── 常量 ────────────────────────────────────────────────────────────────

# 联赛 key → ESPN 的 slug。只认这 6 个：样本里出现别的 league 值时**明确跳过**，
# 不瞎猜 slug（猜错 = 拿别的联赛赛程来配对 = 静默错配）。
LEAGUE_SLUGS: dict[str, str] = {
    "ucl": "uefa.champions",
    "epl": "eng.1",
    "laliga": "esp.1",
    "seriea": "ita.1",
    "bundesliga": "ger.1",
    "ligue1": "fra.1",
}

SCOREBOARD_URL = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
                  "{slug}/scoreboard?dates={dates}&limit={limit}")
SUMMARY_URL = ("https://site.web.api.espn.com/apis/site/v2/sports/soccer/"
               "{slug}/summary?event={event_id}")

# scoreboard 的 limit 默认 50 会**静默截断**整年赛程（实测 eng.1 一年 378 场）
# ⇒ 整年请求必须放大到 400。
SCOREBOARD_LIMIT = 400

# summary 的 odds 是「一家博彩公司一块」的列表；实测只有 Bet365（provider id=2000）
# 稳定给**十进制** 1x2（其余块给美式 moneyLine）⇒ 优先选它，整份样本口径一致。
PREFERRED_PROVIDER_ID = "2000"

# ── 赛前 1x2 的合理性闸（实测踩过的坑，见 _plausible_prematch 的 docstring）──
# 1x2 的平局赔率不可能低于 2.0（否则 P(平) > 50%，足球联赛不可能出现）。
MIN_DECIMAL_DRAW_ODDS = 2.0
# 三侧隐含概率之和 = 1 + 抽水。正常 1x2 在 1.02~1.12；
# 但**强弱悬殊**的场次（热门 1.07）长赔会被压得很贵，抽水能到 1.4 ⇒ 上限放宽到 1.5。
MIN_OVERROUND = 1.0
MAX_OVERROUND = 1.5
# 任一侧的**原始隐含概率**上限：赛前不存在 97% 以上的确定热门。
# 这条闸专门拦「终场/滚球价」（实测那种价是 1.002 / 1.008 ⇒ 隐含 99.8%）。
MAX_PREMATCH_IMPLIED = 0.97
# 统计合理性闸：
# ⚠️ 基线**不许拍脑袋** —— 真实市场里「热门在非平局场次的胜率」约 0.73（不是常被引用的
# 0.55，那个是「含平局」口径）；且必须与**这批价格自身隐含的分布**比，才不会被
# 拍脑袋的常数误伤。下面三个阈值都是**分布形状/自洽性**判据，与口径无关。
MIN_AUDIT_SAMPLES = 30
# 分布校准检验的 z 阈值（3 格 × 每次跑一次 ⇒ 误报率 ~0.05%）。
CALIBRATION_Z_LIMIT = 3.5
# 「隐含概率 ≥ 0.90」的场次占比上限：真实赛前市场很低（生产实测 6.1%），
# 终场价会让它接近 100%（实测 290 条里绝大多数都是 95%+）。
HIGH_CONFIDENCE_THRESHOLD = 0.90
HIGH_CONFIDENCE_MAX_RATIO = 0.15

DEFAULT_FILE = "scripts/references/historical_past_matches.json"
DEFAULT_SLEEP = 0.3
DEFAULT_TIMEOUT = 25
DEFAULT_MAX_WORKERS = 4

# 抓取失败率超过这个比例 ⇒ 退出码 1：宁可报「数据不全」，也别让人以为跑干净了。
MAX_FAILURE_RATE = 0.2

# 隐含概率字段（十进制赔率 1/odds）。
IMPLIED_FIELDS = ("home_ml_implied", "draw_implied", "away_ml_implied")
# 去水后的真实概率字段。
TRUE_PROB_FIELDS = ("home_true_prob", "draw_true_prob", "away_true_prob")

FetchJson = Callable[[str, float], Any]


# ── HTTP 取数（可替换，测试注入假 fetcher）────────────────────────────────

def fetch_json(url: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """默认 HTTP 取数器：urllib + JSON。

    抽成独立函数是为了让测试能整体替换掉（测试里禁止真联网）。
    """
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "sports-lottery-backfill/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class RateLimiter:
    """全局请求节流：保证**任意两次请求的开始时间**间隔 ≥ sleep 秒。

    线程池并发抓 summary 时，若每个线程各睡各的，总速率会飙到 workers×(1/sleep)；
    这里用一把锁把「排号 → 等待 → 放行」串起来，全局速率恒定受 sleep 约束。
    """

    def __init__(self, sleep_seconds: float) -> None:
        self._sleep = max(0.0, float(sleep_seconds))
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        if self._sleep <= 0:
            return
        with self._lock:
            now = time.monotonic()
            gap = self._next_at - now
            if gap > 0:
                time.sleep(gap)
                now = time.monotonic()
            self._next_at = now + self._sleep


# ── 数据结构 ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EventRef:
    """scoreboard 里的一个 event（已按匹配键归一）。"""

    event_id: str
    league: str
    slug: str
    date: str
    # 主/客的 ESPN team id：用来核对赔率块的主客方向（方向反了 = 镜像概率 = 脏数据）
    home_team_id: str = ""
    away_team_id: str = ""
    # 最终比分（ESPN 的 score 是裸数字字符串）：用来核对「赔率所属赛事 ↔ 样本赛事」是不是同一场
    home_score: str = ""
    away_score: str = ""


@dataclass
class BackfillStats:
    """逐条可核对的统计。

    不变式（干跑输出里逐条可对账）：
    - ``total == matched + unmatched``（抓取失败的条目也算「命中」，它有 event id）；
    - ``matched == filled + no_odds + already_filled + fetch_failed``（命中的条目四选一）。
    """

    total: int = 0
    matched: int = 0
    filled: int = 0
    unmatched: int = 0
    fetch_failed: int = 0
    unknown_league: int = 0
    no_odds: int = 0
    already_filled: int = 0
    skipped_non_dict: int = 0
    skipped_league_filter: int = 0
    score_mismatch: int = 0
    leagues: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "matched": self.matched,
            "filled": self.filled,
            "unmatched": self.unmatched,
            "fetch_failed": self.fetch_failed,
            "unknown_league": self.unknown_league,
            "no_odds": self.no_odds,
            "already_filled": self.already_filled,
            "skipped_non_dict": self.skipped_non_dict,
            "skipped_league_filter": self.skipped_league_filter,
            "score_mismatch": self.score_mismatch,
            "leagues": dict(self.leagues),
        }


# ── 匹配键 ────────────────────────────────────────────────────────────────

def entry_names(entry: dict) -> tuple[str, str]:
    """取条目的主客队名：优先 ``home_en`` / ``away_en``，缺失才回落 ``home`` / ``away``。"""
    home = str(entry.get("home_en") or entry.get("home") or "").strip()
    away = str(entry.get("away_en") or entry.get("away") or "").strip()
    return home, away


def match_key(entry: dict) -> tuple[str, str, str]:
    """条目的匹配键：``(归一主队, 归一客队, 开球日 UTC)``。

    日期取 ``kickoff_utc[:10]``：样本里是 ISO UTC 时间（``2025-09-20T15:00Z``），
    ESPN event 的 ``date`` 同样是 UTC ⇒ 同一场比赛两边日期一致。
    """
    home, away = entry_names(entry)
    date = str(entry.get("kickoff_utc") or "")[:10]
    return (normalize_team(home), normalize_team(away), date)


# ── scoreboard：建 event 索引 ─────────────────────────────────────────────

def american_to_decimal(value: Any) -> float | None:
    """美式赔率 → 十进制赔率。``-150`` ⇒ 1.667；``+200`` ⇒ 3.0。

    ESPN 的 ``moneyLine`` 是美式（正数 +X / 负数 -X），不是十进制 ⇒ 必须换算，
    直接当十进制用会把隐含概率算错一个数量级。

    非数字 / 0 ⇒ 返回 None（这一侧没价）。
    """
    try:
        ml = float(value)
    except (TypeError, ValueError):
        return None
    if ml == 0.0:
        return None
    decimal = 1.0 + ml / 100.0 if ml > 0 else 1.0 + 100.0 / abs(ml)
    return decimal if decimal > 1.0 else None


def _decimal_odds(side: Any) -> float | None:
    """⚠️ 终场/滚球价读取器 —— **只用于识别，绝不用于回填**。

    ``summary["odds"][].{home,draw,away}TeamOdds.odds.value`` 是**赛果已知后的价格**
    （实测「多特蒙德 4-1 毕尔巴鄂」⇒ home 1/500、away 450/1）⇒ 用它回填会造出
    「概率更高的一方 100% 全胜」的假象。保留这个函数只为让
    ``_looks_like_live_odds`` 能识别并拒绝这类块。
    """
    if not isinstance(side, dict):
        return None
    odds = side.get("odds")
    candidate = odds if isinstance(odds, dict) else side
    if "value" not in candidate:
        return None
    try:
        value = float(candidate.get("value"))
    except (TypeError, ValueError):
        return None
    if value <= 1.0:
        return None
    handicap = candidate.get("handicap")
    if handicap is not None:
        try:
            if float(handicap) != 0.0:
                return None
        except (TypeError, ValueError):
            return None
    return value


def _looks_like_live_odds(payload: dict) -> bool:
    """这份 summary 的 ``odds[]`` 是不是只有终场/滚球价（没有任何赛前价）。

    判据：``odds[]`` 里出现「隐含概率 > 97%」的侧 —— 赛前不可能有 97% 的确定热门，
    而终场价几乎必然出现（1.002 / 1.008 ⇒ 99.8%）。
    """
    for block in payload.get("odds") or []:
        if not isinstance(block, dict):
            continue
        for key in ("homeTeamOdds", "drawOdds", "awayTeamOdds"):
            value = _decimal_odds(block.get(key))
            if value is not None and (1.0 / value) > MAX_PREMATCH_IMPLIED:
                return True
    return False


def fetch_scoreboard_events(slug: str, dates: str, fetcher: FetchJson,
                            timeout: float, limiter: RateLimiter) -> list[dict]:
    """抓 scoreboard 的 events 列表。``dates`` 支持 ``YYYY``（整年）与 ``YYYYMMDD``（单日）。"""
    url = SCOREBOARD_URL.format(slug=slug, dates=dates, limit=SCOREBOARD_LIMIT)
    limiter.wait()
    payload = fetcher(url, timeout)
    if not isinstance(payload, dict):
        return []
    events = payload.get("events")
    return events if isinstance(events, list) else []


def index_events(events: list, league: str, slug: str) -> dict[tuple[str, str, str], EventRef]:
    """events → ``(归一主队, 归一客队, 日期)`` → event id 索引。

    同一键重复出现时**保留先到的**（同一天同两队出现两场 = 赛程异常，宁缺勿错配）。
    """
    index: dict[tuple[str, str, str], EventRef] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("id") or "")
        date = str(event.get("date") or "")[:10]
        competitions = event.get("competitions") or []
        competition = competitions[0] if competitions and isinstance(competitions[0], dict) else {}
        competitors = competition.get("competitors") or []
        home = next((c for c in competitors if isinstance(c, dict) and c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if isinstance(c, dict) and c.get("homeAway") == "away"), None)
        if not event_id or not home or not away:
            continue
        home_name = str((home.get("team") or {}).get("displayName") or "")
        away_name = str((away.get("team") or {}).get("displayName") or "")
        key = (normalize_team(home_name), normalize_team(away_name), date)
        if not key[0] or not key[1] or not date:
            continue
        index.setdefault(key, EventRef(
            event_id=event_id,
            league=league,
            slug=slug,
            date=date,
            home_team_id=str((home.get("team") or {}).get("id") or ""),
            away_team_id=str((away.get("team") or {}).get("id") or ""),
            home_score=str(home.get("score") or ""),
            away_score=str(away.get("score") or ""),
        ))
    return index


def needed_years(entries: list[dict]) -> dict[str, set[str]]:
    """按 league 归拢需要哪几个日历年（scoreboard 的 ``dates=YYYY`` 整年拉）。

    只统计**已知 league** 且 ``kickoff_utc`` 前 4 位是年份的条目 ⇒
    未知 league 不会被拿去猜 slug。
    """
    years: dict[str, set[str]] = {}
    for entry in entries:
        league = str(entry.get("league") or "")
        if league not in LEAGUE_SLUGS:
            continue
        year = str(entry.get("kickoff_utc") or "")[:4]
        if len(year) < 4 or not year.isdigit():
            continue
        years.setdefault(league, set()).add(year)
    return years


def build_event_index(entries: list[dict], fetcher: FetchJson, timeout: float,
                      limiter: RateLimiter) -> tuple[dict[tuple[str, str, str], EventRef], list[str]]:
    """抓 scoreboard 建索引。返回 ``(索引, 失败批次描述)``。

    单个批次（league×年）失败不拖垮整批：记录失败原因继续，
    最后由调用方按失败率决定退出码。
    """
    index: dict[tuple[str, str, str], EventRef] = {}
    failures: list[str] = []
    for league, years in sorted(needed_years(entries).items()):
        slug = LEAGUE_SLUGS[league]
        for year in sorted(years):
            try:
                events = fetch_scoreboard_events(slug, year, fetcher, timeout, limiter)
            except Exception as exc:  # noqa: BLE001 — 单批次失败不能中断整批
                failures.append(f"{league}/{year}: {type(exc).__name__}: {exc}")
                continue
            index.update(index_events(events, league, slug))
    return index, failures


# ── summary：取赛前十进制赔率 ─────────────────────────────────────────────

def _plausible_prematch(decimals: tuple[float, float, float]) -> bool:
    """三侧十进制赔率构成的 1x2 是否**讲得通**（赛前口径）。

    实测踩过的坑：ESPN 偶尔会给出**不自洽**的块（同一场比赛
    ``home 3.75 / draw 1.364 / away 17.0`` ⇒ 隐含概率 27% / 73% / 6%）——
    平局 1.364 意味着「平局概率 73%」，足球不可能；抽水和（1.06）却看着正常，
    所以**光看抽水抓不住它**，必须单独盯平局价。

    判定：
    - 抽水和 ∈ [1.0, 1.5]（> 1 是抽水，< 1 是数据有问题；强弱悬殊的场次长赔被压贵，
      抽水能到 1.4 ⇒ 上限不能按 1.35 卡）；
    - 平局价 ≥ 2.0（足球联赛 1x2 的平局价下限）；
    - **任一侧隐含概率 ≤ 0.97**：赛前不存在 97% 以上的确定热门 ——
      这条专门拦终场/滚球价（实测那种价是 1.002 ⇒ 隐含 99.8%）。
    """
    home, draw, away = decimals
    overround = (1.0 / home) + (1.0 / draw) + (1.0 / away)
    if not (MIN_OVERROUND <= overround <= MAX_OVERROUND):
        return False
    if draw < MIN_DECIMAL_DRAW_ODDS:
        return False
    return max(1.0 / home, 1.0 / draw, 1.0 / away) <= MAX_PREMATCH_IMPLIED


def _team_id(side: Any) -> str:
    """取一侧的 ESPN team id（``teamId`` 或 ``team.$ref`` 里的 teams/<id>）。"""
    if not isinstance(side, dict):
        return ""
    team_id = str(side.get("teamId") or "").strip()
    if team_id:
        return team_id
    ref = (side.get("team") or {}).get("$ref") if isinstance(side.get("team"), dict) else ""
    if isinstance(ref, str) and "teams/" in ref:
        tail = ref.split("teams/", 1)[1]
        return tail.split("?", 1)[0].split("/", 1)[0].strip()
    return ""


def _orientation_ok(block: dict, ref: EventRef) -> bool:
    """赔率块的主/客方向必须和 event 的主/客一致，否则整块丢掉。

    ESPN 的赔率块用 ``teamId`` / ``team.$ref`` 指明两侧是谁；拿不到 id 就跳过
    这道检查（宁可少一道校验，也不要因为 id 解析不出来把好数据误杀）。
    """
    if not ref.home_team_id or not ref.away_team_id:
        return True
    home_id = _team_id(block.get("homeTeamOdds"))
    away_id = _team_id(block.get("awayTeamOdds"))
    if not home_id or not away_id:
        return True
    return home_id == ref.home_team_id and away_id == ref.away_team_id


def _pickcenter_decimals(payload: dict, ref: EventRef) -> tuple[float, float, float] | None:
    """从 ``summary["pickcenter"][]`` 取**赛前** 1x2（美式 ⇒ 换算十进制）。

    这是 ESPN 比赛页赔率表的字段，**赛前价**。实测同一场比赛：
    ``pickcenter`` 给 ``+180 / +250 / +145``（讲得通），
    而 ``odds[]`` 给 ``501 / 21 / 1.015``（终场价，讲不通）⇒ 必须用这个字段。
    """
    blocks = payload.get("pickcenter")
    if not isinstance(blocks, list):
        return None
    fallback: tuple[float, float, float] | None = None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        decimals = (
            american_to_decimal((block.get("homeTeamOdds") or {}).get("moneyLine")),
            american_to_decimal((block.get("drawOdds") or {}).get("moneyLine")),
            american_to_decimal((block.get("awayTeamOdds") or {}).get("moneyLine")),
        )
        if any(value is None for value in decimals):
            continue
        if not _orientation_ok(block, ref):
            continue
        if not _plausible_prematch(decimals):  # type: ignore[arg-type]
            continue
        provider = block.get("provider")
        provider_id = str(provider.get("id")) if isinstance(provider, dict) else ""
        if provider_id == PREFERRED_PROVIDER_ID:
            return decimals  # type: ignore[return-value]
        if fallback is None:
            fallback = decimals  # type: ignore[assignment]
    return fallback


def _odds_block_prematch_decimals(payload: dict, ref: EventRef) -> tuple[float, float, float] | None:
    """兜底：从 ``summary["odds"][]`` 里带**块级 moneyLine** 的块取赛前 1x2。

    DraftKings / ESPN BET 的块长这样：``homeTeamOdds.moneyLine = -1400``（美式），
    另有 ``open`` / ``current`` 子结构。**"ESPN BET - Live Odds" 是滚球 ⇒ 直接跳过。**
    """
    blocks = payload.get("odds")
    if not isinstance(blocks, list):
        return None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        provider = block.get("provider")
        provider_name = str(provider.get("name") or "") if isinstance(provider, dict) else ""
        if "live" in provider_name.lower():
            continue
        decimals: list[float] = []
        for key in ("homeTeamOdds", "drawOdds", "awayTeamOdds"):
            side = block.get(key)
            if not isinstance(side, dict):
                decimals = []
                break
            money_line = side.get("moneyLine")
            if isinstance(money_line, dict):
                decimal = money_line.get("decimal")
                if decimal is None:
                    decimal = american_to_decimal(money_line.get("american"))
            else:
                decimal = american_to_decimal(money_line)
            if not isinstance(decimal, (int, float)):
                decimals = []
                break
            decimals.append(float(decimal))
        if len(decimals) != 3:
            continue
        triple = (decimals[0], decimals[1], decimals[2])
        if not _orientation_ok(block, ref):
            continue
        if not _plausible_prematch(triple):
            continue
        return triple
    return None


def fetch_summary_decimal_odds(ref: EventRef, fetcher: FetchJson, timeout: float,
                               limiter: RateLimiter) -> tuple[float | None, float | None, float | None]:
    """取 summary 的**赛前** 1x2 赔率（十进制）。

    **任一侧缺失 / 整块不自洽就整场返回 (None, None, None)** —— 半边的赔率不能去水，
    填一半的「真实概率」比不填更危险（下游会当成完整样本用）。

    取数顺序：
    1. ``summary["pickcenter"][]``（**首选**：ESPN 比赛页赔率表，赛前美式价）；
    2. ``summary["odds"][]`` 里带块级 ``moneyLine`` 的非滚球块（兜底）。

    ⚠️ **绝不读** ``odds[].{home,draw,away}TeamOdds.odds.value`` ——
    那是终场/滚球价（实测「多特蒙德 4-1 毕尔巴鄂」⇒ home 1/500），
    用它回填会造出「热门 100% 全胜」的假象（详见模块 docstring）。
    """
    url = SUMMARY_URL.format(slug=ref.slug, event_id=ref.event_id)
    limiter.wait()
    payload = fetcher(url, timeout)
    if not isinstance(payload, dict):
        return None, None, None
    decimals = _pickcenter_decimals(payload, ref)
    if decimals is None:
        decimals = _odds_block_prematch_decimals(payload, ref)
    if decimals is None:
        return None, None, None
    return decimals


# ── 写入 ──────────────────────────────────────────────────────────────────

def _is_missing(value: Any) -> bool:
    """字段是否「还没填」。

    ``None`` / 空白串算没填；``0.0`` 与 ``False`` 是**有效值**（不是缺失）——
    所以判定用「是不是 None / 空串」，而不是「是不是 falsy」。
    """
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


def apply_odds(entry: dict, decimals: tuple[float, float, float]) -> str:
    """把十进制赔率换算成隐含概率 + 去水真实概率写进条目。

    返回 ``"filled"``（写了字段）/ ``"already"``（字段本来就有值，一个都没动）
    / ``"failed"``（去水结果不可用，整场放弃）。

    - 只填**缺**的字段：已有非空值的字段一律不动 ⇒ 幂等。
    - ``odds_data_available`` 是布尔标记：``false`` 表示「没有赔率」⇒ 置 ``true``；
      已经 ``true`` 就不动。
    """
    home_dec, draw_dec, away_dec = decimals
    home_impl = 1.0 / home_dec
    draw_impl = 1.0 / draw_dec
    away_impl = 1.0 / away_dec
    home_true, draw_true, away_true = remove_vig(home_impl, draw_impl, away_impl)
    if home_true is None or draw_true is None or away_true is None:
        return "failed"
    values = {
        "home_ml_implied": home_impl,
        "draw_implied": draw_impl,
        "away_ml_implied": away_impl,
        "home_true_prob": home_true,
        "draw_true_prob": draw_true,
        "away_true_prob": away_true,
    }
    written = 0
    for name, value in values.items():
        if _is_missing(entry.get(name)):
            entry[name] = round(value, 4)
            written += 1
    if entry.get("odds_data_available") is not True:
        entry["odds_data_available"] = True
        written += 1
    return "filled" if written else "already"


def write_json_atomic(path: Path, data: list, newline: str) -> None:
    """原子写回样本文件。

    先写同目录临时文件再 ``replace`` ⇒ 中途崩溃不会把样本文件写成半截 JSON。
    行尾沿用原文件（Windows 上原文件是 CRLF，别为了写盘把整份文件的行尾改一遍）。
    """
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if newline == "\r\n":
        text = text.replace("\n", "\r\n")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    tmp.replace(path)


# ── 统计合理性闸 ───────────────────────────────────────────────────────────

def _score_outcome(score: Any) -> str:
    """把 ``"3-0"`` 这样的比分解析成 ``home`` / ``away`` / ``draw`` / ``unknown``。"""
    text = str(score or "")
    if "-" not in text:
        return "unknown"
    left, _, right = text.partition("-")
    try:
        home_score = int(left.strip())
        away_score = int(right.strip())
    except ValueError:
        return "unknown"
    if home_score > away_score:
        return "home"
    if away_score > home_score:
        return "away"
    return "draw"


@dataclass
class SampleAudit:
    """回填结果的统计体检结论。"""

    ok: bool = True
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "problems": list(self.problems), "notes": list(self.notes)}


def validate_filled_sample(entries: list[dict]) -> SampleAudit:
    """体检：回填后的样本像不像**真实**的赛前赔率。

    ⚠️ 基线不能拍脑袋 —— 这里全部用**数据自身隐含的分布**做参照：

    1. **分布校准检验**（主判据）：把样本里赔率隐含的主胜/平局/客胜概率取平均，
       与**实际比分分布**逐格比（z 检验）。
       校准良好的市场 ⇒ 两者应当接近（生产实测：隐含 45.3/22.4/32.2 vs
       实际 47.2/19.8/33.0，各格差 ≤ 2.6pp）。
       终场价会把这三格打歪（它给「谁赢谁」≈ 99.8%）⇒ 判刑。
    2. **热门条件胜率检验**：热门在非平局场次里的胜率，期望值是
       ``Σ max(h, a) / Σ (h + a)``（**从这批价格自己算出来**，不是拍脑袋的常数）。
       ⚠️ 真实值约 **0.73** 而不是常被引用的 0.55 —— 因为 0.55 是「含平局」的口径，
       而这里排除了平局。实测 243/328 = 74.1% vs 期望 72.8% ⇒ 只差 0.5σ，完全正常。
    3. **隐含概率的分布形状**：真实赛前市场里「热门 ≥ 0.90」的场次占比很低
       （生产实测 6.1%）；终场价会让它接近 100% ⇒ 判刑。

    样本太小（< 30 场有赔率）时不判刑，只记 note —— 小样本本来就没有统计效力。
    """
    audit = SampleAudit()
    with_odds = [e for e in entries if e.get("home_true_prob") is not None]
    if len(with_odds) < MIN_AUDIT_SAMPLES:
        audit.notes.append(f"带赔率样本只有 {len(with_odds)} 场"
                           f"（< {MIN_AUDIT_SAMPLES}）⇒ 统计体检无效力，只做记录")
        return audit

    implied = {"home": 0.0, "draw": 0.0, "away": 0.0}
    actual = {"home": 0, "draw": 0, "away": 0}
    counted = 0
    decisive = 0
    fav_wins = 0
    sum_fav = 0.0
    sum_not_draw = 0.0
    high_conf = 0
    for entry in with_odds:
        home_prob = entry.get("home_true_prob")
        draw_prob = entry.get("draw_true_prob")
        away_prob = entry.get("away_true_prob")
        if not all(isinstance(v, (int, float)) for v in (home_prob, draw_prob, away_prob)):
            continue
        outcome = _score_outcome(entry.get("score"))
        if outcome not in ("home", "draw", "away"):
            continue
        counted += 1
        implied["home"] += float(home_prob)
        implied["draw"] += float(draw_prob)
        implied["away"] += float(away_prob)
        actual[outcome] += 1
        if max(float(home_prob), float(away_prob)) >= HIGH_CONFIDENCE_THRESHOLD:
            high_conf += 1
        if outcome != "draw":
            decisive += 1
            favorite = max(float(home_prob), float(away_prob))
            sum_fav += favorite
            sum_not_draw += float(home_prob) + float(away_prob)
            favorite_is_home = float(home_prob) >= float(away_prob)
            if (favorite_is_home and outcome == "home") or (not favorite_is_home and outcome == "away"):
                fav_wins += 1

    if counted < MIN_AUDIT_SAMPLES:
        audit.notes.append(f"可用于体检的样本只有 {counted} 场 ⇒ 不判刑")
        return audit

    # ── 1. 分布校准检验（隐含 vs 实际，3 格 z 检验）──
    for cell, label in (("home", "主胜"), ("draw", "平局"), ("away", "客胜")):
        implied_rate = implied[cell] / counted
        actual_rate = actual[cell] / counted
        stderr = math.sqrt(max(implied_rate * (1.0 - implied_rate), 1e-12) / counted)
        z = abs(actual_rate - implied_rate) / stderr
        audit.notes.append(f"{label}：赔率隐含 {implied_rate:.1%} vs 实际 {actual_rate:.1%}"
                           f"（z = {z:.2f}）")
        if z > CALIBRATION_Z_LIMIT:
            audit.ok = False
            audit.problems.append(
                f"{label}的实际分布 {actual_rate:.1%} 与赔率隐含的 {implied_rate:.1%} "
                f"偏离过大（z = {z:.2f} > {CALIBRATION_Z_LIMIT}）"
                f"⇒ 这批赔率与真实赛果不自洽，疑似非赛前价")

    # ── 2. 热门条件胜率（期望值由价格自身算出）──
    if decisive >= MIN_AUDIT_SAMPLES and sum_not_draw > 0:
        expected = sum_fav / sum_not_draw
        observed = fav_wins / decisive
        stderr = math.sqrt(max(expected * (1.0 - expected), 1e-12) / decisive)
        z = abs(observed - expected) / stderr
        audit.notes.append(f"热门条件胜率 {fav_wins}/{decisive} = {observed:.1%}"
                           f"（由价格自身算出的期望 {expected:.1%}，z = {z:.2f}）")
        if z > CALIBRATION_Z_LIMIT:
            audit.ok = False
            audit.problems.append(
                f"热门条件胜率 {observed:.1%} 与这批价格自身的期望 {expected:.1%} "
                f"偏离过大（z = {z:.2f} > {CALIBRATION_Z_LIMIT}）⇒ 赔率与赛果不自洽")

    # ── 3. 隐含概率的分布形状（终场价的指纹）──
    high_ratio = high_conf / counted
    audit.notes.append(f"隐含概率 ≥ {HIGH_CONFIDENCE_THRESHOLD:.2f} 的场次占比 {high_ratio:.1%}")
    if high_ratio > HIGH_CONFIDENCE_MAX_RATIO:
        audit.ok = False
        audit.problems.append(
            f"{high_ratio:.1%} 的场次隐含概率 ≥ {HIGH_CONFIDENCE_THRESHOLD:.2f}"
            f"（阈值 {HIGH_CONFIDENCE_MAX_RATIO:.0%}）⇒ 真实赛前市场不会有这么多「几乎确定」的热门")

    if not with_odds:
        audit.notes.append("没有任何带赔率的样本 ⇒ 本次无可体检数据")
    return audit


# ── 主流程 ────────────────────────────────────────────────────────────────

def run_backfill(entries: list[dict], fetcher: FetchJson, timeout: float, sleep: float,
                 max_workers: int, stats: BackfillStats | None = None) -> tuple[BackfillStats, list[str]]:
    """跑一遍补齐流程（就地修改 entries）。返回 ``(统计, 失败批次描述)``。

    流程：scoreboard 建索引 → 条目精确匹配 → **比分一致性校验** → 并发抓 summary
    → 取**赛前**赔率 → 去水 → 只填缺的。
    """
    if stats is None:
        stats = BackfillStats()
    stats.total = len(entries)
    limiter = RateLimiter(sleep)
    index, failures = build_event_index(entries, fetcher, timeout, limiter)

    # 逐条匹配：精确键命中才算数，认不上就跳过计数（绝不模糊匹配）。
    matched: list[tuple[dict, EventRef]] = []
    for entry in entries:
        stats.leagues[str(entry.get("league") or "")] += 1
        league = str(entry.get("league") or "")
        if league not in LEAGUE_SLUGS:
            stats.unknown_league += 1
            stats.unmatched += 1
            continue
        ref = index.get(match_key(entry))
        if ref is None:
            stats.unmatched += 1
            continue
        # 比分一致性：赔率所属 event 的最终比分必须与样本 score 一致。
        # 同队名同日期的不同赛事（同名梯队 / 其它杯赛）能骗过纯键匹配 ⇒ 补这道闸。
        sample_score = str(entry.get("score") or "")
        event_score = f"{ref.home_score}-{ref.away_score}"
        if ref.home_score and ref.away_score and sample_score and sample_score != event_score:
            stats.score_mismatch += 1
            stats.unmatched += 1
            continue
        stats.matched += 1
        matched.append((entry, ref))

    # summary 按 event id 去重后并发抓（同一 event 只请求一次）。
    unique_refs: dict[str, EventRef] = {ref.event_id: ref for _entry, ref in matched}
    odds_by_event: dict[str, tuple[float | None, float | None, float | None]] = {}
    failed_events: set[str] = set()

    def _fetch(ref: EventRef) -> tuple[str, tuple[float | None, float | None, float | None] | None]:
        try:
            return ref.event_id, fetch_summary_decimal_odds(ref, fetcher, timeout, limiter)
        except Exception:  # noqa: BLE001 — 单场失败只影响这一场
            return ref.event_id, None

    if unique_refs:
        workers = max(1, int(max_workers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for event_id, decimals in pool.map(_fetch, unique_refs.values()):
                if decimals is None:
                    failed_events.add(event_id)
                else:
                    odds_by_event[event_id] = decimals

    for entry, ref in matched:
        if ref.event_id in failed_events:
            stats.fetch_failed += 1
            continue
        decimals = odds_by_event.get(ref.event_id)
        if decimals is None or any(value is None for value in decimals):
            # 命中了 event 但 summary 里没有完整的三侧赔率 ⇒ 整场不填。
            stats.no_odds += 1
            continue
        status = apply_odds(entry, decimals)
        if status == "filled":
            stats.filled += 1
        elif status == "already":
            # 字段本来就有值（重跑场景）⇒ 一个都不动，这正是幂等的体现。
            stats.already_filled += 1
        else:
            # 去水结果不可用（理论上不该发生）⇒ 整场不填，计入「命中但未填」。
            stats.no_odds += 1
    return stats, failures


# ── CLI ───────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backfill_hist_odds.py",
        description="用 ESPN summary 的历史十进制赔率补齐 historical_past_matches.json 的真实概率（默认干跑）",
    )
    parser.add_argument("--file", default=DEFAULT_FILE,
                        help=f"样本文件路径（相对仓库根解析；默认 {DEFAULT_FILE}）")
    parser.add_argument("--execute", action="store_true",
                        help="真正写回文件（默认干跑，不写盘）")
    parser.add_argument("--dry-run", action="store_true",
                        help="显式声明干跑（默认行为，与 --execute 互斥）")
    parser.add_argument("--leagues", default="",
                        help="只处理指定联赛，逗号分隔（默认全部；可选：" + ",".join(LEAGUE_SLUGS) + "）")
    parser.add_argument("--limit", type=int, default=0,
                        help="只处理前 N 条（调试用；0 = 不限）")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP,
                        help="每次请求之间的间隔秒（礼貌限速，默认 0.3）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="单请求超时秒（默认 25）")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS,
                        help="summary 并发线程数（默认 4；总速率仍受 --sleep 约束）")
    return parser


def resolve_file(raw: str) -> Path:
    """``--file`` 解析：相对路径按**仓库根**解析（脚本可能从任意目录启动）。"""
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return (_REPO_ROOT / path).resolve()


def parse_leagues(raw: str) -> tuple[set[str], str]:
    """解析 ``--leagues``。返回 ``(集合, 错误信息)``；错误信息非空即参数非法。"""
    wanted = {part.strip() for part in raw.split(",") if part.strip()}
    unknown = sorted(wanted - set(LEAGUE_SLUGS))
    if unknown:
        return set(), (f"未知联赛：{','.join(unknown)}；可选：{','.join(LEAGUE_SLUGS)}")
    return wanted, ""


def print_stats(stats: BackfillStats, path: Path, execute: bool) -> None:
    """打印逐条可核对的统计（干跑也照打，方便对账）。"""
    print(f"[backfill] 模式：{'execute（写回）' if execute else 'dry-run（不写盘）'}")
    print(f"[backfill] 文件：{path}")
    print(f"[backfill] 总条数：{stats.total}")
    print(f"[backfill] 命中 event：{stats.matched}")
    print(f"[backfill] 成功填赔率：{stats.filled}")
    print(f"[backfill] 未匹配到 event：{stats.unmatched}")
    print(f"[backfill] 抓取失败：{stats.fetch_failed}")
    print(f"[backfill] 其中未知 league 跳过：{stats.unknown_league}")
    print(f"[backfill] 命中但无完整赔率（未填）：{stats.no_odds}")
    print(f"[backfill] 命中但已有值（未动）：{stats.already_filled}")
    print(f"[backfill] 跳过（非 dict 条目）：{stats.skipped_non_dict}")
    print(f"[backfill] 跳过（联赛过滤）：{stats.skipped_league_filter}")
    print(f"[backfill] 命中但**比分不一致**（未填）：{stats.score_mismatch}")
    print(f"[backfill] 联赛分布：{dict(stats.leagues)}")
    print(f"[backfill] 自洽：总数 = 命中 + 未匹配 = {stats.total} = {stats.matched} + {stats.unmatched}；"
          f"命中 = 填上 + 无赔率 + 已有值 + 失败 = {stats.matched} = "
          f"{stats.filled} + {stats.no_odds} + {stats.already_filled} + {stats.fetch_failed}")
    print(f"[backfill] 统计：{json.dumps(stats.as_dict(), ensure_ascii=False)}")


def main(argv: list[str] | None = None, fetcher: FetchJson | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.dry_run and args.execute:
        print("[backfill] --dry-run 与 --execute 互斥，请只给一个", file=sys.stderr)
        return 2
    if args.limit < 0:
        print("[backfill] --limit 不能为负", file=sys.stderr)
        return 2
    if args.sleep < 0 or args.timeout <= 0:
        print("[backfill] --sleep / --timeout 非法", file=sys.stderr)
        return 2
    if args.max_workers < 1:
        print("[backfill] --max-workers 至少为 1", file=sys.stderr)
        return 2
    leagues, league_err = parse_leagues(args.leagues)
    if league_err:
        print(f"[backfill] {league_err}", file=sys.stderr)
        return 2

    path = resolve_file(args.file)
    if not path.exists():
        print(f"[backfill] 样本文件不存在：{path}\n"
              f"[backfill] 该文件是运行期产物（.gitignore 忽略），本机可能没有；"
              f"请先在生产机生成，或用 --file 指向本机副本。", file=sys.stderr)
        return 2
    try:
        raw = path.read_bytes()
    except OSError as exc:
        print(f"[backfill] 读取失败：{exc}", file=sys.stderr)
        return 2
    newline = "\r\n" if b"\r\n" in raw[:8192] else "\n"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"[backfill] 文件不是合法 JSON：{exc}", file=sys.stderr)
        return 2
    if not isinstance(data, list):
        print("[backfill] 文件顶层必须是 list[dict]", file=sys.stderr)
        return 2

    stats = BackfillStats()
    entries: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            stats.skipped_non_dict += 1
            continue
        if leagues and str(item.get("league") or "") not in leagues:
            stats.skipped_league_filter += 1
            continue
        entries.append(item)
    if args.limit > 0:
        entries = entries[:args.limit]
    stats.total = len(entries)

    resolved_fetcher: FetchJson = fetcher if fetcher is not None else fetch_json
    stats, failures = run_backfill(
        entries,
        resolved_fetcher,
        timeout=args.timeout,
        sleep=args.sleep,
        max_workers=args.max_workers,
        stats=stats,
    )
    for failure in failures:
        print(f"[backfill] scoreboard 批次失败：{failure}", file=sys.stderr)

    print_stats(stats, path, args.execute)

    attempted = stats.matched
    failure_rate = (stats.fetch_failed / attempted) if attempted else 0.0
    if failures or failure_rate > MAX_FAILURE_RATE:
        print(f"[backfill] 数据不全：抓取失败率 {failure_rate:.1%}（阈值 {MAX_FAILURE_RATE:.0%}；"
              f"scoreboard 批次失败 {len(failures)} 个，summary 失败 {stats.fetch_failed} 条）⇒ 请重跑",
              file=sys.stderr)
        return 1

    # ── 统计合理性闸：像不像**真实赛前赔率**？不像就拒绝写盘 ──
    audit = validate_filled_sample(entries)
    print(f"[backfill] 统计体检：{json.dumps(audit.as_dict(), ensure_ascii=False)}")
    if not audit.ok:
        print("[backfill] ❌ 统计体检不通过 ⇒ 拒绝写盘：\n  - " + "\n  - ".join(audit.problems),
              file=sys.stderr)
        return 1

    if args.execute:
        write_json_atomic(path, data, newline)
        print(f"[backfill] 已写回：{path}（更新 {stats.filled} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
