"""契约 v1.1 新增用例（单事务 + 收尾 ROLLBACK；目录一律 tmp）：src_hash 篡改整行拒绝不落库；
kind="error" 真行必须落 stg（丢掉分不清"接口挂了"和"真没比赛"）；同批重推幂等（幂等键 = src_hash，
第二遍 rows_ups=0 且 stg 行数不变）。P0-COLLECT1b 机械拆分自 tests/test_collector_contract.py，
逐字搬移，断言值一条不动；P0-COLLECT1c：共享 fixture/helper 改从 tests/collector_helpers.py import
（全仓唯一一份），本文件只留 3 条 v1.1 用例。"""

from __future__ import annotations

import json
import uuid

# ruff: noqa: F401, F811  # conn 必须显式 import 才能被 pytest fixture 发现（无 conftest.py）
from tests.collector_helpers import FIXTURES, HOST, _drop, _envelope, _q, conn
from ingest.collector_pull import run


def test_dirty_tampered_src_hash_is_rejected(conn, tmp_path):
    """src_hash 被篡改一行 ⇒ 复算不符整行 rejected 且不入库；原件进 bad/、标记进 done/。"""
    good = _envelope("jczq_offer", {"matchId": 2041483, "block": "had", "poolCode": "HAD", "probe": uuid.uuid4().hex})
    tampered = _envelope("jczq_offer", {"matchId": 2041483, "block": "had"}, src_hash="0" * 64)  # 篡改幂等键
    incoming, done, bad, data = _drop(tmp_path, "jczq_offer", [good, tampered])
    assert run(conn, incoming, done, bad)[0]["rejected_n"] == 1
    assert _q(conn, "SELECT count(*) FROM stg.jczq_offer WHERE src_file = %s", data.name)[0][0] == 1  # 只落合法行
    assert not data.exists() and (bad / HOST / "jczq_offer" / data.name).exists()
    assert _q(conn, "SELECT count(*) FROM stg.jczq_offer WHERE src_hash = %s", "0" * 64)[0][0] == 0


def test_kind_error_line_lands_in_stg(conn, tmp_path):
    """kind="error" 行（真实 P0001 失败体）必须落 stg：丢了就分不清"接口挂了"和"真没比赛"。"""
    error = json.loads((FIXTURES / "errors.jsonl").read_text(encoding="utf-8").splitlines()[1])  # topic=jczq_result
    incoming, done, bad, _ = _drop(tmp_path, error["topic"], [json.dumps(error, ensure_ascii=False)])
    assert run(conn, incoming, done, bad)[0]["rows_ups"] == 1
    assert _q(conn, "SELECT line->>'kind', line->'payload'->>'errorCode' FROM stg.jczq_result WHERE src_hash = %s",
              error["src_hash"])[0] == ("error", "P0001")


def test_clean_file_repush_is_idempotent(conn, tmp_path):
    """同批重推 ⇒ 第二遍 rows_ups=0（幂等键 = src_hash，ON CONFLICT DO NOTHING），stg 只留一行。"""
    payload = {"lotteryGameNum": "85", "lotteryDrawNum": "26105", "prizeLevelList": [], "probe": uuid.uuid4().hex}
    line = _envelope("lottery_draw", payload)
    for attempt in range(2):
        incoming, done, bad, data = _drop(tmp_path / f"try{attempt}", "lottery_draw", [line])
        report = run(conn, incoming, done, bad)[0]
        expected = 1 if attempt == 0 else 0  # 第二遍：一行都不新增
        assert (report["rows_in"], report["rows_ups"],
                _q(conn, "SELECT rows_ups FROM ops.ingest_log WHERE src_file = %s", data.name)[0][0]) == \
               (1, expected, expected)
        assert (done / HOST / "lottery_draw" / data.name).exists() and not (bad / HOST).exists()
    assert _q(conn, "SELECT count(*) FROM stg.lottery_draw WHERE src_hash = %s",
              json.loads(line)["src_hash"])[0][0] == 1
