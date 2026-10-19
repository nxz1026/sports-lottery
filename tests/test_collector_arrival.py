"""采集契约"批到达"去向用例（P0-COLLECT1c 自 test_collector_contract.py 机械搬移，断言一条不动；
唯一修改 = 队长点名第 1 处：report["ok"] 半句 → report["rejected_n"] == 0，依据
scripts/ingest/collector_pull.py——load_file 返回体 {rows_in, rows_ups, rejected_n, rejected}（:55），
_one 补 topic/src_file/archived_to（:83），返回体无 ok 键；ingest_log 的 ok 列写库口径正是
rejected_n==0（:67-68），故 rejected_n==0 即"这批干净"的同义表达）：
零拒绝 → 原件与 .done 都进 done/；坏行（非 JSON / 非 object / 缺外壳八字段）全进 rejected 且不入库
（原件进 bad/、标记进 done/）；缺 .done（半包）→ 原地不动。
同批重推幂等 / src_hash 篡改 / kind="error" 落 stg 见 tests/test_collector_v11.py。"""

# ruff: noqa: F401, F811  # conn 必须显式 import 才能被 pytest fixture 发现（无 conftest.py）
from __future__ import annotations

import json
import uuid

from ingest.collector_pull import run

from tests.collector_helpers import HOST, _drop, _envelope, _q, conn


def test_dirty_file_rejects_bad_lines_and_archives_to_bad(conn, tmp_path):
    """非 JSON / 非 object / 缺外壳八字段 → 全进 rejected 且不入库；原件进 bad/、标记进 done/。"""
    good = _envelope("jczq_offer", {"matchId": 2041483, "block": "had", "poolCode": "HAD", "probe": uuid.uuid4().hex})
    incoming, done, bad, data = _drop(tmp_path, "jczq_offer",
                                      [good, "{not json", "[1, 2]", json.dumps({"payload": {}}, ensure_ascii=False)])
    size = data.stat().st_size
    assert run(conn, incoming, done, bad)[0]["rejected_n"] == 3
    assert _q(conn, "SELECT count(*) FROM stg.jczq_offer WHERE src_file = %s", data.name)[0][0] == 1  # 只落合法行
    assert _q(conn, "SELECT rows_in, rows_ups, ok, jsonb_array_length(rejected) FROM ops.ingest_log"
                    " WHERE src_file = %s", data.name)[0] == (4, 1, False, 3)
    assert _q(conn, "SELECT bytes, rows, done_marker FROM ops.file_arrival WHERE src_file = %s",
              str(data.relative_to(incoming)))[0] == (size, 4, True)
    assert not data.exists() and (bad / HOST / "jczq_offer" / data.name).exists()
    assert (done / HOST / "jczq_offer" / f"{data.name}.done").exists()


def test_clean_file_archives_to_done(conn, tmp_path):
    """零拒绝 → 原件与 .done 都进 done/、ingest_log.ok=true（同批重推幂等见 test_collector_v11.py）。"""
    payload = {"lotteryGameNum": "85", "lotteryDrawNum": "26105", "prizeLevelList": [], "probe": uuid.uuid4().hex}
    line = _envelope("lottery_draw", payload)
    incoming, done, bad, data = _drop(tmp_path, "lottery_draw", [line])
    report = run(conn, incoming, done, bad)[0]
    assert report["rows_in"] == 1 and report["rows_ups"] == 1 and report["rejected_n"] == 0
    assert (done / HOST / "lottery_draw" / data.name).exists() and not (bad / HOST).exists()


def test_file_without_done_marker_is_ignored(conn, tmp_path):
    """缺 .done（疑似半包）→ 原地不动、不入库、不留 ingest_log。"""
    payload = {"matchId": 2041524, "sectionsNo1": "2:1", "sectionsNo999": "5:1", "probe": uuid.uuid4().hex}
    incoming, done, bad, data = _drop(tmp_path, "jczq_result", [_envelope("jczq_result", payload)], marker=False)
    assert run(conn, incoming, done, bad) == []  # 没标记 → 整批不处理
    assert data.exists() and not (done / HOST).exists() and not (bad / HOST).exists()
    assert _q(conn, "SELECT count(*) FROM ops.ingest_log WHERE src_file = %s", data.name)[0][0] == 0
    assert _q(conn, "SELECT count(*) FROM stg.jczq_result WHERE src_file = %s", data.name)[0][0] == 0
