#!/usr/bin/env python3
"""ASEL-BACKFILL-VALIDATE: 独立校验 5 个回填 JSON 的完整度。

只读 scripts/results/backfill_*.json，独立计算，不读任何 .report.txt 自述。
全部 PASS -> 退出码 0；任一 FAIL -> 退出码 1。
"""
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill_validate")

RESULTS = Path(__file__).resolve().parent / "results"
REPORT = Path("/home/ubuntu/league-v2/repo/.omp-logs/ASEL-BACKFILL-VALIDATE.report.txt")

# 联赛 -> (文件名后缀, api_football_id, 期望场数)
# 期望场数按联赛真实球队数：20 队 = 380/季；18 队 = 306/季。
# 德甲恒为 18 队；法甲 2022-23 为 20 队(380)，2023-24/2024-25 为 18 队(306)。
LEAGUES = {
    "epl":        ("epl",        39, 380 * 3),
    "laliga":     ("laliga",    140, 380 * 3),
    "seriea":     ("seriea",    135, 380 * 3),
    "bundesliga": ("bundesliga", 78, 306 * 3),
    "ligue1":     ("ligue1",     61, 380 + 306 + 306),
}

REQUIRED_KEYS = [
    "fixture.id", "fixture.date", "league.id",
    "teams.home.name", "teams.away.name",
    "score.fulltime.home", "score.fulltime.away",
]

TOLERANCE = 0.03  # 容许 ±3%


def season_of(date_str: str) -> int:
    """按赛季起始年归类：8 月及以后属当年赛季，1-7 月属上一赛季。"""
    year = int(date_str[:4])
    month = int(date_str[5:7])
    return year if month >= 8 else year - 1


def check_scale(fixtures, expected, league):
    n = len(fixtures)
    floor = int(expected * (1 - TOLERANCE))
    ok = n >= floor
    log.info("[%s] 规模: %s fixtures=%d 期望=%d 下限=%d", league,
             "PASS" if ok else "FAIL", n, expected, floor)
    return ok, f"fixtures={n} 期望={expected} 下限={floor}"


def check_structure(fixtures, league):
    missing = []
    for i, fx in enumerate(fixtures):
        for k in REQUIRED_KEYS:
            if k not in fx:
                missing.append((i, k))
    ok = not missing
    log.info("[%s] 结构: %s 缺键=%d", league, "PASS" if ok else "FAIL", len(missing))
    return ok, f"缺键={len(missing)}" + (f" 首例={missing[0]}" if missing else "")


def check_seasons(fixtures, league):
    counts = {}
    for fx in fixtures:
        s = season_of(fx["fixture.date"])
        counts[s] = counts.get(s, 0) + 1
    missing = [y for y in (2022, 2023, 2024) if y not in counts]
    ok = not missing
    detail = " ".join(f"{y}={counts.get(y, 0)}" for y in (2022, 2023, 2024))
    log.info("[%s] 赛季覆盖: %s %s", league, "PASS" if ok else "FAIL", detail)
    return ok, detail


def check_dedup(fixtures, league):
    n = len(fixtures)
    uniq = len({fx["fixture.id"] for fx in fixtures})
    dup_rate = (n - uniq) / n if n else 0.0
    ok = dup_rate <= 0.01
    log.info("[%s] 去重: %s 总数=%d 唯一=%d 重复率=%.4f%%",
             league, "PASS" if ok else "FAIL", n, uniq, dup_rate * 100)
    return ok, f"总数={n} 唯一={uniq} 重复率={dup_rate * 100:.2f}%"


def check_score(fixtures, league):
    bad = []
    for i, fx in enumerate(fixtures):
        h = fx["score.fulltime.home"]
        a = fx["score.fulltime.away"]
        for side, v in (("home", h), ("away", a)):
            if v is None:
                continue
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                bad.append((i, side, v))
    ok = not bad
    log.info("[%s] 评分完整: %s 非法=%d", league, "PASS" if ok else "FAIL", len(bad))
    return ok, f"非法={len(bad)}" + (f" 首例={bad[0]}" if bad else "")


def check_league(fixtures, league, suffix, api_id):
    bad = []
    for i, fx in enumerate(fixtures):
        if fx["league.id"] != api_id:
            bad.append((i, fx["league.id"]))
    ok = not bad
    log.info("[%s] 联赛自洽: %s league.id=%s 期望=%s", league,
             "PASS" if ok else "FAIL", api_id, api_id)
    return ok, f"league.id={api_id} 期望={api_id} 异常={len(bad)}"


def main() -> int:
    all_ok = True
    lines = []
    for league, (suffix, api_id, expected) in LEAGUES.items():
        path = RESULTS / f"backfill_{league}_2022-2024.json"
        log.info("===== %s (%s) =====", league, path.name)
        lines.append(f"===== {league} ({path.name}) =====")
        with open(path) as fh:
            data = json.load(fh)
        fixtures = data.get("fixtures", [])

        checks = [
            ("规模", check_scale(fixtures, expected, league)),
            ("结构", check_structure(fixtures, league)),
            ("赛季覆盖", check_seasons(fixtures, league)),
            ("去重", check_dedup(fixtures, league)),
            ("评分完整", check_score(fixtures, league)),
            ("联赛自洽", check_league(fixtures, league, suffix, api_id)),
        ]
        # 联赛自洽另含顶层 league 字段 == 文件名后缀
        top_league_ok = data.get("league") == suffix
        log.info("[%s] 顶层league字段: %s league=%s 期望=%s", league,
                 "PASS" if top_league_ok else "FAIL", data.get("league"), suffix)
        checks.append(("顶层league字段", (top_league_ok,
                      f"league={data.get('league')} 期望={suffix}")))

        for name, (ok, detail) in checks:
            lines.append(f"  [{name}] {'PASS' if ok else 'FAIL'} {detail}")
            if not ok:
                all_ok = False

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n")
    log.info("报告已写入 %s", REPORT)
    log.info("总体: %s", "ALL PASS" if all_ok else "HAS FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())