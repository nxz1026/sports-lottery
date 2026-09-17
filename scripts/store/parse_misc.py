"""契约 v1.1 官方 payload → 规范行：lottery_draw 开奖与 jclq 篮彩（纯函数：零 DB、零网络、零副作用，只用 stdlib）。
parse_lottery_draw 一行 = 一个彩种的一期 → fact.lottery_draw（号码串原样；解析结果另存新列 numbers，不重排不去重）。
jclq 两 topic 裁决：§5.5 未冻结 ⇒ parse_jclq_offer raise；§5.6 已冻结且落点表已建 ⇒ parse_jclq_result 真解析。
"""

from __future__ import annotations

from .parse_values import build, dec, need, out, split_numbers

LOTTERY = ("lotteryGameNum>game_num lotteryDrawNum>issue_no lotteryGameName>game_name lotteryDrawTime:c>draw_date "
           "lotteryDrawStatus>status lotteryDrawResult>numbers_raw drawFlowFund>pool "
           "lotteryEquipmentCount>equipment_count")

_RAW_BLOCK_KEYS = (
    "leagueId", "leagueName", "leagueNameAbbr", "leagueBackColor", "matchNum", "matchNumStr",
    "matchDate", "matchTime", "allHomeTeam", "allAwayTeam", "homeTeam", "awayTeam", "homeTeamId", "awayTeamId",
)

def _parse_ft(status: int, final_score: str) -> tuple[int | None, int | None]:
    """status=2 且比分合法 → (ft_h, ft_a)；否则 → (None, None)；非法比分 raise。"""
    if status != 2 or final_score in ("-", ""):
        return None, None
    parts = final_score.split("-")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"status=2 但 finalScore 无法解析为主-客整数：{final_score!r}")
    return int(parts[0]), int(parts[1])

def _odds(block: dict) -> object:
    """winOdds 空串/非数字 → None，数字串 → Decimal（dec 自带空串→None）。"""
    return dec(block.get("winOdds"))

def _line(block: dict) -> object:
    """goalLine 空串/'-'/非数字 → None，数字串 → Decimal。"""
    return dec(block.get("goalLine"))


def parse_lottery_draw(payload: dict) -> dict:
    """一行 = 一个彩种的一期 → fact.lottery_draw（号码串原样；解析结果另存新列 numbers，不重排不去重）。"""
    for key, kind in (("lotteryGameNum", str), ("lotteryDrawNum", str), ("lotteryDrawResult", str),
                      ("prizeLevelList", list)):
        need(payload, key, kind)
    row = build(payload, LOTTERY) | {"numbers": None, "prizes": payload["prizeLevelList"]}
    row["numbers"] = split_numbers(row["game_num"], row["numbers_raw"])
    notes = ["prizes 整数组原样（含 stakeAmount '---' 与千分位逗号），解析层不转数字"]
    return out("fact.lottery_draw", {"game_num": row["game_num"], "issue_no": row["issue_no"]}, row, notes)


def parse_jclq_offer(payload: dict) -> dict:
    """§5.5：jclq_offer 契约 v1.1 **不冻结**（探针窗口内篮彩无开售）⇒ 不解析、不推测字段名。"""
    raise ValueError("jclq_offer 契约 v1.1 未冻结（§5.5）：等开售窗口复探后升 v1.2 再解析")


def parse_jclq_result(payload: dict) -> dict:
    """§5.6 已冻结 jclq_result → fact.jbq_result（篮彩盘口+战果；不按联赛过滤，闸门在下一单）。
    缺 matchId/status 或四大盘口块（mnl/hdc/hilo/wnm）非 dict ⇒ 点名 ValueError，不让 KeyError 穿透。"""
    need(payload, "matchId", int)
    status = payload.get("status")
    if not isinstance(status, int):
        raise ValueError(f"字段 status 缺失或类型不对：{status!r}，期望 int")
    final_score = payload.get("finalScore", "-") or "-"
    ft_h, ft_a = _parse_ft(status, final_score)
    blocks = {k: need(payload, k, dict) for k in ("mnl", "hdc", "hilo", "wnm")}
    mnl, hdc, hilo, wnm = blocks["mnl"], blocks["hdc"], blocks["hilo"], blocks["wnm"]
    singles = {k: blk.get("single") for k, blk in blocks.items()}
    notes: list[str] = []
    if len(set(singles.values())) > 1:
        notes.append(
            f"betting_single 四块不一致: mnl={singles['mnl']} hdc={singles['hdc']}"
            f" hilo={singles['hilo']} wnm={singles['wnm']}"
        )
    row = {
        "match_id":           payload["matchId"],
        "final_score":        final_score,
        "ft_h":               ft_h,
        "ft_a":               ft_a,
        "status":             status,
        "pool_status":        payload.get("poolStatus"),
        "betting_single":     singles["mnl"],
        "mnl_combination":    mnl.get("combination"),
        "mnl_desc":           mnl.get("combinationDesc"),
        "mnl_result_status":  mnl.get("resultStatus"),
        "mnl_odds":           _odds(mnl),
        "hdc_line":           _line(hdc),
        "hdc_combination":    hdc.get("combination"),
        "hdc_desc":           hdc.get("combinationDesc"),
        "hdc_result_status":  hdc.get("resultStatus"),
        "hdc_odds":           _odds(hdc),
        "hilo_line":          _line(hilo),
        "hilo_combination":   hilo.get("combination"),
        "hilo_desc":          hilo.get("combinationDesc"),
        "hilo_result_status": hilo.get("resultStatus"),
        "hilo_odds":          _odds(hilo),
        "wnm_combination":    wnm.get("combination"),
        "wnm_desc":           wnm.get("combinationDesc"),
        "wnm_result_status":  wnm.get("resultStatus"),
        "wnm_odds":           _odds(wnm),
        "raw_blocks":         {k: payload.get(k) for k in _RAW_BLOCK_KEYS},
    }
    return out("fact.jbq_result", {"match_id": row["match_id"]}, row, notes)
