"""B1-B7: parse_jclq_result 纯函数契约（零DB、零网络）。内嵌payload源自采集机2026-09-16T09:53:05Z真实探针响应。"""
from __future__ import annotations
import sys
from decimal import Decimal
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from store.parse_misc import parse_jclq_result  # noqa: E402

def _blk(gl, wo, co="", cd="", rs="-"):
    return {"combination": co, "combinationDesc": cd, "goalLine": gl,
            "resultStatus": rs, "single": 0, "winOdds": wo}

_HDC_OPEN  = _blk("-27.5", "")
_HILO_OPEN = _blk("170.5", "")
_MNL_OPEN  = _blk("", "", rs="未开售")
_WNM_OPEN  = _blk("", "", rs="未开售")

_OPEN = {  # status=1 开售中 leagueId=8
    "allAwayTeam": "沙特阿拉伯", "allHomeTeam": "中国", "awayTeam": "沙特", "awayTeamId": 321,
    "finalScore": "-", "hdc": _HDC_OPEN, "hilo": _HILO_OPEN,
    "homeTeam": "中国", "homeTeamId": 91, "leagueBackColor": "8E8698", "leagueId": 8,
    "leagueName": "亚运会男篮", "leagueNameAbbr": "亚运男篮", "matchDate": "2026-09-16",
    "matchId": 2041526, "matchNum": "3302", "matchNumStr": "周三302", "matchTime": "15:00:00",
    "mnl": _MNL_OPEN, "poolStatus": "", "status": 1, "wnm": _WNM_OPEN,
}

_FINISHED = {  # status=2 已完赛 leagueId=8
    "allAwayTeam": "巴林", "allHomeTeam": "伊朗", "awayTeam": "巴林", "awayTeamId": 170,
    "finalScore": "74-79",
    "hdc":  _blk("-3.5",  "1.65", co="H", cd="让分主胜", rs=""),
    "hilo": _blk("149.5", "1.70", co="H", cd="大",      rs=""),
    "homeTeam": "伊朗", "homeTeamId": 85, "leagueBackColor": "8E8698", "leagueId": 8,
    "leagueName": "亚运会男篮", "leagueNameAbbr": "亚运男篮", "matchDate": "2026-09-16",
    "matchId": 2041525, "matchNum": "3301", "matchNumStr": "周三301", "matchTime": "12:00:00",
    "mnl": _blk("", "1.37", co="H",  cd="主胜",    rs=""),
    "poolStatus": "", "status": 2,
    "wnm": _blk("", "4.90", co="+1", cd="主胜1-5", rs=""),
}

_DDL_COLS = {
    "match_id", "final_score", "ft_h", "ft_a", "status", "pool_status", "betting_single",
    "mnl_combination", "mnl_desc", "mnl_result_status", "mnl_odds",
    "hdc_line", "hdc_combination", "hdc_desc", "hdc_result_status", "hdc_odds",
    "hilo_line", "hilo_combination", "hilo_desc", "hilo_result_status", "hilo_odds",
    "wnm_combination", "wnm_desc", "wnm_result_status", "wnm_odds",
    "raw_blocks", "src_hash", "src_file", "first_seen_at", "last_seen_at",
}
_AUDIT = {"src_hash", "src_file", "first_seen_at", "last_seen_at"}

def test_open_sample_status1():
    """B1 开售样本：ft_h/ft_a None；hdc_line=-27.5；mnl_odds None（winOdds 空串）。"""
    r = parse_jclq_result(_OPEN)["row"]
    assert r["status"] == 1 and r["final_score"] == "-"
    assert r["ft_h"] is None and r["ft_a"] is None
    assert r["match_id"] == 2041526
    assert r["hdc_line"] == Decimal("-27.5") and r["hilo_line"] == Decimal("170.5")
    assert r["mnl_odds"] is None

def test_finished_sample_status2():
    """B2 完赛样本：ft_h==74, ft_a==79；hdc_odds==1.65；hilo_desc=='大'。"""
    r = parse_jclq_result(_FINISHED)["row"]
    assert r["status"] == 2 and r["final_score"] == "74-79"
    assert r["ft_h"] == 74 and r["ft_a"] == 79
    assert r["hdc_odds"] == Decimal("1.65") and r["hdc_line"] == Decimal("-3.5")
    assert r["hdc_combination"] == "H" and r["hilo_desc"] == "大"

def test_row_keys_are_ddl_subset():
    """B3 两份样本的 row 键 ⊆ fact.jbq_result DDL 列集（离线白名单校验）。"""
    for payload in (_OPEN, _FINISHED):
        unknown = set(parse_jclq_result(payload)["row"]) - _DDL_COLS
        assert not unknown, f"row 出现 DDL 列集外的键: {unknown}"

def test_missing_required_raises():
    """B4 删掉 matchId ⇒ ValueError 点名 matchId。"""
    with pytest.raises(ValueError, match="matchId"):
        parse_jclq_result({k: v for k, v in _OPEN.items() if k != "matchId"})

def test_bad_finalscore_raises_or_none():
    """B5 status=2 但 finalScore='abc' ⇒ raise；status=1 finalScore='-' ⇒ ft_h/ft_a None。"""
    with pytest.raises(ValueError):
        parse_jclq_result(_FINISHED | {"finalScore": "abc"})
    r = parse_jclq_result(_OPEN)["row"]
    assert r["ft_h"] is None and r["ft_a"] is None

def test_no_league_filtering():
    """B6 leagueId 26/7/8 均正常返回，解析层不做联赛闸门。"""
    for lid in (26, 7, 8):
        result = parse_jclq_result(_OPEN | {"leagueId": lid})
        assert result is not None and result["row"]["match_id"] == 2041526

def test_raw_blocks_holds_rest():
    """B7 raw_blocks 含 leagueId/leagueName/matchNumStr；四个审计键不在 row 里。"""
    r = parse_jclq_result(_OPEN)["row"]
    rb = r["raw_blocks"]
    assert rb["leagueId"] == 8 and rb["leagueName"] == "亚运会男篮" and rb["matchNumStr"] == "周三302"
    assert not (_AUDIT & set(r)), f"审计键不许在 row 里: {_AUDIT & set(r)}"
