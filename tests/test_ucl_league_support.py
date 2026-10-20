"""建议 1 · 模型补欧冠（路线图《产品路线图-竞彩闭环-20261020.md》§建议 1）。

锁住本轮新增/改动欧冠支持的关键契约（全部有 2026-10-20 实测依据）：

1. ``LEAGUE_CONFIG["ucl"]`` 字段正确：``data_source="espn"``、``api_football_id=2``、
   ``espn_slug="uefa.champions"``、**故意不设 ``league_id``**（football-data 没有欧冠）、
   ``espn_season_fetch=True``、``monte_carlo=False``（两阶段赛制不适用积分榜式夺冠模拟）。
2. ``LEAGUE_DC_RHO["ucl"]`` / ``LEAGUE_LAMBDA_MULTIPLIER["ucl"]`` 有实测取值。
3. ``fetch_espn_season``：按 ``dates=<日历年>`` 抓三个自然年 → 按 id 去重 →
   只保留最近两个 ``season.year``（ESPN 的 ``dates`` 不吃区间，实测区间 400）。
4. ``fetch_events`` 在 ``espn_season_fetch=True`` 时走整季通道、忽略 dates_str。
5. ``parse_events``：``STATUS_FINAL_AET``（ESPN 加时赛终场）必须进 past 桶且有比分
   —— 原先只列 ``STATUS_FINAL_ET``，欧冠 2026 日历年实测有 2 场 AET 会被误判 in_progress。
6. 冷启动降档：``ucl`` 置信度钳到 0.5、note 含「冷启动」、星级最高 2-star；
   五大联赛不受影响。
7. 队名链：UCL 队的模型中文名 = 竞彩官方中文名（``references/team_cn_official.json``）。
8. web 侧：欧冠进主面板联赛白名单（模型有预测产出 ⇒ 走主面板，不是"临时赛事"附面板）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ── 1. LEAGUE_CONFIG ────────────────────────────────────────────────

def test_ucl_league_config_fields():
    from core.leagues import LEAGUE_CONFIG

    ucl = LEAGUE_CONFIG["ucl"]
    assert ucl["name"] == "UEFA Champions League"
    assert ucl["name_cn"] == "欧冠"
    assert ucl["data_source"] == "espn"
    assert ucl["api_football_id"] == 2
    assert ucl["espn_slug"] == "uefa.champions"
    assert ucl["espn_season_fetch"] is True
    assert ucl["host_country"] == "World"
    # football-data.org 没有欧冠 ⇒ 故意不设 league_id（设了回填器会拼出无效 URL）
    assert "league_id" not in ucl
    # 两阶段赛制 ⇒ 积分榜式夺冠模拟不适用
    assert ucl["monte_carlo"] is False


def test_ucl_league_params_present():
    from core.leagues import LEAGUE_CONFIG, LEAGUE_DC_RHO, LEAGUE_LAMBDA_MULTIPLIER

    assert LEAGUE_DC_RHO["ucl"] == 0.08
    assert LEAGUE_LAMBDA_MULTIPLIER["ucl"] == 3.7
    assert set(LEAGUE_DC_RHO) == set(LEAGUE_LAMBDA_MULTIPLIER) == set(LEAGUE_CONFIG) - {"nba"}


def test_ucl_has_confidence_note_and_cap():
    from core.leagues import LEAGUE_CONFIDENCE_CAP, LEAGUE_CONFIDENCE_NOTES

    note = LEAGUE_CONFIDENCE_NOTES["ucl"]
    assert "冷启动" in note
    # 不得谎称已用 FIFA 播种（实测 FIFA 排名是国家队，俱乐部不在其中）
    assert "FIFA" in note
    assert LEAGUE_CONFIDENCE_CAP["ucl"] == 0.5
    # 五大联赛不受冷启动降档影响
    for lg in ("epl", "laliga", "bundesliga", "seriea", "ligue1"):
        assert lg not in LEAGUE_CONFIDENCE_NOTES
        assert lg not in LEAGUE_CONFIDENCE_CAP


# ── 2/3. ESPN 整季取数 ─────────────────────────────────────────────

def _event(eid: str, year: int, slug: str = "league-phase") -> dict:
    return {"id": eid, "name": f"{eid} at X", "date": f"{year}-09-01T00:00Z",
            "season": {"year": year, "slug": slug}, "competitions": []}


def test_fetch_espn_season_years_dedupe_and_two_seasons(monkeypatch):
    """抓 [本年-1, 本年, 本年+1] 三个日历年；按 id 去重；只保留最近两个 season.year。"""
    from core.data import fetch

    calls: list[tuple[str, int]] = []

    def fake_fetch_espn(dates_str, league_slug="epl", limit=50):
        calls.append((dates_str, limit))
        year = int(dates_str)
        if year == 2026:
            return [_event("a", 2026), _event("b", 2026)]
        if year == 2025:
            return [_event("c", 2025), _event("a", 2026)]  # "a" 重复 → 应被去重
        return [_event("d", 2026)]

    monkeypatch.setattr(fetch, "fetch_espn", fake_fetch_espn)
    out = fetch.fetch_espn_season("uefa.champions",
                                  now_utc=datetime(2026, 10, 20, tzinfo=timezone.utc))

    assert [c[0] for c in calls] == ["2025", "2026", "2027"]
    assert all(c[1] == fetch.ESPN_SEASON_LIMIT for c in calls)   # limit 必须放大（实测 50 会截断）
    ids = [e["id"] for e in out]
    assert sorted(ids) == ["a", "b", "c", "d"]        # 4 个不同 id
    assert ids.count("a") == 1                       # "a" 在 2025/2026 两年都出现 → 按 id 去重
    assert {e["season"]["year"] for e in out} == {2025, 2026}   # 保留最近两个赛季


def test_fetch_espn_season_keeps_two_most_recent_seasons(monkeypatch):
    """season.year 保留最近两个（当前赛季给预测，上一赛季给 ELO 冷启动补历史）。"""
    from core.data import fetch

    def fake_fetch_espn(dates_str, league_slug="epl", limit=50):
        year = int(dates_str)
        return [_event(f"y{year}", year - 1)] if year == 2027 else [_event(f"y{year}", year)]

    monkeypatch.setattr(fetch, "fetch_espn", fake_fetch_espn)
    out = fetch.fetch_espn_season("uefa.champions",
                                  now_utc=datetime(2026, 10, 20, tzinfo=timezone.utc))
    years = {e["season"]["year"] for e in out}
    assert years == {2025, 2026}


def test_fetch_espn_season_year_failure_is_non_fatal(monkeypatch):
    """单年抓取失败不拖垮整季（其余年照常）。"""
    from core.data import fetch

    def fake_fetch_espn(dates_str, league_slug="epl", limit=50):
        if dates_str == "2025":
            raise RuntimeError("boom")
        return [_event(f"y{dates_str}", int(dates_str))]

    monkeypatch.setattr(fetch, "fetch_espn", fake_fetch_espn)
    out = fetch.fetch_espn_season("uefa.champions",
                                  now_utc=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert {e["id"] for e in out} == {"y2026", "y2027"}


def test_fetch_events_uses_season_channel_for_ucl(monkeypatch):
    """LEAGUE_CONFIG 里 espn_season_fetch=True 的联赛走整季通道、忽略 dates_str。"""
    from core.data import fetch

    seen: list[str] = []

    def fake_fetch_espn_season(league_slug, now_utc=None):
        seen.append(league_slug)
        return [_event("x", 2026)]

    monkeypatch.setattr(fetch, "fetch_espn_season", fake_fetch_espn_season)
    out = fetch.fetch_events("20260101-20261231", "ucl", "espn", whole_season=True)
    assert seen == ["uefa.champions"]
    assert len(out) == 1


# ── 4. 完赛状态 ────────────────────────────────────────────────────

def _espn_event(status: str, home: str = "Liverpool", away: str = "Everton",
                hs: str = "2", as_: str = "1") -> dict:
    return {
        "name": f"{away} at {home}",
        "date": "2026-10-20T16:45Z",
        "competitions": [{
            "status": {"type": {"name": status, "completed": status.startswith("STATUS_FINAL")
                                or status == "STATUS_FULL_TIME"}},
            "competitors": [
                {"homeAway": "home", "team": {"displayName": home, "abbreviation": "LIV"}, "score": hs},
                {"homeAway": "away", "team": {"displayName": away, "abbreviation": "EVE"}, "score": as_},
            ],
            "odds": [],
        }],
    }


@pytest.mark.parametrize("status", ["STATUS_FULL_TIME", "STATUS_FINAL_PEN",
                                    "STATUS_FINAL_ET", "STATUS_FINAL_AET"])
def test_finished_statuses_all_bucket_to_past_with_score(status):
    """ESPN 的 AET（加时终场）必须进 past 且带比分 —— 欧冠淘汰赛常见。"""
    from core.data.parse import parse_events

    past, future, in_prog = parse_events(
        [_espn_event(status)], datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc))
    assert len(past) == 1
    assert not in_prog
    assert past[0]["score"] == "2-1"


def test_live_status_stays_in_progress():
    from core.data.parse import parse_events

    past, future, in_prog = parse_events(
        [_espn_event("STATUS_IN_PROGRESS")], datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc))
    assert not past
    assert len(in_prog) == 1


# ── 5. 冷启动降档 ──────────────────────────────────────────────────

def test_cold_start_caps_confidence_and_stars():
    from core.predictor import _apply_league_confidence_cap, stars_for_confidence

    conf, note, stars = _apply_league_confidence_cap("ucl", 0.92, None, "5-star")
    assert conf == 0.5
    assert "冷启动" in note
    assert stars == "2-star"
    assert stars_for_confidence(conf) == stars


def test_cold_start_keeps_lower_confidence_untouched():
    from core.predictor import _apply_league_confidence_cap

    conf, note, stars = _apply_league_confidence_cap("ucl", 0.3, None, "1-star")
    assert conf == 0.3
    assert stars == "1-star"
    assert "冷启动" in note


def test_non_cold_start_league_unchanged():
    from core.predictor import _apply_league_confidence_cap

    conf, note, stars = _apply_league_confidence_cap("epl", 0.92, None, "5-star")
    assert (conf, note, stars) == (0.92, None, "5-star")


# ── 6. 队名链 ──────────────────────────────────────────────────────

def test_ucl_team_names_are_jc_official():
    """UCL 队的模型中文名 = 竞彩官方中文名（不是通用叫法）。"""
    from core.i18n import to_cn

    expect = {
        "Manchester City": "曼彻斯特城",
        "Manchester United": "曼彻斯特联",
        "Paris Saint-Germain": "巴黎圣日尔曼",
        "Tottenham Hotspur": "托特纳姆热刺",
        "Newcastle United": "纽卡斯尔联",
        "Fenerbahce": "费内巴切",
        "Slavia Prague": "布拉格斯拉维亚",
        "Sabah FK": "萨巴赫",
        "PSV Eindhoven": "PSV埃因霍温",
        "AEK Athens": "雅典AEK",
        "Bodo/Glimt": "博德闪耀",
    }
    for en, cn in expect.items():
        assert to_cn(en) == cn, f"{en} → {to_cn(en)}，期望 {cn}"


def test_official_names_file_is_valid_json_with_meta():
    p = REPO_ROOT / "scripts" / "references" / "team_cn_official.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    teams = {k: v for k, v in raw.items() if not k.startswith("_")}
    assert len(teams) >= 100, "队名表条目数异常（可能被清空）"
    assert all(isinstance(v, str) and v for v in teams.values())


# ── 7. web 侧白名单 ────────────────────────────────────────────────

def test_ucl_in_web_main_panel_whitelist():
    import web.config as config

    assert "欧洲冠军联赛" in config.MODEL_LEAGUES
    assert "欧洲冠军联赛" in config.MAIN_LEAGUES
    assert "欧洲冠军联赛" in config.big5_with_temp()


def test_daily_image_main_panel_includes_ucl():
    """欧冠有模型预测产出 ⇒ 必须进主面板（main_rows），不是"临时赛事"附面板。"""
    from web.app import jc as app_jc

    assert "欧洲冠军联赛" in app_jc.LEAGUE_KEYS
    assert app_jc.LEAGUE_KEYS["欧洲冠军联赛"] == "ucl"
