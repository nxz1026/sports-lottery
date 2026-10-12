"""P2-2 验收测试：jobs.py::_build_env 白名单透传。

修复点：旧实现 `return dict(os.environ)`，把 AUTH_USERNAME / AUTH_PASSWORD
/ SESSION_DB_PATH 等 web-only 敏感凭据泄露给子进程（任何子进程后续
env dump 都立即泄漏）。

修复：白名单透传 ~28 个关键变量，其余全部丢弃；显式排除 AUTH_*
WEB_* / SESSION_* / LOGIN_* / USE_HTTPS / CORS_ORIGINS。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1].parent
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


def test_excludes_web_only_secrets():
    """AUTH_USERNAME / AUTH_PASSWORD / SESSION_DB_PATH / USE_HTTPS 不应透传。"""
    from web.services import jobs

    # 注入敏感变量到 os.environ
    sentinel = {
        "AUTH_USERNAME": "admin",
        "AUTH_PASSWORD": "supersecret-xyz",
        "SESSION_DB_PATH": "/var/secrets/sessions.db",
        "USE_HTTPS": "1",
        "WEB_HOST": "0.0.0.0",
        "WEB_PORT": "8080",
        "CORS_ORIGINS": "*",
        "SESSION_TTL_SECONDS": "43200",
        "LOGIN_MAX_FAILURES": "5",
    }
    import os
    saved = {k: os.environ.get(k) for k in sentinel}
    try:
        for k, v in sentinel.items():
            os.environ[k] = v
        env = jobs._build_env()
        for k in sentinel:
            assert k not in env, (
                f"P2-2: {k!r} 是 web-only / 敏感变量，不应透传给子进程"
            )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_includes_engine_relevant_keys():
    """predict / ai_analyze 实际用到的 key 必须透传。"""
    from web.services import jobs

    sentinel = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": "/home/ubuntu/DSH/sports-lottery",
        "PYTHONUNBUFFERED": "1",
        "LP_OUTPUT_DIR": "/tmp/lp",
        "API_FOOTBALL_KEY": "abc123",
        "ODDS_API_KEY": "odds-key",
        "NBA_API_KEY": "nba-key",
        "LLM_API_KEY": "llm-key",
        "DB_HOST": "127.0.0.1",
        "DSN_ING": "postgresql://x:y@127.0.0.1/db",
    }
    import os
    saved = {k: os.environ.get(k) for k in sentinel}
    try:
        for k, v in sentinel.items():
            os.environ[k] = v
        env = jobs._build_env()
        for k, v in sentinel.items():
            assert env.get(k) == v, (
                f"子进程实际需要 {k!r}={v!r}，got {env.get(k)!r}"
            )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_unknown_env_var_dropped():
    """未列在白名单的任意变量都被丢弃（最小特权）。"""
    from web.services import jobs

    import os
    saved = os.environ.get("TOTALLY_RANDOM_XYZ_VAR")
    try:
        os.environ["TOTALLY_RANDOM_XYZ_VAR"] = "secret-stuff"
        env = jobs._build_env()
        assert "TOTALLY_RANDOM_XYZ_VAR" not in env
    finally:
        if saved is None:
            os.environ.pop("TOTALLY_RANDOM_XYZ_VAR", None)
        else:
            os.environ["TOTALLY_RANDOM_XYZ_VAR"] = saved


def test_build_env_does_not_mutate_os_environ():
    """_build_env 必须返回新 dict，不应改动 os.environ。"""
    from web.services import jobs

    import os
    before = dict(os.environ)
    env = jobs._build_env()
    # 反向修改
    env["SHOULD_NOT_LEAK"] = "x"
    assert "SHOULD_NOT_LEAK" not in os.environ, "_build_env 共享了 os.environ 引用"
    # os.environ 与快照一致
    assert dict(os.environ) == before


def test_build_env_handles_missing_keys():
    """未设置的可选变量允许不存在；不应该抛 KeyError。"""
    from web.services import jobs

    import os
    saved = {k: os.environ.pop(k, None) for k in ("API_FOOTBALL_KEY", "LLM_API_KEY", "DSN_ING")}
    try:
        env = jobs._build_env()
        assert "API_FOOTBALL_KEY" not in env
        assert "LLM_API_KEY" not in env
        assert "DSN_ING" not in env
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v