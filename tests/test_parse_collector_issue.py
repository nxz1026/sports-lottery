"""契约 v1.1 解析层逐条对着夹具断言（零 DB；夹具 = tests/fixtures/collector_v11/**，真实探针响应，一个字节都不许改）：
⑤ jc_issue n_matches == len(matchList)、90 与 900129 期号字符串相等；⑥ jc_issue_result
game_key ∈ {sfc,jqc,bqc}、jqc "3＋,1" 全角、bqc "3,3"、sfc 带 *Rj 孪生键（任九不单独成期）；⑦ lottery_draw 号码串
原样含空格、stakeAmount "---" 保持 str；⑧ kind="error" 不产 fact 行；⑨ 负向：缺身份键点名 raise、坏比分不 raise。
P0-COLLECT1b 机械拆分自 tests/test_parse_collector.py（①~④ 在 tests/test_parse_collector_offer.py）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from store import parse_collector as pc  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "collector_v11"


def rows(name: str) -> list[dict]:
    """夹具原样读入（payload 与官方响应逐字一致，见 PROVENANCE.md）。"""
    text = (FIX / f"{name}.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def payloads(name: str) -> list[dict]:
    return [env["payload"] for env in rows(name)]


def test_jc_issue_head_and_issue_no_collision():
    """⑤ 四期期头 n_matches == len(matchList)（任九给 14 场）；真包在售期号 26127/26181/26188，
    90 与 900129 期号跨玩法重号 ⇒ pk 必须带 game_num（换值不换语义）。"""
    envs = rows("jc_issue")
    parsed = [pc.parse_line(env) for env in envs]
    assert [(row["row"]["game_num"], row["row"]["issue_no"], row["row"]["n_matches"]) for row in parsed] == [
        ("90", "26127", 14), ("900129", "26127", 14), ("98", "26181", 6), ("94", "26188", 4)]
    for row, env in zip(parsed, envs):
        assert row["row"]["n_matches"] == len(env["payload"]["matchList"])
    assert parsed[0]["pk"]["issue_no"] == parsed[1]["pk"]["issue_no"]  # 期号跨玩法重号 ⇒ pk 必须带 game_num
    assert parsed[0]["pk"]["game_num"] != parsed[1]["pk"]["game_num"]


def test_jc_issue_result_game_keys_and_rj_twins():
    """⑥ 三行 game_key 各不同；jqc 判奖串全角 "3＋,1"、bqc "3,3"；sfc 的 *Rj 孪生字段原样落（任九不单独成期）。"""
    envs = rows("jc_issue_result")
    by_key = {env["payload"]["gameKey"]: env["payload"] for env in envs}
    parsed = {pc.parse_line(env)["row"]["game_key"]: pc.parse_line(env)["row"] for env in envs}
    assert set(parsed) == set(pc.GAME_KEYS) == {"sfc", "jqc", "bqc"}
    assert by_key["jqc"]["matchList"][0]["result"] == "3＋,1"  # 全角加号，不许被转成 "3+,1"
    assert by_key["bqc"]["matchList"][0]["result"] == "3,3"
    assert parsed["sfc"]["sales_rj"] == by_key["sfc"]["totalSaleAmountRj"]
    tiers = [tier["prizeLevel"] for tier in by_key["sfc"]["prizeLevelList"]]
    assert "任选9场" in tiers  # 任九混在 sfc 期里 ⇒ 不单独成期
    assert parsed["jqc"]["game_key"] == "jqc" and parsed["bqc"]["game_num"] == "98"


def test_lottery_draw_raw_numbers_and_dash_amount():
    """⑦ 22 行 = 4 玩法各 1 期 + 6 行跨批变化 + 12 行旧探针；号码串原样含空格（不拆不排不去重）；
    stakeAmount "---" 必须仍是 str，不许变 None/0。"""
    parsed = [pc.parse_line(env) for env in rows("lottery_draw")]
    assert len(parsed) == 22
    first = parsed[0]["row"]
    assert first["numbers_raw"] == "10 14 30 33 34 09 12" and first["numbers"] == {
        "front": ["10", "14", "30", "33", "34"], "back": ["09", "12"]}
    assert parsed[3]["row"]["numbers"] == {"digits": ["2", "8", "3", "4", "5", "6", "4"]}  # 7星彩原序
    dash = [tier for row in parsed for tier in row["row"]["prizes"] if tier["stakeAmount"] == "---"]
    assert all(tier["stakeAmountFormat"] == "-1" for tier in dash) and len(dash) == 4
    assert all(isinstance(tier["stakeAmount"], str) for row in parsed for tier in row["row"]["prizes"])


def test_error_lines_produce_no_fact_row():
    """⑧ kind="error"（真实 P0001）在解析层不产 fact 行：错误行只留在 stg.<topic>.line。"""
    errors = rows("errors")
    assert [env["payload"]["errorCode"] for env in errors] == ["P0001", "P0001"]
    assert [pc.parse_line(env) for env in errors] == [None, None]


def test_negative_shapes_raise_and_keep_raw_values():
    """⑨ 抹掉 matchId ⇒ ValueError 点名 matchId；sectionsNo999 改成 "3:3:3" ⇒ 解析列 None、原值照抄。
    锚点行 1:4（真包周二003 卡塔尔亚vs韩国亚：半场 0:3、SP 18.00/7.20/1.07、winFlag A、poolStatus Payout）。
    jclq_result 已接通真解析（P0-COLLECT2lqP §5.6）：parse_line 正常返回 fact.jbq_result 行。"""
    good = next(p for p in payloads("jczq_result") if p["sectionsNo999"] == "1:4")
    with pytest.raises(ValueError, match="matchId"):
        pc.parse_jczq_result({key: value for key, value in good.items() if key != "matchId"})
    weird = pc.parse_jczq_result(good | {"sectionsNo999": "3:3:3"})["row"]
    assert (weird["ft_h"], weird["ft_a"], weird["sections_no_999"]) == (None, None, "3:3:3")
    with pytest.raises(ValueError, match="lotteryGameNum"):
        pc.parse_lottery_draw({key: value for key, value in payloads("lottery_draw")[0].items()
                               if key != "lotteryGameNum"})
    with pytest.raises(ValueError, match="契约"):
        pc.parse_jclq_offer(payloads("jclq_result")[0])  # §5.5 未冻结：不猜字段名
    # §5.6 已冻结且落点表已建：jclq_result 现在真解析，parse_line 正常返回 dict
    result = pc.parse_line({"kind": "line", "topic": "jclq_result",
                            "payload": payloads("jclq_result")[0]})
    assert result is not None and result["table"] == "fact.jbq_result"
    assert "match_id" in result["pk"]
