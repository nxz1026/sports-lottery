"""PG 连接工厂：角色名 → DSN，外加只读/写入两种上下文管理器。

口令只存在于 repo/.env（600，gitignored）；本模块与调用方一律不打印 DSN。
角色分工见 docs/infra/README-infra.md：ing=装载写 / app=模型只读 / ro=人工分析。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from core import constants  # noqa: F401  # 导入即触发 core/constants.py 的 _load_dotenv()

ROLE_DSN_ENV: dict[str, str] = {"ing": "DSN_ING", "app": "DSN_APP", "ro": "DSN_RO"}
CONNECT_TIMEOUT_S: int = 5


def dsn(role: str = "ing") -> str:
    """按角色名取 DSN；变量缺失即抛，绝不静默降级成另一个角色。"""
    if role not in ROLE_DSN_ENV:
        raise ValueError(f"unknown role {role!r}; expected {sorted(ROLE_DSN_ENV)}")
    env_key = ROLE_DSN_ENV[role]
    value = os.environ.get(env_key, "").strip()
    if not value:
        raise RuntimeError(f"{env_key} 未设置：连接信息只走环境变量，不写进代码或日志")
    return value


def connect(role: str = "ing") -> psycopg.Connection:
    """裸连接，不托管事务：调用方自行 commit/rollback（测试靠它在收尾整体 rollback）。"""
    return psycopg.connect(dsn(role), connect_timeout=CONNECT_TIMEOUT_S)


@contextmanager
def write_conn(role: str = "ing") -> Iterator[psycopg.Connection]:
    """写事务：正常退出 commit，异常 rollback，退出即关连接。"""
    conn = connect(role)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def read_conn(role: str = "ro") -> Iterator[psycopg.Connection]:
    """只读事务：退出前强制 rollback（永不 commit），保证只读上下文不落任何写。"""
    conn = connect(role)
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
