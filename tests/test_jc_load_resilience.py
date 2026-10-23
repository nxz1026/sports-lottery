"""2026-10-23：装载器「坏文件不再拖死整批」+ 「gaps 口径」的行为级回归。

背景（生产实测，不是假想）：
- ``ops.ingest_log`` 累计 **1208 万行 / 1740 MB** 且**还在长**：实测最近 24h 写
  **10201 行**，其中**没有一个新文件** ⇒ 全是同一批老文件被**每 10 分钟重装一遍**
  （每小时 402 行 / 55 个文件）。
- 根因链（逐层实测）：盘上的**历史坏文件**（采集端 TOCTOU 竞态写出的「两批内容
  交织」文件，实测 29 个）⇒ 该文件读不出来 ⇒ ``read_lines`` 抛异常 ⇒
  ``_load_file`` 失败 ⇒ **整批 marker** 标 ``ok=false`` ⇒ 水位（只认 ok=true）
  失效 ⇒ 那几批**每 10 分钟被重装一遍** ⇒ 每天 1 万行重复日志（且无信息增量）。

⇒ 本文件锁住三件事：
  ① **清单列了、但文件不存在** ⇒ 记**真缺档**（gaps +1）+ 留痕（ingest_log ok=false），
     但**不掀翻整批**（不抛异常）⇒ 批次能标 ok=true ⇒ 停止重复重装；
  ② **清单里压根没有该 topic**（残缺批 / 单 topic 推送）⇒ state="absent"
     ⇒ **不是缺档**、不落任何痕迹 ⇒ **gaps 不再虚高**（实测 49 条 gaps 全是这类假缺档）；
  ③ **清单列了、文件在、但行数与声明不符**（rejected）⇒ 行为**不变**（照常装载，
     done=true）—— ticket P0-COLLECT2q3b 明定的契约，不许趁改口径时顺手改掉。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from ingest import jc_topic  # noqa: E402


class _Cur:
    """最小 cursor：记录 execute 的 SQL 与参数。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sqls(cur: _Cur) -> str:
    return "\n".join(s for s, _ in cur.calls)


# ── ① 文件不存在 ⇒ 真缺档 + 不掀翻整批 ─────────────────────────────────

def test_missing_file_records_gap_without_raising(tmp_path):
    """清单列了但文件不存在 ⇒ 返回 gap=True，**不抛异常**（不掀翻整批）。"""
    cur = _Cur()
    marker = tmp_path / "2026-10-23T00-00Z.done"
    marker.write_text("x\n", encoding="utf-8")
    gone = tmp_path / "jczq_offer" / "2026-10-23T00-00Z__001.jsonl"   # **不存在**

    with patch.object(jc_topic, "read_lines", side_effect=AssertionError(
            "文件不存在时不该走到 read_lines（那会 FileNotFoundError 掀翻整批）")):
        out = jc_topic.load_topic(cur, tmp_path, marker, "jczq_offer", gone, "jsonl")

    assert out["gap"] is True, "文件真丢了 ⇒ 必须是真缺档"
    sql = _sqls(cur)
    assert "ops.ingest_log" in sql, "要留痕（排障能查到哪个文件读不出来）"
    assert "ops.file_arrival" in sql, "真缺档要写 file_arrival ⇒ gaps 计数 +1"
    # 「#MISSING」在**参数**里（src_file 形态 `<marker>/<topic>#MISSING`），不在 SQL 文本里
    params = [p for _s, p in cur.calls if p]
    assert any("#MISSING" in str(p) for p in params), \
        f"file_arrival 的 src_file 必须是 <marker>/<topic>#MISSING 形态: {params}"


def test_missing_file_does_not_produce_marker_failure(tmp_path):
    """⚠️ 关键：**load_topic 返回正常 dict**（不抛）⇒ load_batch 不收 errors
    ⇒ 批次能标 ok=true ⇒ **停止每 10 分钟重复重装**。"""
    cur = _Cur()
    marker = tmp_path / "m.done"
    marker.write_text("x\n", encoding="utf-8")
    gone = tmp_path / "jczq_offer" / "gone.jsonl"

    out = jc_topic.load_topic(cur, tmp_path, marker, "jczq_offer", gone, "jsonl")
    assert isinstance(out, dict)
    assert out.get("ups") == 0
    assert "gap" in out


# ── ② 清单没列该 topic ⇒ absent（不是缺档） ────────────────────────────

def test_absent_topic_leaves_no_trace(tmp_path):
    """state="absent" ⇒ **不写** ingest_log / file_arrival ⇒ gaps 不虚高。"""
    cur = _Cur()
    marker = tmp_path / "m.done"
    marker.write_text("x\n", encoding="utf-8")

    out = jc_topic.load_topic(cur, tmp_path, marker, "jczq_offer", None, "absent")

    assert out["gap"] is False
    # ⚠️ 返回值的键集合必须与其它路径**完全一致**（既有契约：
    #    tests/test_jc_topic_issue.py 断言 set(ret) == {"topic","lines","ups","gap"}）
    #    ⇒ 这里不许塞 "absent" 之类的额外键（2026-10-23 在 Oracle 上实测踩过）。
    assert set(out) == {"topic", "lines", "ups", "gap"}
    sql = _sqls(cur)
    assert "ops.ingest_log" not in sql, "absent 不该写 ingest_log（没发生过装载）"
    assert "ops.file_arrival" not in sql, "absent 不该写 file_arrival（否则假缺档）"


# ── ③ rejected（行数不符、文件在）⇒ 行为不变（照常装载） ────────────────

def test_rejected_file_with_rows_mismatch_still_loads(tmp_path):
    """ticket P0-COLLECT2q3b 契约：rejected ⇒ **照常装载**（done=true）。

    ⚠️ 「行数不符」不等于「读不出来」：文件在、能读 ⇒ 照装。
    （不许趁修 gaps 口径时把这条契约改掉。）
    """
    cur = _Cur()
    marker = tmp_path / "m.done"
    marker.write_text("x\n", encoding="utf-8")
    f = tmp_path / "jczq_offer" / "a.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('{"kind":"line"}\n', encoding="utf-8")

    with patch.object(jc_topic, "read_lines", return_value=[{"kind": "line"}]):
        out = jc_topic.load_topic(cur, tmp_path, marker, "jczq_offer", f, "rejected")

    assert out["gap"] is False, "rejected 不是缺档（文件在盘上）"
    assert "lines" in out
