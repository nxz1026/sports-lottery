"""契约 v1.1 官方 payload → 规范行（纯函数：零 DB、零网络、零副作用，只用 stdlib）。
§5.0 口径「采集机是搬运工」：官方原值列一律照抄——"取消" / "3＋,1" / "---" / "国  安" / lotterySaleEndtime
不清洗、不改名、不换算；数字与时间只写进新列（kickoff_bj 由 matchDate+matchTime 拼、ft_h 由 sectionsNo999 拆、
goal_line_value 由 goalLineValue 转），原值列同排保留。外壳校验（八字段、snap_ts 以 Z 结尾、src_hash 本地复算）
也在本层，装载层只负责把它接进逐行 rejected。每 topic 一个 parse_x(payload)；返回 {"table","pk","row","notes"}；
结构坏（缺身份键 / 类型不对 / 列表与声明不符）raise ValueError 点名键；kind="error" 不产 fact 行（parse_line
返回 None）。规格表写法 "官方键[:转换码]>规范列"，转换码见 CAST；带 :c 的时间列缺/空/解析不了一律 None。
P0-COLLECT1b 机械拆分：值换算器在 parse_values，各 topic 解析器在 parse_jczq / parse_jcissue / parse_misc；
本壳留 §5.0 外壳校验 + parse_line 分派（单向 import，分派表 7 个 topic 一览），原公共名从本壳 re-export。
"""

from __future__ import annotations

import json
from typing import Any

from .parse_values import canonical_src_hash, clock, need, dec, pair, split_numbers, build, out, CAST
from .parse_jczq import BLOCKS, MATCH, RESULT, parse_jczq_offer, parse_jczq_result
from .parse_jcissue import GAME_KEYS, ISSUE, DRAW, parse_jc_issue, parse_jc_issue_result
from .parse_misc import LOTTERY, parse_lottery_draw, parse_jclq_offer, parse_jclq_result

ENVELOPE = {"kind": str, "topic": str, "snap_ts": str, "endpoint": str, "http_status": int,
            "collector_host": str, "payload": dict, "src_hash": str}
KINDS = ("line", "error")

__all__ = ["ENVELOPE", "KINDS", "BLOCKS", "GAME_KEYS", "CAST", "MATCH", "RESULT", "ISSUE", "DRAW", "LOTTERY",
           "canonical_src_hash", "row_error", "envelope_error", "need", "dec", "clock", "pair", "build", "out",
           "split_numbers", "parse_jczq_offer", "parse_jczq_result", "parse_jc_issue", "parse_jc_issue_result",
           "parse_lottery_draw", "parse_jclq_offer", "parse_jclq_result", "PARSERS", "parse_line"]


def envelope_error(obj: Any, topic: str) -> str | None:
    """§5.0 外壳校验：合法返回 None，否则返回拒绝原因（kind="error" 行同样必须过这一关，错误信息不可丢）。"""
    if not isinstance(obj, dict):
        return "not a json object"
    lack = [key for key in ENVELOPE if key not in obj]
    if lack:
        return f"missing envelope field(s): {','.join(lack)}"
    wrong = [key for key, kind in ENVELOPE.items()
             if isinstance(obj[key], bool) or not isinstance(obj[key], kind)]
    if wrong:
        return f"envelope field(s) type mismatch: {','.join(wrong)}"
    if obj["kind"] not in KINDS:
        return f"bad kind {obj['kind']!r}"
    if obj["topic"] != topic:
        return f"topic {obj['topic']!r} != file topic {topic!r}"
    if not all(obj[key] for key in ("snap_ts", "endpoint", "collector_host", "src_hash")):
        return "snap_ts/endpoint/collector_host/src_hash 不能为空"
    if not obj["snap_ts"].endswith("Z") or clock(obj["snap_ts"]) is None:
        return f"bad snap_ts {obj['snap_ts']!r}：须 UTC 且以 Z 结尾"
    if obj["src_hash"] != canonical_src_hash(obj["payload"]):
        return "src_hash 复算不符：整行拒绝"
    return None


def row_error(line: str, topic: str) -> tuple[dict | None, str | None]:
    """一行 JSON 文本 → (外壳合规的对象, 拒绝原因)；非 JSON / 外壳不合规都拒绝。"""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, f"invalid json: {exc.msg}"
    return obj, envelope_error(obj, topic)


PARSERS: dict[str, Any] = {"jczq_offer": parse_jczq_offer, "jczq_result": parse_jczq_result, "jc_issue": parse_jc_issue,
                           "jc_issue_result": parse_jc_issue_result, "jclq_offer": parse_jclq_offer,
                           "jclq_result": parse_jclq_result, "lottery_draw": parse_lottery_draw}


def parse_line(env: dict) -> dict | None:
    """外壳行 → 规范行：kind="error" 返回 None（错误行只留 stg）；未知 topic / 结构坏一律 raise。
    P0-COLLECT2：jc_issue / jc_issue_result 返回 list[dict]（parent+children）⇒ 仅取 parent（向下兼容）；
    children 改由 parse_lines_all(env) → list[dict|None] 一并返回。"""
    if env.get("kind") == "error":
        return None
    topic = env.get("topic")
    if topic not in PARSERS:
        raise ValueError(f"未知 topic {topic!r}")
    parse = PARSERS[topic]
    parsed = parse(env["payload"], env.get("snap_ts")) if topic == "jczq_offer" else parse(env["payload"])
    if isinstance(parsed, list):
        return parsed[0] if parsed else None
    return parsed


def parse_lines_all(env: dict) -> list[dict | None]:
    """外壳行 → 全部规范行（parent + children），jc_issue/jc_issue_result 返回多行；其它单行 ⇒ [单]。"""
    if env.get("kind") == "error":
        return [None]
    topic = env.get("topic")
    if topic not in PARSERS:
        raise ValueError(f"未知 topic {topic!r}")
    parse = PARSERS[topic]
    parsed = parse(env["payload"], env.get("snap_ts")) if topic == "jczq_offer" else parse(env["payload"])
    if isinstance(parsed, list):
        return parsed or [None]
    return [parsed]
