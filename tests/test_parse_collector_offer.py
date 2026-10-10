"""契约 v1.1/v1.2 解析层逐条对着夹具断言（零 DB；夹具 = tests/fixtures/collector_v11/**，v1.2 真包抽样，
一个字节都不许改）：① 142 行外壳校验全过、src_hash 复算 0 失败（外壳两态 9 键/8 键）；② jczq_offer 91 行 = 17 场、
crs 31 选项键（28+3）与块内 66 键；③ 真包 HHAD 让球线五档分布、非 HHAD 块空线串 ⇒ 解析列 None（不是 0）；
④ jczq_result 15 行：取消/未开按官方三字段组合判 2 行 ⇒ ft_* 为 None 且不 raise、空 SP 2 行。
P0-COLLECT1b 机械拆分：⑤~⑨ 移 tests/test_parse_collector_issue.py。"""

from __future__ import annotations

import json
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from store import parse_collector as pc  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "collector_v11"
SNAP = "2026-09-15T14:35:37Z"  # 真包批 1 snap_ts（批内全行同值，见 PROVENANCE.md）


def rows(name: str) -> list[dict]:
    """夹具原样读入（payload 与官方响应逐字一致，见 PROVENANCE.md）。"""
    text = (FIX / f"{name}.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def payloads(name: str) -> list[dict]:
    return [env["payload"] for env in rows(name)]


def test_all_lines_pass_envelope_and_src_hash():
    """① 142 行（v1.2 真包抽样，见 PROVENANCE.md）：外壳八字段/类型/snap_ts(Z)/src_hash 复算全过；
    fetched_at 是 v1.2 新增可选键（旧探针 8 键行没有）⇒ 解析层不得要求它存在；幂等键只认本地复算。"""
    seen = 0
    for path in sorted(FIX.glob("*.jsonl")):
        for env in [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]:
            seen += 1
            topic = path.stem if path.stem != "errors" else env["topic"]  # errors 行各自带真实 topic
            assert pc.envelope_error(env, topic) is None
            assert env["src_hash"] == pc.canonical_src_hash(env["payload"])
    assert seen == 142



def test_jczq_offer_block_structure():
    """② 91 行 = 17 场（85 全量第 1 批 5 块 + 第 2 批 6 行跨批变化），crs 块 66 键 = (28 比分格 + 3 其它) × 2 旗标 + 4 元键；
    跨批 6 行只动 *f 旗标 ⇒ 3 场各多 1 行（4/7/8 块），不许按「每场恰 5 块」断言。"""
    offers = payloads("jczq_offer")
    by_match: dict = {}
    for payload in offers:
        by_match.setdefault(payload["matchId"], []).append(payload["block"])
    assert len(offers) == 91 and len(by_match) == 17
    assert all(sorted(set(blocks)) == ["crs", "had", "hafu", "hhad", "ttg"] for blocks in by_match.values())
    options = next(p["options"] for p in offers if p["block"] == "crs")
    scores = [key for key in options if re.fullmatch(r"s\d{2}s\d{2}$", key)]
    others = [key for key in options if re.fullmatch(r"s1s[hda]$", key)]
    assert len(scores) == 28 and len(others) == 3 and len(options) == 66 == (len(scores) + len(others)) * 2 + 4


def test_jczq_offer_goal_line_parsed_columns():
    """③ 真包 HHAD 让球线分布 +1/+1.00×7、-1/-1.00×8、-2/-2.00×1、+2/+2.00×1、-3/-3.00×1（options.goalLine/goalLineValue）；
    顶层无让球线键、HAD 块两键为空串 ⇒ goal_line/goal_line_value 一律 None（不是 0）；让球线只在 options 里，不许换位置。"""
    parsed = [pc.parse_jczq_offer(payload, SNAP) for payload in payloads("jczq_offer")]
    lines = {(row["row"]["goal_line"], row["row"]["goal_line_value"])
             for row in parsed if row["row"]["play_type"] == "hhad"}
    assert lines == {("+1", Decimal("1.00")), ("-1", Decimal("-1.00")), ("-2", Decimal("-2.00")),
                     ("+2", Decimal("2.00")), ("-3", Decimal("-3.00"))}
    for row in parsed:
        if row["row"]["play_type"] != "hhad":
            assert row["row"]["goal_line"] is None and row["row"]["goal_line_value"] is None
    assert parsed[0]["table"] == "fact.jc_offer"
    assert parsed[0]["pk"] == {"match_id": 2041485, "play_type": "had", "snap_ts": SNAP}  # 真包首场=周二006
    assert parsed[0]["row"]["options"]["goalLine"] == ""  # 原值照抄，清洗只发生在解析列
    assert parsed[0]["match"]["kickoff_bj"].strftime("%Y-%m-%d %H:%M") == "2026-09-16 00:00"  # matchDate+matchTime


def test_jczq_result_cancel_and_empty_sp():
    """④ 15 行真包赛果：取消/未开一律用官方三字段组合（sectionsNo999='' + matchResultStatus='1' + poolStatus='Close'，
    实测 2 行）判定，真包无字面量「取消」（0 行），不许拿中文串做匹配；空 SP 2 行 ⇒ sp_home None；让球线原值照抄。"""
    parsed = [pc.parse_line(env) for env in rows("jczq_result")]
    cancelled = [row["row"] for row in parsed if row["row"]["sections_no_999"] == ""
                 and row["row"]["match_result_status"] == "1" and row["row"]["pool_status"] == "Close"]
    assert len(cancelled) == 2  # 周二004/周二005（真包实测）
    assert all(row["ft_h"] is None and row["ft_a"] is None and row["sections_no_999"] == "" for row in cancelled)
    assert sum(1 for row in parsed if row["row"]["sp_home"] is None) == 2  # 非竞彩开盘场次：空 SP 是常态
    goals = {row["row"]["goal_line"] for row in parsed}
    assert goals == {"+1", "+2", "-1", "-2"} and all(isinstance(goal, str) for goal in goals)  # 含正号也不许换算
    scored = next(row["row"] for row in parsed if row["row"]["sections_no_999"] == "1:4")  # 周二003 卡塔尔亚vs韩国亚
    assert (scored["ft_h"], scored["ft_a"], scored["sections_no_1"]) == (1, 4, "0:3")
    assert (scored["sp_home"], scored["sp_draw"], scored["sp_away"], scored["win_flag"], scored["pool_status"]) == \
           (Decimal("18.00"), Decimal("7.20"), Decimal("1.07"), "A", "Payout")
