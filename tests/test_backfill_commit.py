"""P0-BACKFILL1b 回归锁：提交权不可降级（§9-19 同坑第二例 = 请求无痕蒸发）；A/B/E 用真库绝不提交，C/D 用假连接。"""

from __future__ import annotations

import io
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from ingest import api_get, run_backfill  # noqa: E402
from store import pg  # noqa: E402

ROWS = run_backfill.plan_rows(("af",), ("2099",))  # 不存在的赛季号：hash 与真数据绝不撞（2 条即可复现）


@pytest.fixture(autouse=True)
def _fake_api_keys(monkeypatch):
    """假 key（占位即可，不发真请求）：``api_get._http`` 在 ``urlopen`` 之前必查 key_env，
    无 key 直接 RuntimeError；测试不需要真 key，但需要它"非空"绕过校验。
    """
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-fake-key")
    monkeypatch.setenv("FOOTBALL_DATA_KEY", "test-fake-key")

class _Stop(Exception):  # A 的哨兵：探针只到 HTTP 派发点就中断本轮，绝不真发请求、绝不落块
    ...

class _FakeConn:
    """假连接兼假游标（零 DB 零写入）：charge 的 RETURNING 与落块探针给一行，缓存/剩余额度给 None。"""
    autocommit = True
    __enter__ = lambda self: self  # noqa: E731  下面这几个 lambda 都是无状态小接口，写成 def 只是多占行数
    __exit__ = lambda self, *exc: False
    cursor = lambda self: _FakeConn()
    transaction = lambda self: nullcontext()
    execute = lambda self, sql, params=None: setattr(self, "sql", str(sql)) or self  # 记下 SQL 并返回自身
    fetchone = lambda self: (1,) if "RETURNING used" in self.sql or "SELECT 1 FROM" in self.sql else None

@pytest.fixture()
def conn():
    """真库连接：守卫只认 autocommit 连接；绝不提交 ⇒ 写入方自己套 force_rollback 真事务。"""
    try:
        connection = pg.connect("ing")
    except Exception as exc:  # 无库/无凭据：skip 不 fail
        pytest.skip(f"no db: {type(exc).__name__}: {exc}")
    connection.autocommit = True
    yield connection
    connection.rollback()
    connection.close()

def _install(monkeypatch, responses: list) -> list:
    """假 urlopen：按调用序号取 responses（末项复用）；>=400 抛真 HTTPError（与生产同一条分支）。"""
    calls: list = []
    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        code, body = responses[min(len(calls) - 1, len(responses) - 1)]
        if code >= 400:
            raise HTTPError(req.full_url, code, "e", {}, io.BytesIO(json.dumps(body).encode()))
        return nullcontext(SimpleNamespace(status=code, read=lambda: json.dumps(body).encode()))
    monkeypatch.setattr(api_get.urllib.request, "urlopen", urlopen)
    return calls

def _fetch(conn, row: dict):
    return api_get.fetch(conn, **{k: row[k] for k in ("source", "table", "endpoint", "params", "cap")})

def test_main_sets_autocommit_before_requests(conn, monkeypatch):
    """A：main --execute 走的连接在发请求前已 autocommit（守卫不破）；探针不真发请求、不落块。"""
    seen: list = []
    def stop(*a, **kw):
        seen.append(conn.autocommit)
        raise _Stop
    monkeypatch.setattr(api_get, "_http", stop)
    rc = run_backfill.main(["--execute", "--source", "af", "--seasons", "2099", "--limit", "1"], conn=conn)
    assert (seen, rc) == ([True], 1)

def test_fetch_refuses_degraded_commit_authority(conn, monkeypatch):
    """B：autocommit 被强行设回 False → 发请求之前 RuntimeError，假 transport 调用次数 == 0（不烧配额）。"""
    calls = _install(monkeypatch, [(200, {"ok": 1})])
    conn.autocommit = False
    with pytest.raises(RuntimeError, match="提交权不可降级"):
        _fetch(conn, ROWS[0])
    assert calls == []

def test_429_stops_the_round_after_first_request(monkeypatch, caplog):
    """C：429 → run 第一轮就正常收工（rc=0），剩余计划条一条都不再发（假 transport 计数 == 1）。"""
    calls = _install(monkeypatch, [(429, {})])
    assert run_backfill.run(_FakeConn(), ROWS, dry=False, limit=5) == 0
    # ROWS = AF 源单赛季计划条 = 6 个有 api_football_id 的联赛（2026-10-20 起含欧冠）
    assert (len(calls), len(ROWS)) == (1, 6) and "限流，本轮到此为止（10 req/min）" in caplog.text

def test_pacing_sleeps_only_between_requests(monkeypatch):
    """D：两条之间 sleep(7) 恰一次；第一条之前不 sleep（假 sleep 只记序列，绝不真等）。"""
    calls, slept = _install(monkeypatch, [(200, {"ok": 1})]), []
    monkeypatch.setattr(run_backfill.time, "sleep", slept.append)
    assert run_backfill.run(_FakeConn(), ROWS[:2], dry=False, limit=2) == 0
    assert (len(calls), slept) == (2, [7])

def test_failure_block_and_success_block_coexist(conn, monkeypatch):
    """E：同 hash 先 429 再 200 ⇒ 两行都在（partial unique 只锁 200）；第三次命中成功块零请求零新行。"""
    calls = _install(monkeypatch, [(429, {}), (200, {"ok": 1})])
    with conn.transaction(force_rollback=True):  # 真事务 + 收尾 ROLLBACK：两行都在、测试零留痕
        seen = [_fetch(conn, ROWS[0]) for _ in range(3)]
        probe = "SELECT count(*) FROM raw.af_raw WHERE params_hash = %s"
        rows = conn.execute(probe, (ROWS[0]["hash"],)).fetchone()[0]
    assert (seen, len(calls), rows) == ([(429, {}), (200, {"ok": 1}), (200, {"ok": 1})], 2, 2)
