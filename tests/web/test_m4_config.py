"""M4 配置健壮性：坏 env 不崩溃 + 生产模式默认口令守卫。

覆盖工单 WO-M7b2：
- _env_int 缺省/非法值一律回退默认值，绝不因坏 env 让进程崩溃；
- 非本机 HOST 且 AUTH_PASSWORD 仍为占位口令时，模块加载即 RuntimeError。
"""
from __future__ import annotations

import importlib
import logging

import pytest

import web.config


def test_env_int_missing_env_returns_default() -> None:
    """env 名不存在 → 直接回退默认值。"""
    assert web.config._env_int("PORT_坏境变量名", 8000) == 8000


def test_env_int_bad_value_returns_default(monkeypatch, caplog) -> None:
    """env 值非整数 → 记 warning 并回退默认值，不抛异常。"""
    monkeypatch.setenv("WEB_PORT", "abc")
    with caplog.at_level(logging.WARNING, logger="web.config"):
        assert web.config._env_int("WEB_PORT", 8000) == 8000
    assert "WEB_PORT" in caplog.text


def test_prod_guard_rejects_default_password(monkeypatch) -> None:
    """非本机 HOST + 进程未设 AUTH_PASSWORD → 模块加载即 RuntimeError。

    2026-09-18 补丁：.env 现含 AUTH_PASSWORD，reload config 时 load_dotenv 会从
    磁盘读回真实值、遮蔽"未设置"场景。为隔离真实 .env，本测试把 load_dotenv
    patch 成 no-op，使进程 env 完全决定结果，真正验证守卫路径。
    """
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("WEB_HOST", "0.0.0.0")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="禁止使用默认口令"):
        importlib.reload(web.config)

    # 还原：清掉 WEB_HOST 后 reload 必须恢复正常，且不得污染其它测试文件。
    monkeypatch.delenv("WEB_HOST", raising=False)
    importlib.reload(web.config)
    assert web.config.HOST == "127.0.0.1"
    assert web.config.AUTH_PASSWORD == web.config._DEFAULT_PW
