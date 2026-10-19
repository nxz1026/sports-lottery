"""统一「取一次远程 → 落 raw 块」内核：缓存优先、失败也落块、只对网络异常与 5xx 退避重试（成功块判定见 cached）。
顺序 = 源开关 → 缓存 → 预检 → 守卫(提交权) → HTTP → quota.charge → INSERT raw（落块失败必须点名，重跑会再花 1 次）；
预检剩余 0 抛 QuotaExceeded 绝不先发请求；账本按逻辑请求计 1 ⇒ raw 行数 = 账本增量；fetch 返回 (status, body)。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
import urllib.request
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode

from config import source_policy, spend_allowed
from core.constants import TIMEOUT_API_FOOTBALL, TIMEOUT_FOOTBALL_DATA
from core.log import logger
from psycopg import Connection, pq, sql
from psycopg.types.json import Jsonb

from ingest import quota

SOURCES: dict[str, tuple[str, str, int]] = {  # source → (key 环境变量, 鉴权头, 超时秒)；key 名唯一出处是 config
    "api_football": (source_policy("api_football").key_env, "x-apisports-key", TIMEOUT_API_FOOTBALL),
    "football_data": (source_policy("football_data").key_env, "X-Auth-Token", TIMEOUT_FOOTBALL_DATA)}
RAW = {"af_raw": sql.Identifier("raw", "af_raw"), "fd_raw": sql.Identifier("raw", "fd_raw")}
RETRY_DELAYS, RETRY_STATUS = (5, 15, 45), frozenset({0, 500, 502, 503, 504})  # 0=网络层；其余绝不重试
CACHED_SQL = sql.SQL("SELECT http_status, body FROM {} WHERE params_hash = %s AND http_status = 200")
INSERT_SQL = sql.SQL("INSERT INTO {} (endpoint, params, params_hash, http_status, quota_cost, body) VALUES"
                     " (%s, %s, %s, %s, %s, %s) ON CONFLICT (params_hash) WHERE http_status = 200 DO NOTHING")

def params_hash(source: str, endpoint: str, params: dict[str, Any]) -> str:
    """sha256(endpoint + "?" + 排序规范化参数串)：同一逻辑请求（含键序不同的同义 dict）必得同一键。"""
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}; expected {sorted(SOURCES)}")
    return hashlib.sha256(f"{endpoint}?{urlencode(sorted(params.items()))}".encode("utf-8")).hexdigest()

def cached(conn: Connection, table: str, params_hash: str) -> tuple[int, Any] | None:
    """成功块才算「已取过」：返回库里的 (http_status, body)；失败块不在其列 ⇒ 重跑会再请求、再留证。"""
    row = conn.execute(CACHED_SQL.format(RAW[table]), (params_hash,)).fetchone()
    return (row[0], row[1]) if row else None

def _decode(payload: bytes) -> Any:
    try:
        return json.loads(payload)
    except ValueError as exc:  # 截断/非 JSON 也留证：JSONDecodeError 与非法编码都属 ValueError
        return {"_error": f"{type(exc).__name__}: {exc}"}

def _http(source: str, endpoint: str, params: dict[str, Any]) -> tuple[int, Any]:
    """发一次 HTTP：5xx/网络异常各退避 5/15/45s 重试三次，其余（含 429/401/404）一次即返。"""
    key_env, header, timeout = SOURCES[source]
    if not (key := os.environ.get(key_env, "").strip()):  # 无 key 必 401：宁可不发，别白烧一次配额
        raise RuntimeError(f"{key_env} 未设置：key 只从环境变量取，不发无名请求")
    req = urllib.request.Request(f"{endpoint}?{urlencode(sorted(params.items()))}",
                                 headers={"User-Agent": "LeaguePredict/5.0", header: key})
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, _decode(resp.read())
        except HTTPError as exc:  # 非 2xx 也是「取到了」：真状态码 + 响应体一起留证
            status, body = exc.code, _decode(exc.read())
        except (OSError, HTTPException) as exc:  # 连不上/超时/DNS/TLS/响应截断 → 网络层，记 0
            status, body = 0, {"_error": f"{type(exc).__name__}: {exc}"}
        if status not in RETRY_STATUS or attempt == len(RETRY_DELAYS):
            return status, body
        logger.warning(f"[{source}] {status or '网络异常'}：第 {attempt + 1} 次退避 {RETRY_DELAYS[attempt]}s 后重试")
        time.sleep(RETRY_DELAYS[attempt])

def _guard(conn: Connection) -> None:
    """发请求前的提交权守卫（§9-19）：非 autocommit 上 cached() 的 SELECT 已开隐式事务 ⇒ transaction() 降级成 SAVEPOINT、RELEASE 不是提交。"""
    if not conn.autocommit and conn.pgconn.transaction_status != pq.TransactionStatus.IDLE:
        raise RuntimeError("提交权不可降级：要么 autocommit，要么在 IDLE 上开事务"
                           f"（当前 status={conn.pgconn.transaction_status}，IDLE={pq.TransactionStatus.IDLE}）")

def fetch(conn: Connection, *, source: str, table: str, endpoint: str, params: dict[str, Any],
          cap: int) -> tuple[int, Any]:
    """源开关最先（关闭的源零请求零写库，连缓存都不读）；随后缓存优先；命中成功块即零请求零配额，越界先抛。"""
    spend_allowed(source)  # README §2-D9：关闭/未放行的付费源在取 key 与动库之前就炸，账本与 raw 均不动
    ph = params_hash(source, endpoint, params)
    if (hit := cached(conn, table, ph)) is not None:
        return hit
    day = dt.date.today()
    if quota.remaining(conn, day, source, cap) < 1:
        raise quota.QuotaExceeded(f"{source} {day} 预检剩余 0（cap={cap}）：未发请求即收工")
    _guard(conn)  # 发请求之前：提交权已降级就必须炸，绝不允许静默 SAVEPOINT
    status, body = _http(source, endpoint, params)
    try:
        quota.charge(conn, day, source, cap=cap, n=1)
        conn.execute(INSERT_SQL.format(RAW[table]), (endpoint, Jsonb(params), ph, status, 1, Jsonb(body)))
    except quota.QuotaExceeded as exc:  # 预检后被并发抢先：请求已发、配额未记
        logger.error(f"[{source}] HTTP {status} 已取但 charge 越界，未落块：重跑会再花 1 次（{exc}）")
        raise
    except Exception as exc:  # INSERT 等失败：配额已花、响应未落块
        logger.error(f"[{source}] 配额已花、原始响应未落块（HTTP {status}）：{exc}；重跑会再花 1 次")
        raise
    logger.info(f"[{source}] 落块 raw.{table} HTTP {status} {ph[:12]}")
    return status, body
