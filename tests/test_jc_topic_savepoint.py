"""R48：逐条保存点隔离的纯单元测试（不依赖真库/真包）。

为什么要有这组用例：R47 生产实测发现，装载器 70% 的批次因
``fact.jc_issue_prize`` 撞唯一键而**整批回滚**（``load_batch`` 捕获异常后
``conn.rollback()``）⇒ 队尾新批次永远落不了库。

原实现只 catch ``ValueError``：``IntegrityError``（约束冲突）会直接掀翻整个批事务。
本组用例锁住「**任何单行失败都不废批**」这条不变量。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from psycopg import errors as pg_errors

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from ingest import jc_topic  # noqa: E402

_INS = {"table": "fact.jc_issue_prize", "pk": {"game_num": "90", "issue_no": "1", "tier_no": 1},
        "row": {"game_num": "90", "issue_no": "1", "tier_no": 1}, "notes": []}


class _Cur:
    """记录 execute 的假游标（保存点语句要能被断言到）。"""

    def __init__(self):
        self.calls: list[str] = []

    def execute(self, q, p=None):
        self.calls.append(str(q))


def _run(writer):
    cur = _Cur()
    rej: list = []
    with patch.object(jc_topic, "parse_lines_all", return_value=[_INS]):
        ups = jc_topic._load_saved(cur, "jc_issue_result", "f.jsonl",
                                   [{"src_hash": "h"}], rej, writer)
    return ups, rej, cur.calls


def test_value_error_isolated_by_savepoint():
    """ValueError：回滚到保存点、记 rejected、批次继续（既有行为）。"""
    def writer(cur, ins, src_hash, rel):
        raise ValueError("未知写指令目标表")

    ups, rej, calls = _run(writer)
    assert ups == 0
    assert len(rej) == 1
    assert "未知写指令目标表" in rej[0]["reason"]
    assert any("rollback to savepoint iw" in c for c in calls)


def test_integrity_error_isolated_by_savepoint():
    """R48：唯一键冲突（IntegrityError）同样走保存点隔离 —— 只废这一行，不废整批。"""
    def writer(cur, ins, src_hash, rel):
        raise pg_errors.UniqueViolation(
            'duplicate key value violates unique constraint "jc_issue_prize_pkey"')

    ups, rej, calls = _run(writer)
    assert ups == 0
    assert len(rej) == 1
    assert "UniqueViolation" in rej[0]["reason"]
    assert "jc_issue_prize" in rej[0]["reason"]
    # 保存点语义：开过 savepoint、冲突后 rollback to（没有 release）
    assert any("savepoint iw" in c for c in calls)
    assert any("rollback to savepoint iw" in c for c in calls)
    assert not any("release savepoint iw" in c for c in calls)


def test_success_releases_savepoint():
    """成功路径：release savepoint、ups 计数 +1、无 rejected。"""
    def writer(cur, ins, src_hash, rel):
        return 1

    ups, rej, calls = _run(writer)
    assert ups == 1
    assert rej == []
    assert any("release savepoint iw" in c for c in calls)
    assert not any("rollback to savepoint iw" in c for c in calls)
