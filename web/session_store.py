"""web.session_store — SQLite 会话存储（仅 stdlib sqlite3）。

模型：token(uuid4hex) / created(UTC epoch) / expires(UTC epoch)。
- 默认 TTL 12h（config.SESSION_TTL_SECONDS）。
- 过期行在每次访问时惰性清理（check_same_thread=False + 每请求短连接，
  线程安全无需锁）。
- 时间一律 UTC epoch 秒；展示换算 BJT 由上层负责。
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from web import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token   TEXT PRIMARY KEY,
    created REAL NOT NULL,
    expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires);
"""


def _connect() -> sqlite3.Connection:
    Path(config.SESSION_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.SESSION_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


_db_initialized = False
_db_init_lock = threading.Lock()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """首次调用执行建表 DDL，后续请求直接跳过（进程级 once 哨兵）。"""
    global _db_initialized
    if _db_initialized:
        return
    with _db_init_lock:
        if not _db_initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            _db_initialized = True


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """显式关闭的连接上下文：with conn: 提交语义 + finally 关闭连接。"""
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def create_session(ttl_seconds: int | None = None) -> str:
    """创建会话，返回 token。ttl 覆盖 config 默认值（测试用）。"""
    ttl = ttl_seconds if ttl_seconds is not None else config.SESSION_TTL_SECONDS
    token = uuid.uuid4().hex
    now = time.time()
    with _db() as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO sessions (token, created, expires) VALUES (?, ?, ?)",
            (token, now, now + ttl),
        )
    return token


def validate_token(token: str) -> bool:
    """token 有效（存在且未过期）则 True，并惰性清理过期行。"""
    with _db() as conn:
        _ensure_schema(conn)
        _purge_expired(conn)
        row = conn.execute(
            "SELECT expires FROM sessions WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return False
        if row["expires"] < time.time():
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return False
        return True


def delete_session(token: str) -> None:
    """主动失效（logout）。"""
    with _db() as conn:
        _ensure_schema(conn)
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


def _purge_expired(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
    conn.commit()
    return cur.rowcount


def purge_expired_sessions() -> int:
    """启动清理口：建连接+建表+清理过期会话，返回删除行数（幂等兜底）。"""
    with _db() as conn:
        _ensure_schema(conn)
        return _purge_expired(conn)
