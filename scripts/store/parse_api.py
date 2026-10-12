"""AF/FD 原始响应 → 统一中间行（纯函数：零 DB、零网络、只用 stdlib）。

字段路径全部来自 2026-09-15 回填落块的真报文（核对输出见 .omp-logs/P0-FACT1.report.txt §1），不照文档猜：
AF 节点 = fixture{id,date,venue,status{short,elapsed}} + league{id,season,round} + teams{home{id,name},away{…}}
+ score{halftime,fulltime}，数组键 response；FD 节点 = id/utcDate/matchday/stage/status/season{startDate}
+ competition{code} + homeTeam/awayTeam + score{duration,fullTime,halfTime}，数组键 **matches**（驼峰 halfTime）。

P2-22 迁移：``AF_LEAGUES`` / ``FD_LEAGUES`` 单一真源改为 ``scripts/core/leagues.LEAGUE_CONFIG``
（``api_football_id`` / ``league_id`` 字段）。新增联赛只需改 LEAGUE_CONFIG，
本模块自动跟随；NBA 等无 api_football_id / league_id 的项自动跳过。
"""

from __future__ import annotations

from typing import Any

from core.leagues import LEAGUE_CONFIG

ROW_KEYS = ("fixture_id", "league_key", "season", "round", "kickoff_at", "af_team_id", "fd_team_id",
            "home_name", "away_name", "status", "venue", "ft_h", "ft_a", "ht_h", "ht_a", "elapsed_ht", "raw_node")
AF_STATUS = {"NS": "scheduled", "TBD": "unknown", "1H": "pending", "HT": "pending", "2H": "pending", "ET": "pending",
             "BT": "pending", "P": "pending", "LIVE": "pending", "SUSP": "interrupted", "INT": "interrupted",
             "FT": "ft", "AET": "aet", "PEN": "pen", "PST": "postponed", "CANC": "canceled", "ABD": "abandoned",
             "AWD": "defaulted", "WO": "defaulted"}
FD_STATUS = {"SCHEDULED": "scheduled", "TIMED": "timed", "IN_PLAY": "pending", "PAUSED": "pending",
             "FINISHED": "ft", "SUSPENDED": "interrupted", "POSTPONED": "postponed", "CANCELLED": "canceled",
             "AWARDED": "defaulted", "EXTRA_TIME": "aet", "PENALTY_SHOOTOUT": "pen"}
FD_DURATION = {"EXTRA_TIME": "aet", "PENALTY_SHOOTOUT": "pen"}  # FD 完赛只有 FINISHED，加时/点球看 duration

# 派生自 LEAGUE_CONFIG（api_football_id / league_id 字段）；NBA 等无字段的项自动跳过。
# 历史值（向后兼容 ABI）：
#   AF_LEAGUES = {39: "epl", 140: "laliga", 78: "bundesliga", 135: "seriea", 61: "ligue1"}
#   FD_LEAGUES = {"PL": "epl", "PD": "laliga", "BL1": "bundesliga", "SA": "seriea", "FL1": "ligue1"}
AF_LEAGUES: dict[int, str] = {
    v["api_football_id"]: k
    for k, v in LEAGUE_CONFIG.items()
    if isinstance(v.get("api_football_id"), int)
}
FD_LEAGUES: dict[str, str] = {
    v["league_id"]: k
    for k, v in LEAGUE_CONFIG.items()
    if isinstance(v.get("league_id"), str)
}


def _row(**kw: Any) -> dict[str, Any]:
    """钉死统一键集：两条解析路径缺的键一律 None，不让 AF/FD 行键集漂移。"""
    return {key: kw.get(key) for key in ROW_KEYS}


def _num(value: Any) -> int | None:  # 只认数字：None/""/缺键一律 None，绝不补 0
    return int(value) if isinstance(value, (int, float)) else None


def _pair(block: dict, key: str) -> tuple[int | None, int | None]:
    """取 score.<key>{home,away}；整块缺失或值为 null → (None, None)，绝不补 0。"""
    got = block.get(key) or {}
    return _num(got.get("home")), _num(got.get("away"))


def af_fixtures(body: dict, leagues: dict | None = None) -> list[dict]:
    """AF /fixtures 响应 → 统一行；leagues = {league_id: league_key}，缺省用内置五联赛表。"""
    known = AF_LEAGUES if leagues is None else leagues
    nodes: list = (body or {}).get("response") or []
    return [row for row in (_af_row(node, known) for node in nodes) if row]


def _af_row(node: dict, leagues: dict) -> dict | None:
    fixture = node.get("fixture") or {}
    league = node.get("league") or {}
    teams = node.get("teams") or {}
    if fixture.get("id") is None or not fixture.get("date"):
        return None  # 没有身份（id / 开球时间）的行宁可丢，也不猜
    score = node.get("score") or {}
    ft_h, ft_a = _pair(score, "fulltime")
    ht_h, ht_a = _pair(score, "halftime")
    status = fixture.get("status") or {}
    home, away = teams.get("home") or {}, teams.get("away") or {}
    # elapsed 只有快照拍在半场（short=HT）时才是"半场已用时长"，其余状态一律 NULL
    elapsed = _num(status.get("elapsed")) if status.get("short") == "HT" else None
    return _row(fixture_id=fixture["id"], league_key=leagues.get(league.get("id")),
                season=_num(league.get("season")), round=league.get("round"), kickoff_at=fixture["date"],
                af_team_id=(home.get("id"), away.get("id")), home_name=home.get("name"), away_name=away.get("name"),
                status=AF_STATUS.get(status.get("short"), "unknown"), venue=(fixture.get("venue") or {}).get("name"),
                ft_h=ft_h, ft_a=ft_a, ht_h=ht_h, ht_a=ht_a, elapsed_ht=elapsed, raw_node=node)


def fd_fixtures(body: dict, leagues: dict | None = None) -> list[dict]:
    """FD /matches 响应 → 统一行（数组键是 matches 不是 response；403 块由调用方按 http_status 排除）。"""
    known = FD_LEAGUES if leagues is None else leagues
    block = body or {}
    season_default = _num((block.get("filters") or {}).get("season"))
    nodes: list = block.get("matches") or []
    return [row for row in (_fd_row(node, known, season_default) for node in nodes) if row]


def _fd_row(node: dict, leagues: dict, season_default: int | None) -> dict | None:
    if node.get("id") is None or not node.get("utcDate"):
        return None
    score = node.get("score") or {}
    ft_h, ft_a = _pair(score, "fullTime")
    ht_h, ht_a = _pair(score, "halfTime")
    status = FD_STATUS.get(node.get("status"), "unknown")
    if status == "ft":
        status = FD_DURATION.get(score.get("duration"), "ft")
    home, away = node.get("homeTeam") or {}, node.get("awayTeam") or {}
    matchday = node.get("matchday")
    # 赛季口径 = 起始年（fact.fixture.season 注释：2024-25 季 = 2024）→ FD season.startDate 前四位
    season = _num(str((node.get("season") or {}).get("startDate") or "")[:4]) or season_default
    return _row(fixture_id=node["id"], league_key=leagues.get((node.get("competition") or {}).get("code")),
                season=season, round=f"Matchday {matchday}" if isinstance(matchday, int) else node.get("stage"),
                kickoff_at=node["utcDate"], fd_team_id=(home.get("id"), away.get("id")),
                home_name=home.get("name"), away_name=away.get("name"), status=status, venue=None,
                ft_h=ft_h, ft_a=ft_a, ht_h=ht_h, ht_a=ht_a, elapsed_ht=None, raw_node=node)
