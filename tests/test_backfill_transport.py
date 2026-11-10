"""api_get 传输内核回归锁：假 urlopen（零真网络）+ 真库单事务收尾 ROLLBACK（零留痕、零真实花销）。
钉死 A（200 命中零请求、配额只扣一次、键序无关）、B（坏块也落块且记真状态码；失败块不算已取过 ⇒ 重跑会重取）、
C（只有 5xx/网络异常退避 5/15/45s，429 一次即返）；连不上库 → skip 带原因；提交权守卫见 test_backfill_commit。"""

from __future__ import annotations

import datetime as dt
import io
import json
import sys
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.bjt import now_bjt  # noqa: E402
from ingest import api_get, quota  # noqa: E402
from store import pg  # noqa: E402

CAPS = {"api_football": ("af_raw", 100), "football_data": ("fd_raw", 30)}


@pytest.fixture()
def conn():
    try:
        connection = pg.connect("ing")
    except Exception as exc:  # 无库/无凭据：skip 不 fail
        pytest.skip(f"no db: {type(exc).__name__}: {exc}")
    connection.autocommit = True    # api_get 守卫只认 autocommit 连接（非 autocommit 必抛 RuntimeError）
    connection.execute("BEGIN")     # 手开真事务：写入留在里面，收尾 rollback，绝不提交
    yield connection
    connection.rollback()
    connection.close()


def _install(monkeypatch, responses: list) -> list:
    """假 urlopen：按调用序号取 responses（末项复用）；>=400 抛真 HTTPError，异常对象原样抛出。"""
    calls: list = []
    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        if item[0] >= 400:  # 非 2xx 走真 HTTPError 分支，与生产路径同一分支
            raise HTTPError(req.full_url, item[0], "e", {}, io.BytesIO(json.dumps(item[1]).encode()))
        return nullcontext(SimpleNamespace(status=item[0], read=lambda: json.dumps(item[1]).encode()))

    monkeypatch.setattr(api_get.urllib.request, "urlopen", urlopen)
    return calls


def _args(source: str, params: dict) -> dict:
    table, cap = CAPS[source]
    url = f"https://example.invalid/fixtures/{uuid.uuid4().hex}"  # 一次性 endpoint 段：与库内残留不同键
    return {"source": source, "table": table, "endpoint": url, "params": params, "cap": cap}


@pytest.mark.parametrize("source,params,body,status", [
    ("api_football", {"league": 39, "season": "2024"}, {"response": [1, 2]}, 200),
    ("football_data", {"season": "2024"}, {"errors": {"plan": "no access"}}, 404),
])
def test_cache_hit_and_error_block_are_not_refetched(conn, monkeypatch, source, params, body, status):
    """A+B：200 块二次调用零请求、配额只扣一次；4xx 不算「已取过」⇒ 重取留第二块（失败不再永久挡路）。"""
    calls = _install(monkeypatch, [(status, body)])
    args = _args(source, params)
    if (before := quota.remaining(conn, now_bjt().date(), source, CAPS[source][1])) < 1:
        pytest.skip(f"今日 {source} 余额 {before}")
    n = 1 if status == 200 else 2  # 只有成功块算「已取过」：失败块重跑会再请求、再落一块
    with conn.transaction():
        first = api_get.fetch(conn, **args)
        again = api_get.fetch(conn, **{**args, "params": dict(reversed(list(params.items())))})
        row = conn.execute(f"SELECT http_status, quota_cost, body FROM raw.{args['table']} WHERE params_hash = %s",
                           (api_get.params_hash(source, args["endpoint"], params),)).fetchone()
        after = quota.remaining(conn, now_bjt().date(), source, CAPS[source][1])
    assert (len(calls), first, again) == (n, (status, body), (status, body))
    assert (row, after) == ((status, 1, body), before - n)


@pytest.mark.parametrize("responses,calls_n,waits,stored", [
    ([(503, {"e": 1})], 4, [5, 15, 45], {"e": 1}),                      # 5xx：退避三次后如实落块
    ([(429, {})], 1, [], {}),                                           # 429 绝不重试（重试只会更糟）
    ([(503, {}), (503, {}), (200, {"ok": 1})], 3, [5, 15], {"ok": 1}),  # 退避途中成功即停
    ([URLError("boom")], 4, [5, 15, 45], {"_error": "URLError: <urlopen error boom>"}),  # 网络层：退避三次，状态记 0
])
def test_retry_policy(conn, monkeypatch, responses, calls_n, waits, stored):
    """C：只有 5xx 与网络异常退避 5/15/45s（各三次），其余一次即返；退避用假 sleep，绝不真等。"""
    calls = _install(monkeypatch, responses)
    monkeypatch.setattr(api_get.time, "sleep", (slept := []).append)
    args = _args("football_data", {"season": "2023"})
    if quota.remaining(conn, now_bjt().date(), "football_data", 30) < 1:
        pytest.skip("今日 football_data 余额不足")
    with conn.transaction():
        api_get.fetch(conn, **args)
        body = conn.execute(f"SELECT body FROM raw.{args['table']} WHERE endpoint = %s",
                            (args["endpoint"],)).fetchone()[0]
    assert (len(calls), slept, body) == (calls_n, waits, stored)
