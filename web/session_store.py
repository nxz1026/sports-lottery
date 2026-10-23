"""web.session_store — SQLite 会话存储（仅 stdlib sqlite3）。

模型：token(uuid4hex) / created(UTC epoch) / expires(UTC epoch)。
- 默认 TTL 12h（config.SESSION_TTL_SECONDS）。
- ⚠️ **P2-C（2026-10-23）鉴权路径零写**：``validate_token`` 现在**只 SELECT**。
  旧实现每次鉴权都 ``_purge_expired``（DELETE + commit）⇒ **每个鉴权请求 = 一次写事务**
  ⇒ SQLite 写锁竞争 ⇒ ``timeout=5`` 之后 ``database is locked`` ⇒ **鉴权 500**
  （看板开着时：首屏 8 个并发鉴权 + 60s 一次的轮询 ⇒ 所有面板同时变红）。
  ⇒ 过期行改由**后台清理线程**（``PURGE_INTERVAL_SECONDS``）+ 登录时顺带清理。
- ⚠️ **P2-C（2026-10-23）登录限速计数落盘**：``login_failures`` 表让
  「失败计数 / 锁定窗口」**跨进程重启存活** —— 否则运维每重启一次服务，
  暴力破解窗口就被重新打开一次（计数器在进程内存里 ⇒ 重启即清零）。
- WAL：读者不与写者互斥 ⇒ 写锁竞争不再把**读**请求打成 500。
- 时间一律 UTC epoch 秒；展示换算 BJT 由上层负责。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from web import config

logger = logging.getLogger("web.session_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token   TEXT PRIMARY KEY,
    created REAL NOT NULL,
    expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires);
-- 登录失败计数（跨重启存活）：ip → 连续失败次数 / 锁定窗口截止 / 最后失败时刻。
-- ⚠️ 这张表是**限速闸门**的真相来源；进程内存只是热路径缓存（见 web/auth.py）。
CREATE TABLE IF NOT EXISTS login_failures (
    ip              TEXT PRIMARY KEY,
    fail_count      INTEGER NOT NULL DEFAULT 0,
    lockout_until   REAL NOT NULL DEFAULT 0,
    last_failure_at REAL NOT NULL DEFAULT 0
);
"""

# 过期会话的清理节奏（秒）：后台线程周期清理 ⇒ **鉴权路径零写**。
# 不用 env：web/config.py 由别的 worker 拥有，本模块不新增 env 入口。
PURGE_INTERVAL_SECONDS = 300.0


def _connect() -> sqlite3.Connection:
    Path(config.SESSION_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.SESSION_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    # WAL：读者不与写者互斥（P2-C：写锁竞争曾把鉴权打成 500）。
    # 失败也不致命（只读介质 / 网络盘上 WAL 不可用）⇒ 忽略，退回默认日志模式。
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error as exc:  # noqa: BLE001 - 只是优化，失败照常工作
        logger.debug("sessions.db WAL 不可用（退回默认模式）: %s", exc)
    return conn


_db_initialized_paths: set[str] = set()
_db_init_lock = threading.Lock()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """首次调用执行建表 DDL，后续请求直接跳过（**按 DB 路径**的 once 哨兵）。

    ⚠️ **P1（2026-10-23）**：哨兵原来是**一个进程级 bool** ⇒ 只要有任何一次建过表，
    之后**换任何 DB 文件**都不再执行 DDL ⇒ 新库「no such table: sessions」。
    ⇒ 这不只是测试问题：**生产上会话库被删/被轮转/被换路径**时，
    进程会一直报「no such table」直到重启。
    ⇒ 改成**按路径**记哨兵：换库 ⇒ 重新建表。
    """
    key = str(config.SESSION_DB_PATH)
    if key in _db_initialized_paths:
        return
    with _db_init_lock:
        if key not in _db_initialized_paths:
            conn.executescript(_SCHEMA)
            conn.commit()
            _db_initialized_paths.add(key)


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
        # 登录路径顺带清一次过期行（**不在鉴权路径上**）⇒ 表不会无限增长。
        _purge_expired(conn)
    _ensure_sweeper()
    return token


def validate_token(token: str) -> bool:
    """token 有效（存在且**未过期**）则 True。

    ⚠️ **P2-C（2026-10-23）鉴权路径零写**：这里**只 SELECT**，不再 DELETE。
    旧实现每次鉴权都先 ``_purge_expired``（DELETE + commit）⇒ 写锁竞争 ⇒ 500。
    过期行由后台清理线程 + 登录时顺带清理（``PURGE_INTERVAL_SECONDS``）。

    读失败（磁盘满 / 库被换掉）⇒ 降级成「未登录」（401），**不许**把 DB 抖动变成 500。
    """
    if not token:
        return False
    try:
        with _db() as conn:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT expires FROM sessions WHERE token = ? AND expires > ?",
                (token, time.time()),
            ).fetchone()
            return row is not None
    except sqlite3.Error as exc:  # noqa: BLE001 - 读失败降级为「未登录」
        logger.warning("会话校验读失败（降级为未登录）: %s", exc)
        return False


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
    """启动清理口：建连接+建表+清理过期会话，返回删除行数（幂等兜底）。

    同时**启动后台清理线程** ⇒ 长期运行的进程里过期会话也会被定期清掉
    （鉴权路径不能写库 ⇒ 清理必须挪到这条非鉴权路径上）。
    """
    with _db() as conn:
        _ensure_schema(conn)
        deleted = _purge_expired(conn)
    _ensure_sweeper()
    return deleted


# ── 后台清理线程（鉴权路径零写的关键）────────────────────────────────────

_sweeper_started = False
_sweeper_lock = threading.Lock()


def _sweeper_loop() -> None:
    while True:
        time.sleep(PURGE_INTERVAL_SECONDS)
        try:
            purge_expired_sessions()
        except Exception as exc:  # noqa: BLE001 - 清理失败不许拖死进程
            logger.warning("会话清理线程异常（下一轮再试）: %s", exc)


def _ensure_sweeper() -> None:
    """幂等启动后台清理线程（daemon；进程退出即结束）。"""
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started:
            return
        _sweeper_started = True
        threading.Thread(target=_sweeper_loop, name="session-purge", daemon=True).start()


# ── 登录失败计数（跨重启存活）────────────────────────────────────────────
#
# ⚠️ **P2-C（2026-10-23）**：限速计数器原来只在**进程内存**里 ⇒ 服务一重启就清零
# ⇒ 运维重启一次 = 暴力破解窗口重新打开一次。
# ⇒ 计数落 SQLite（同会话库，独立表）⇒ 跨重启存活、单调（见 web/auth.py 的恢复点）。


def record_login_failure(ip: str, fail_count: int, lockout_until: float,
                         last_failure_at: float) -> None:
    """把登录失败状态**落盘**（跨重启存活）。写失败只记日志，不许把登录打成 500。"""
    try:
        with _db() as conn:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO login_failures (ip, fail_count, lockout_until, last_failure_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(ip) DO UPDATE SET "
                "fail_count=excluded.fail_count, lockout_until=excluded.lockout_until, "
                "last_failure_at=excluded.last_failure_at",
                (ip, int(fail_count), float(lockout_until), float(last_failure_at)),
            )
    except sqlite3.Error as exc:  # noqa: BLE001 - 落盘失败降级为进程内计数
        logger.warning("登录失败计数落盘失败（降级为进程内计数）: %s", exc)


def load_login_failure(ip: str) -> dict | None:
    """读回落盘的登录失败状态；没有 / 读失败 ⇒ None。

    返回的键与进程内记录同形：``{"count", "lockout_until", "last_failure_at"}``。
    """
    try:
        with _db() as conn:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT fail_count, lockout_until, last_failure_at "
                "FROM login_failures WHERE ip = ?",
                (ip,),
            ).fetchone()
            if row is None:
                return None
            return {
                "count": int(row["fail_count"]),
                "lockout_until": float(row["lockout_until"]),
                "last_failure_at": float(row["last_failure_at"]),
            }
    except sqlite3.Error as exc:  # noqa: BLE001 - 读失败降级为「进程内计数」
        logger.warning("登录失败计数读回失败（降级为进程内计数）: %s", exc)
        return None


def clear_login_failure(ip: str) -> None:
    """清掉落盘的登录失败状态（登录成功 / 记录过期）。"""
    try:
        with _db() as conn:
            _ensure_schema(conn)
            conn.execute("DELETE FROM login_failures WHERE ip = ?", (ip,))
            conn.commit()
    except sqlite3.Error as exc:  # noqa: BLE001 - 清理失败只记日志
        logger.warning("登录失败计数清理失败: %s", exc)
