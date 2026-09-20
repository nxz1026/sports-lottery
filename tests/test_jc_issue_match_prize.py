"""P0-COLLECT2 续：parse_jc_issue / parse_jc_issue_result 展开 matchList → fact.jc_issue_match、
prizeLevelList → fact.jc_issue_prize。纯函数 + 假游标两层测，DB 由 conn fixture 隔离。"""
from __future__ import annotations

import sys, uuid
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from ingest import jc_issue_write  # noqa: E402
from store import pg  # noqa: E402
from store.parse_collector import parse_line, parse_lines_all  # noqa: E402

# ---------- 解析器纯函数：parent + children 展开 ----------

ISSUE_PAYLOAD = {
    "lotteryGameNum": "90", "lotteryDrawNum": "26T01", "lotteryGameName": "胜负游戏",
    "lotterySaleBeginTime": "2026-09-13 20:00", "lotterySaleEndTime": "2026-09-16 17:30",
    "lotteryDrawTime": "2026-09-17 14:00",
    "drawNumList": ["26T01"],
    "matchList": [
        {"matchNum": 1, "matchName": "亚运男足", "startTime": "2026-09-16",
         "masterTeamName": "中国亚", "guestTeamName": "朝鲜亚",
         "masterTeamAllName": "中国亚运男足", "guestTeamAllName": "朝鲜亚运男足",
         "gmMatchId": 2041494, "leagueId": 83},
        {"matchNum": 2, "matchName": "亚运男足", "startTime": "2026-09-16",
         "masterTeamName": "日本亚", "guestTeamName": "韩国亚",
         "masterTeamAllName": "日本亚运男足", "guestTeamAllName": "韩国亚运男足",
         "gmMatchId": 2041495, "leagueId": 83},
    ],
}

DRAW_PAYLOAD = {
    "gameKey": "sfc", "lotteryGameNum": "90", "lotteryDrawNum": "26T02", "lotteryGameName": "胜负游戏",
    "lotteryDrawResult": "1-0", "poolBalanceAfterdraw": "100.00", "totalSaleAmount": "200.00",
    "poolBalanceAfterdrawRj": "", "totalSaleAmountRj": "",
    "lotteryPaidBeginTime": "2026-09-19 03:00", "lotteryPaidEndTime": "2026-09-19 12:00",
    "isDelay": 0, "delayRemark": "",
    "matchList": [],
    "prizeLevelList": [
        {"awardType": 0, "group": "10", "prizeLevel": "一等奖", "sort": 10,
         "stakeAmount": "19,062", "stakeAmountFormat": "19062", "stakeCount": "462",
         "totalPrizeamount": "8,806,644"},
        {"awardType": 0, "group": "20", "prizeLevel": "二等奖", "sort": 20,
         "stakeAmount": "304", "stakeAmountFormat": "304", "stakeCount": "12,387",
         "totalPrizeamount": "3,765,648"},
    ],
}


def test_parse_jc_issue_returns_parent_plus_match_children():
    from store.parse_jcissue import parse_jc_issue
    ins_list = parse_jc_issue(ISSUE_PAYLOAD)
    assert len(ins_list) == 3  # parent + 2 matches
    assert ins_list[0]["table"] == "fact.jc_issue"
    assert ins_list[0]["pk"] == {"game_num": "90", "issue_no": "26T01"}
    for i, m in enumerate(ins_list[1:], 1):
        assert m["table"] == "fact.jc_issue_match"
        assert m["pk"] == {"game_num": "90", "issue_no": "26T01", "seq": i}
        assert m["row"]["seq"] == i
        assert m["row"]["gm_match_id"] in (2041494, 2041495)
        assert m["row"]["is_drawn"] is False
        assert m["row"]["start_date"] == date(2026, 9, 16)


def test_parse_jc_issue_match_pk_includes_seq():
    """jc_issue_match 真实 PK 是 (game_num, issue_no, seq)，parser 必须把 seq 带进 pk 以让 on conflict 工作。"""
    from store.parse_jcissue import parse_jc_issue
    ins_list = parse_jc_issue(ISSUE_PAYLOAD)
    for m in ins_list[1:]:
        assert set(m["pk"]) == {"game_num", "issue_no", "seq"}


def test_parse_jc_issue_result_returns_parent_plus_prize_children():
    from store.parse_jcissue import parse_jc_issue_result
    ins_list = parse_jc_issue_result(DRAW_PAYLOAD)
    assert len(ins_list) == 3  # draw + 2 prizes
    assert ins_list[0]["table"] == "fact.jc_issue_draw"
    for i, p in enumerate(ins_list[1:], 1):
        assert p["table"] == "fact.jc_issue_prize"
        assert p["pk"] == {"game_num": "90", "issue_no": "26T02", "tier_no": i}
        assert p["row"]["tier_no"] == i
        assert p["row"]["prize_level"] in ("一等奖", "二等奖")
        assert p["row"]["is_rj"] is False
        assert p["row"]["is_current"] is True


def test_parse_lines_all_flattens_for_jc_issue():
    from store.parse_collector import parse_lines_all
    env = {"kind": "line", "topic": "jc_issue", "src_hash": "h1", "payload": ISSUE_PAYLOAD}
    all_ins = parse_lines_all(env)
    assert len(all_ins) == 3
    # parse_line 单调用仍只返 parent（向下兼容）
    only_parent = parse_line(env)
    assert only_parent["table"] == "fact.jc_issue"


def test_parse_lines_all_returns_single_for_other_topics():
    """jclq_result 等仍单指令；parse_lines_all 应给出 [单] 列表。"""
    env = {"kind": "line", "topic": "jclq_result", "src_hash": "h1",
           "payload": {"matchId": 1, "status": 2, "finalScore": "1-0",
                       "mnl": {"single": 1}, "hdc": {"single": 1},
                       "hilo": {"single": 1}, "wnm": {"single": 1}}}
    all_ins = parse_lines_all(env)
    assert len(all_ins) == 1
    assert all_ins[0]["table"] == "fact.jbq_result"


def test_parse_lines_all_error_kind_returns_none_marker():
    env = {"kind": "error", "topic": "jc_issue", "payload": {}}
    assert parse_lines_all(env) == [None]


def test_parse_jc_issue_empty_matchlist_raises():
    from store.parse_jcissue import parse_jc_issue
    bad = {**ISSUE_PAYLOAD, "matchList": []}
    with pytest.raises(ValueError, match="matchList"):
        parse_jc_issue(bad)


def test_parse_jc_issue_match_missing_matchnum_raises():
    from store.parse_jcissue import parse_jc_issue
    bad = {**ISSUE_PAYLOAD,
           "matchList": [ISSUE_PAYLOAD["matchList"][0] | {"matchNum": "1"}]}  # str not int
    with pytest.raises(ValueError, match="matchNum"):
        parse_jc_issue(bad)


# ---------- 写手 + 假游标：upsert SQL 形态 + JSONB 包裹 ----------

class FakeCur:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.calls = []
    def execute(self, q, p):
        self.calls.append((q, p))


def _sql(q):
    return q.as_string(None) if hasattr(q, "as_string") else str(q)


def test_upsert_issue_match_sql_shape():
    """jc_issue_match on conflict 三列 PK + raw 包 Json。"""
    ins = {"table": "fact.jc_issue_match",
           "pk": {"game_num": "90", "issue_no": "26T01", "seq": 1},
           "row": {"game_num": "90", "issue_no": "26T01", "seq": 1,
                   "gm_match_id": 2041494, "league_cn": "亚运男足",
                   "home_cn": "中国亚运男足", "away_cn": "朝鲜亚运男足",
                   "home_short": "中国亚", "away_short": "朝鲜亚",
                   "start_date": date(2026, 9, 16),
                   "raw": {"x": 1}, "is_drawn": False},
           "notes": []}
    cur = FakeCur()
    n = jc_issue_write.upsert_issue_instruction(cur, ins, "h1", "f.jsonl")
    assert n == 1
    txt = _sql(cur.calls[0][0])
    assert "insert into" in txt and "on conflict" in txt
    assert '"fact"."jc_issue_match"' in txt
    assert "do update set" in txt
    assert "last_seen_at = now()" in txt
    # on conflict 三列
    assert "game_num" in txt and "issue_no" in txt and "seq" in txt
    # Json 包裹：raw 列是 Json
    from psycopg.types.json import Json
    json_wrapped = [v for v in cur.calls[0][1] if isinstance(v, Json)]
    assert len(json_wrapped) == 1 and json_wrapped[0].obj == {"x": 1}


def test_upsert_issue_prize_sql_shape():
    """jc_issue_prize 是 append-only（无 last_seen_at）⇒ 走纯 INSERT。"""
    ins = {"table": "fact.jc_issue_prize",
           "pk": {"game_num": "90", "issue_no": "26T02", "tier_no": 1},
           "row": {"game_num": "90", "issue_no": "26T02", "tier_no": 1,
                   "prize_level": "一等奖", "stake_count": "462",
                   "stake_amount": "19,062", "stake_amount_format": "19062",
                   "total_prizeamount": "8,806,644",
                   "award_type": 0, "grp": "10", "sort_no": 10,
                   "is_rj": False, "raw": {"x": 2}, "is_current": True},
           "notes": []}
    cur = FakeCur()
    n = jc_issue_write.upsert_issue_instruction(cur, ins, "h1", "f.jsonl")
    assert n == 1
    txt = _sql(cur.calls[0][0])
    assert '"fact"."jc_issue_prize"' in txt
    assert "tier_no" in txt
    # 不应是 UPSERT
    assert "on conflict" not in txt
    assert "last_seen_at" not in txt


def test_upsert_issue_match_pk_missing_seq_raises():
    ins = {"table": "fact.jc_issue_match",
           "pk": {"game_num": "90", "issue_no": "26T01"},  # 缺 seq
           "row": {"game_num": "90", "issue_no": "26T01"},
           "notes": []}
    cur = FakeCur()
    with pytest.raises(ValueError, match="主键缺列"):
        jc_issue_write.upsert_issue_instruction(cur, ins, "h1", "f.jsonl")


# ---------- 真 DB 端到端：解析 + 写手 → 行数符合预期 ----------

PK_ISSUE = {"game_num": "90", "issue_no": "T" + uuid.uuid4().hex[:6] + "M1"}
PK_DRAW = {"game_num": "90", "issue_no": "T" + uuid.uuid4().hex[:6] + "M2"}


@pytest.fixture()
def conn():
    try:
        c = pg.connect("ing")
    except Exception as exc:
        pytest.skip(f"no db: {type(exc).__name__}: {exc}")
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def test_endtoend_jc_issue_match_writes(conn):
    """一整期 jc_issue ⇒ fact.jc_issue + 2 行 fact.jc_issue_match；replay 幂等。"""
    from store.parse_jcissue import parse_jc_issue
    issue_payload = {**ISSUE_PAYLOAD, "lotteryDrawNum": PK_ISSUE["issue_no"]}
    ins_list = parse_jc_issue(issue_payload)
    cur = conn.cursor()
    ups_n = 0
    for ins in ins_list:
        ups_n += jc_issue_write.upsert_issue_instruction(cur, ins, "h1", "f.jsonl")
    conn.commit()
    assert ups_n == 3
    n_parent = cur.execute("select count(*) from fact.jc_issue where game_num=%s and issue_no=%s",
                           [PK_ISSUE["game_num"], PK_ISSUE["issue_no"]]).fetchone()[0]
    n_match = cur.execute("select count(*) from fact.jc_issue_match where game_num=%s and issue_no=%s",
                          [PK_ISSUE["game_num"], PK_ISSUE["issue_no"]]).fetchone()[0]
    assert n_parent == 1
    assert n_match == 2
    # gm_match_id 落库
    gm_ids = [r[0] for r in cur.execute(
        "select gm_match_id from fact.jc_issue_match where game_num=%s and issue_no=%s order by seq",
        [PK_ISSUE["game_num"], PK_ISSUE["issue_no"]]).fetchall()]
    assert gm_ids == [2041494, 2041495]
    # replay 幂等
    for ins in ins_list:
        jc_issue_write.upsert_issue_instruction(cur, ins, "h1", "f.jsonl")
    conn.commit()
    n_match2 = cur.execute("select count(*) from fact.jc_issue_match where game_num=%s and issue_no=%s",
                           [PK_ISSUE["game_num"], PK_ISSUE["issue_no"]]).fetchone()[0]
    assert n_match2 == 2


def test_endtoend_jc_issue_prize_writes(conn):
    """一整期 jc_issue_result ⇒ fact.jc_issue_draw + 2 行 fact.jc_issue_prize。
    FK ⇒ 先把 parent fact.jc_issue 行占上（同 game_num/issue_no），再落 draw+prize。"""
    from store.parse_jcissue import parse_jc_issue, parse_jc_issue_result
    cur = conn.cursor()
    # 先落 parent jc_issue 占位（同 issue_no）
    parent_ins = parse_jc_issue(ISSUE_PAYLOAD | {"lotteryDrawNum": PK_DRAW["issue_no"]})[0]
    jc_issue_write.upsert_issue_instruction(cur, parent_ins, "h1", "f.jsonl")
    draw_payload = {**DRAW_PAYLOAD, "lotteryDrawNum": PK_DRAW["issue_no"]}
    ins_list = parse_jc_issue_result(draw_payload)
    for ins in ins_list:
        jc_issue_write.upsert_issue_instruction(cur, ins, "h1", "f.jsonl")
    conn.commit()
    n_draw = cur.execute("select count(*) from fact.jc_issue_draw where game_num=%s and issue_no=%s",
                         [PK_DRAW["game_num"], PK_DRAW["issue_no"]]).fetchone()[0]
    n_prize = cur.execute("select count(*) from fact.jc_issue_prize where game_num=%s and issue_no=%s",
                          [PK_DRAW["game_num"], PK_DRAW["issue_no"]]).fetchone()[0]
    assert n_draw == 1
    assert n_prize == 2
    levels = [r[0] for r in cur.execute(
        "select prize_level from fact.jc_issue_prize where game_num=%s and issue_no=%s order by tier_no",
        [PK_DRAW["game_num"], PK_DRAW["issue_no"]]).fetchall()]
    assert levels == ["一等奖", "二等奖"]
