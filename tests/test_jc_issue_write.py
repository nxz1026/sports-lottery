"""P0-COLLECT2eA2 写手测试：三表通用 upsert_issue_instruction。fixture 独占连接收尾 rollback+close；"库 0 行"另开 ro（§9-83）。

真包测试需真库（DSN_ING / DSN_RO）。无真库环境 → 自动 skip（不假失败）。
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from ingest import jc_issue_write  # noqa: E402
from store import pg  # noqa: E402

# 守门：需真库 DSN_ING（写）+ DSN_RO（验证回滚）。无 DSN → skip。
pytestmark = pytest.mark.skipif(
    not all(os.environ.get(k) for k in ("DSN_ING", "DSN_RO")),
    reason="真包写手测试需 DSN_ING/DSN_RO",
)

# 本模块每次运行一个唯一键前缀 ⇒ 绝不与 cron 装进来的真数据撞键。
# 2026-09-17 实测：合成键 26127/26128/26001 与真实期号撞车，"count(*)==1" 变成 2
# ⇒ 绝对计数在生产库上必然假失败，一律改按本模块自己的键计数。
RUN = "t" + uuid.uuid4().hex[:8]
PK = {"game_num": "90", "issue_no": RUN + "26127"}
ISSUE = {"table": "fact.jc_issue", "pk": PK, "notes": [],
         "row": {**PK, "game_name": "竞彩足球", "sale_begin": datetime(2026, 9, 16, 19, 0),
                  "sale_end": datetime(2026, 9, 19, 1, 0), "draw_at": datetime(2026, 9, 19, 2, 0),
                  "n_matches": 14, "draw_num_list": ["001", "002"], "raw_head": {"topic": "jc_issue"}}}
DRAW = {"table": "fact.jc_issue_draw", "pk": {"game_num": "90", "issue_no": RUN + "26128"}, "notes": [],
        "row": {"game_num": "90", "issue_no": RUN + "26128", "game_key": "sfc", "game_name": "竞彩足球",
                "draw_result": "1-0", "pool_after": "1234.5", "sales": "100.0", "pool_after_rj": "",
                "sales_rj": "", "paid_begin": datetime(2026, 9, 19, 3, 0), "paid_end": datetime(2026, 9, 19, 12, 0),
                "is_delay": 0, "delay_remark": ""}}
LOT = {"table": "fact.lottery_draw", "pk": {"game_num": "84", "issue_no": RUN + "26001"}, "notes": [],
       "row": {"game_num": "84", "issue_no": RUN + "26001", "game_name": "大乐透", "draw_date": date(2026, 9, 15),
               "status": 1, "numbers_raw": "01 02 03 + 08 09", "numbers": [1, 2, 3, 8, 9],
               "pool": "5000000.00", "prizes": [{"name": "一等", "amount": 2000000}], "equipment_count": 2}}

@pytest.fixture()
def conn():
    try:
        connection = pg.connect("ing")
    except Exception as exc:
        pytest.skip(f"no db: {type(exc).__name__}: {exc}")
    try:
        yield connection  # 用例里不要再套 closing / with
    finally:
        connection.rollback()  # 库里最终 0 行的保证
        connection.close()

def _write(cur, ins):
    return jc_issue_write.upsert_issue_instruction(cur, ins, "t2eA", "t2eA.jsonl")

def test_one_row_per_table(conn):
    cur = conn.cursor()
    for ins in (ISSUE, DRAW, LOT):
        assert _write(cur, ins) == 1
        n, fs = cur.execute(f"select count(*), max(first_seen_at) from {ins['table']} "
                            "where issue_no=%s", [ins["pk"]["issue_no"]]).fetchone()
        assert n == 1 and fs is not None

def test_replay_same_pk_updates_not_duplicates(conn):
    cur = conn.cursor()
    assert _write(cur, DRAW) == 1
    r1 = cur.execute("select draw_result, first_seen_at, last_seen_at from fact.jc_issue_draw "
                     "where issue_no=%s", [RUN + "26128"]).fetchone()
    again = {**DRAW, "row": {**DRAW["row"], "draw_result": "2-1"}}
    assert _write(cur, again) == 1
    assert cur.execute("select count(*) from fact.jc_issue_draw where issue_no=%s",
                       [RUN + "26128"]).fetchone()[0] == 1
    r2 = cur.execute("select draw_result, first_seen_at, last_seen_at from fact.jc_issue_draw "
                     "where issue_no=%s", [RUN + "26128"]).fetchone()
    assert r2[0] == "2-1" and r2[1] == r1[1] and r2[2] >= r1[2]

def test_empty_string_becomes_null(conn):
    cur = conn.cursor()
    _write(cur, DRAW)
    row = cur.execute("select delay_remark is null, pool_after_rj is null, sales_rj is null "
                      "from fact.jc_issue_draw where issue_no=%s", [RUN + "26128"]).fetchone()
    assert row == (True, True, True)

def test_bad_instructions_rejected_loudly_good_rows_survive(conn):
    cur = conn.cursor()
    with pytest.raises(ValueError):
        _write(cur, {"table": "fact.evil", "pk": PK, "row": {**PK}})
    with pytest.raises(ValueError):  # 未登记列（解析器漂移防线）
        _write(cur, {**ISSUE, "row": {**ISSUE["row"], "bogus_col": "x"}})
    with pytest.raises(ValueError):  # 主键缺列
        _write(cur, {**DRAW, "pk": {"game_num": "90"}, "row": {"game_num": "90", "draw_result": "x"}})
    assert _write(cur, DRAW) == 1  # 拒绝后同批好行仍可写
    assert cur.execute("select count(*) from fact.jc_issue_draw where issue_no=%s",
                       [RUN + "26128"]).fetchone()[0] == 1

def test_jsonb_roundtrip_not_strified(conn):
    cur = conn.cursor()
    _write(cur, ISSUE)
    _write(cur, LOT)
    nums = cur.execute("select numbers::text from fact.lottery_draw where issue_no=%s",
                       [RUN + "26001"]).fetchone()[0]
    assert json.loads(nums) == [1, 2, 3, 8, 9]
    head = cur.execute("select raw_head::text from fact.jc_issue where issue_no=%s",
                       [RUN + "26127"]).fetchone()[0]
    assert json.loads(head) == {"topic": "jc_issue"}

def test_finale_ro_no_traces_from_this_module():
    """回滚无残留 = 本模块写过的三个键在三表里都不存在。
    原先断言 count(*)==0，但 2026-09-17 起 cron 真在往这三表装数据
    （实测 jc_issue=8 / lottery_draw=120）⇒ 绝对 0 行必然假失败；
    改查"本模块自己的键缺席"：语义不变（回滚是否生效），但不再依赖生产库为空。"""
    with pg.read_conn("ro") as c:
        for t, key in (("fact.jc_issue", "26127"),
                       ("fact.jc_issue_draw", "26128"),
                       ("fact.lottery_draw", "26001")):
            got = c.execute(f"select count(*) from {t} where issue_no=%s",
                            [RUN + key]).fetchone()[0]
            assert got == 0, '%s 残留 %s 行（回滚没生效）' % (t, got)

