"""契约 v1.1 官方 payload → 规范行：jc_issue 期头与开奖（纯函数：零 DB、零网络、零副作用，只用 stdlib）。
parse_jc_issue 一行 = 一个玩法的一期期头 → [fact.jc_issue, *fact.jc_issue_match]；
parse_jc_issue_result 一行 = 一期开奖 → [fact.jc_issue_draw, *fact.jc_issue_prize]。
规格表写法 "官方键[:转换码]>规范列"，值换算器见 parse_values。
P0-COLLECT2：parent 在前、children 在后；matchList/prizeLevelList 都展开。
"""

from __future__ import annotations

from .parse_values import build, need, out

GAME_KEYS = ("sfc", "jqc", "bqc")  # jc_issue_result 三棵详情树；任九是 sfc 期里的孪生字段，不单独成期
ISSUE = ("lotteryGameNum>game_num lotteryDrawNum>issue_no lotteryGameName>game_name lotterySaleBeginTime:c>sale_begin "
         "lotterySaleEndTime:c>sale_end lotteryDrawTime:c>draw_at matchList:n>n_matches drawNumList>draw_num_list")
DRAW = ("lotteryGameNum>game_num lotteryDrawNum>issue_no lotteryGameName>game_name lotteryDrawResult>draw_result "
        "poolBalanceAfterdraw>pool_after totalSaleAmount>sales poolBalanceAfterdrawRj>pool_after_rj "
        "totalSaleAmountRj>sales_rj lotteryPaidBeginTime:c>paid_begin lotteryPaidEndTime:c>paid_end "
        "isDelay>is_delay delayRemark>delay_remark")
# 期头 matchList 单行规范列：seq/matchNum/gmMatchId/startTime/队名全/短
MATCH = ("matchNum:i>seq gmMatchId:i>gm_match_id startTime:c>start_date "
         "matchName>league_cn masterTeamAllName>home_cn guestTeamAllName>away_cn "
         "masterTeamName>home_short guestTeamName>away_short")


def parse_jc_issue(payload: dict) -> list[dict]:
    """parent + N match 指令（list[dict]，父在前子在后；matchList 为空 ⇒ 单 parent 指令，len 1）。"""
    for key, kind in (("lotteryGameNum", str), ("lotteryDrawNum", str), ("drawNumList", list), ("matchList", list)):
        need(payload, key, kind)
    matches = payload["matchList"]  # need() 已验类型
    if not matches or not all(isinstance(match, dict) for match in matches):
        raise ValueError(f"字段 matchList 必须是非空对象数组（fact.jc_issue.n_matches 必须 > 0）：{matches!r}")
    row = build(payload, ISSUE) | {"raw_head": payload}
    notes = [f"matchList {len(matches)} 场（长度不按常识断言，任九给 14 场）→ fact.jc_issue_match，P0-COLLECT2 落"]
    parent = out("fact.jc_issue", {"game_num": row["game_num"], "issue_no": row["issue_no"]}, row, notes)
    base_pk = {"game_num": row["game_num"], "issue_no": row["issue_no"]}
    children = []
    for m in matches:
        if not isinstance(m.get("matchNum"), int):
            raise ValueError(f"matchList 项缺 matchNum(int)：{m!r}")
        mrow = build(m, MATCH) | base_pk | {"raw": m, "is_drawn": False}
        # PK 三列：jc_issue_match 真实主键是 (game_num, issue_no, seq)，写入端的 on conflict 列
        children.append(out("fact.jc_issue_match",
                            {**base_pk, "seq": mrow["seq"]}, mrow))
    return [parent, *children]


def parse_jc_issue_result(payload: dict) -> list[dict]:
    """draw + N prize 指令；prizeLevelList 为空 ⇒ 单 draw 指令，len 1。"""
    key = need(payload, "gameKey", str)
    if key not in GAME_KEYS:
        raise ValueError(f"字段 gameKey 不在 {GAME_KEYS} 内：{key!r}")
    for field in ("lotteryGameNum", "lotteryDrawNum"):
        need(payload, field, str)
    matches = need(payload, "matchList", list)
    prizes = need(payload, "prizeLevelList", list)
    row = build(payload, DRAW) | {"game_key": key}
    notes = [f"matchList {len(matches)} 场 + prizeLevelList {len(prizes)} 条（条数不固定）→ P0-COLLECT2"]
    parent = out("fact.jc_issue_draw", {"game_num": row["game_num"], "issue_no": row["issue_no"]}, row, notes)
    base_pk = {"game_num": row["game_num"], "issue_no": row["issue_no"]}
    children = []
    for i, p in enumerate(prizes, 1):
        if not isinstance(p, dict):
            raise ValueError(f"prizeLevelList[{i-1}] 不是对象：{p!r}")
        # tier_no 用列表下标（1 起）；真包实测 prizeLevelList 在开奖前后会整体换形，is_current 由重刷置 false（v1.2 注释）
        prow = {
            **base_pk,
            "tier_no": i,
            "prize_level": str(p.get("prizeLevel") or ""),
            "stake_count": p.get("stakeCount"),
            "stake_amount": p.get("stakeAmount"),
            "stake_amount_format": p.get("stakeAmountFormat"),
            "total_prizeamount": p.get("totalPrizeamount"),
            "award_type": p.get("awardType"),
            "grp": p.get("group"),
            "sort_no": p.get("sort"),
            "is_rj": False,
            "raw": p,
            "is_current": True,
        }
        # PK 三列：jc_issue_prize 真实主键是 (game_num, issue_no, tier_no)
        children.append(out("fact.jc_issue_prize",
                            {**base_pk, "tier_no": i}, prow))
    return [parent, *children]
