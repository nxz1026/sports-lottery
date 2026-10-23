"""P2-C（2026-10-23）「会话与运维」9 条口径缺陷的行为级回归。

逐条对应审计里的 9 条（静态 + 动态运行 + web 逐元素三路审计的「会话与运维」组）：

  C1 登录 cookie 的 Secure 必须跟着**实际对外协议**走（旧：只看静态 USE_HTTPS=0）
  C2 鉴权路径**零写** SQLite（旧：每次鉴权都 DELETE + commit ⇒ 写锁竞争 ⇒ 500）
  C3 /health 的 code_sha **有缓存**（旧：每次请求 spawn 一个 git 子进程）
  C3b /health 不回传**服务器绝对路径**（免鉴权端点 ⇒ 泄露部署布局）
  C4 /api/v1/ai/status 不回传**服务器绝对路径**
  C5 登录限速计数**跨重启存活**且单调（旧：进程内存 ⇒ 重启即清零）
  C6 ops.ingest_log 的 (at) 索引有 DDL 文件（**只写文件，不连库**）
  C7 rows_in / rows_ups 口径**定义清楚**并能区分「读入/落库/未落库」
  C8 异形文件名**不许掀翻整批**（文件级隔离 + 留痕）
  C9 隔离事件**入台账**且**幂等**（不重复记账）+ ops 视图能查到

原则：
- **零外部依赖**：不连 PostgreSQL、不连网络；DB 相关断言全部用假 cursor / 假连接。
- **不写库**：本文件不调用任何 ``commit()``（真库用例一律 rollback —— 硬约束）。
- 凭证只走 env，测试里不出现任何真实口令。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ─────────────────────────── 公共小工具 ───────────────────────────


class _Req:
    """最小 Request 替身：够 ``_client_ip`` / ``_request_is_https`` 用。"""

    def __init__(self, host: str = "127.0.0.1", headers: dict | None = None,
                 scheme: str = "http"):
        self.client = SimpleNamespace(host=host)
        self.headers = headers or {}
        self.url = SimpleNamespace(scheme=scheme)


class _Cur:
    """最小 cursor：记录 (sql, params)。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ingest_params(cur: _Cur) -> list[tuple]:
    """台账写入的参数（``ops.ingest_log``）。"""
    return [p for s, p in cur.calls if "ops.ingest_log" in s and p]


# ══════════════════════ C1 · cookie Secure 跟着实际协议 ══════════════════════


def test_c1_secure_true_for_https_via_trusted_proxy(monkeypatch):
    """C1：生产 .env 是 USE_HTTPS=0，但公网是 https（nginx 透传 X-Forwarded-Proto）
    ⇒ **受信反代**报 https 时，cookie **必须**带 Secure（旧实现只看静态 env ⇒ 漏）。"""
    from web import auth

    monkeypatch.setattr("web.config.USE_HTTPS", False)
    req = _Req(host="127.0.0.1", headers={"x-forwarded-proto": "https"})
    assert auth._cookie_secure(req) is True


def test_c1_secure_true_when_use_https_flag_set(monkeypatch):
    """C1：运维显式声明 USE_HTTPS=1 ⇒ 一律带 Secure。"""
    from web import auth

    monkeypatch.setattr("web.config.USE_HTTPS", True)
    assert auth._cookie_secure(_Req()) is True


def test_c1_secure_false_for_local_http(monkeypatch):
    """C1：**本地 http 开发环境不能直接登不上** ⇒ 明文 http 时**不**带 Secure
    （浏览器拒收带 Secure 的 http cookie）。这是明确的降级口径。"""
    from web import auth

    monkeypatch.setattr("web.config.USE_HTTPS", False)
    assert auth._cookie_secure(_Req(host="127.0.0.1")) is False


def test_c1_untrusted_client_cannot_forge_forwarded_proto(monkeypatch):
    """C1：X-Forwarded-Proto 只有**直连 IP 在白名单内**才采信 ⇒ 外部伪造无效。"""
    from web import auth

    monkeypatch.setattr("web.config.USE_HTTPS", False)
    monkeypatch.setattr("web.config.TRUSTED_PROXY_IPS", ("127.0.0.1", "::1"))
    forged = _Req(host="203.0.113.9", headers={"x-forwarded-proto": "https"})
    assert auth._cookie_secure(forged) is False


def test_c1_cookie_keeps_httponly_and_samesite(monkeypatch):
    """C1：修 Secure 时**不许**把 HttpOnly / SameSite 弄丢。"""
    from web import auth

    monkeypatch.setattr("web.config.USE_HTTPS", False)
    seen = {}

    class _Resp:
        def set_cookie(self, *args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs

    auth._set_session_cookie(_Resp(), "tok", secure=auth._cookie_secure(_Req()))
    assert seen["kwargs"]["httponly"] is True
    assert seen["kwargs"]["samesite"] == "lax"
    assert seen["kwargs"]["secure"] is False
    assert seen["args"][0] == "lp_session"


# ══════════════════════ C2 · 鉴权路径零写 SQLite ══════════════════════


def _session_db_path(tmp_path, monkeypatch) -> str:
    db = tmp_path / "sessions.db"
    monkeypatch.setattr("web.config.SESSION_DB_PATH", str(db))
    return str(db)


def test_c2_validate_token_performs_zero_sqlite_writes(tmp_path, monkeypatch):
    """C2：鉴权 N 次 ⇒ SQLite **写次数为 0**（旧实现每次都 DELETE + commit
    ⇒ 每个鉴权请求一次写事务 ⇒ 写锁竞争 ⇒ 5s 后 database is locked ⇒ 500）。"""
    from web import session_store

    db = _session_db_path(tmp_path, monkeypatch)
    token = session_store.create_session()

    stmts: list[str] = []
    orig_connect = session_store._connect

    def _spy_connect():
        conn = orig_connect()
        conn.set_trace_callback(stmts.append)
        return conn

    monkeypatch.setattr(session_store, "_connect", _spy_connect)
    # 先暖一次（建表 DDL 只在进程第一次发生，不属于「鉴权路径的写」）
    assert session_store.validate_token(token) is True
    stmts.clear()

    for _ in range(25):
        assert session_store.validate_token(token) is True

    writes = [s for s in stmts
              if s.strip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))]
    assert writes == [], f"鉴权路径不许写库，实际写了：{writes}"
    assert stmts, "（守卫：本次确实执行了 SQL，避免空跑假绿）"

    with sqlite3.connect(db) as conn:
        n = conn.execute("select count(*) from sessions").fetchone()[0]
    assert n == 1, "鉴权不许改变表内容"


def test_c2_expired_sessions_are_still_cleaned(tmp_path, monkeypatch):
    """C2：过期会话**仍然会被清理**（不能无限增长）——后台清理线程已武装，
    且清理口 purge_expired_sessions() 真的删得掉过期行。"""
    from web import session_store

    db = _session_db_path(tmp_path, monkeypatch)
    short = session_store.create_session(ttl_seconds=1)
    long_lived = session_store.create_session(ttl_seconds=3600)
    time.sleep(1.1)

    assert session_store.validate_token(short) is False, "过期会话必须判无效"
    assert session_store.validate_token(long_lived) is True

    deleted = session_store.purge_expired_sessions()
    assert deleted >= 1
    with sqlite3.connect(db) as conn:
        rows = conn.execute("select token from sessions").fetchall()
    assert [r[0] for r in rows] == [long_lived]
    assert session_store._sweeper_started is True, "后台清理线程必须已武装（否则长期运行会无限增长）"


def test_c2_concurrent_auth_has_no_lock_error(tmp_path, monkeypatch):
    """C2：并发鉴权不许出现 ``database is locked``（旧实现的 500 来源）。"""
    from web import session_store

    _session_db_path(tmp_path, monkeypatch)
    token = session_store.create_session()
    errors: list[BaseException] = []

    def _worker():
        try:
            for _ in range(20):
                assert session_store.validate_token(token) is True
        except BaseException as exc:  # noqa: BLE001 - 测试要看见任何异常
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], f"并发鉴权出现异常：{errors!r}"


# ══════════════════════ C3 · /health 的 code_sha 缓存 + 不泄露路径 ══════════════════════


def test_c3_second_health_request_does_not_spawn_subprocess(tmp_path, monkeypatch):
    """C3：/health 每次请求 spawn 一个 git 子进程 ⇒ 可被免鉴权地放大。
    ⇒ 第二次请求**不再** spawn（进程内缓存 + TTL）。"""
    from web.services import health

    calls: list[list] = []

    class _Completed:
        stdout = "abc1234\n"
        stderr = ""
        returncode = 0

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Completed()

    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setattr("web.config.SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr("web.services.store.OUTPUT_DIR", tmp_path)
    health._code_sha_cache.update({"value": None, "at": 0.0})  # 清缓存 ⇒ 第一次必然 spawn

    first = health.check()
    assert len(calls) == 1, "第一次请求允许 spawn 一次"
    assert first["code_sha"] == "abc1234"

    second = health.check()
    assert len(calls) == 1, f"第二次请求不许再 spawn 子进程（实际 calls={calls}）"
    assert second["code_sha"] == "abc1234"


def test_c3_health_body_has_no_server_absolute_path(tmp_path, monkeypatch):
    """C3：/health **免鉴权** ⇒ 绝不能回传服务器绝对路径（部署布局对攻击者有用）。"""
    from web.services import health

    db = tmp_path / "sessions.db"
    monkeypatch.setattr("web.config.SESSION_DB_PATH", str(db))
    monkeypatch.setattr("web.services.store.OUTPUT_DIR", tmp_path)
    out = health.check()
    label = out["checks"]["sessions_db"]["path"]
    assert not Path(label).is_absolute(), f"会话库路径必须脱敏：{label!r}"
    assert str(tmp_path) not in json.dumps(out, default=str), "响应里不许出现服务器绝对路径"
    assert label == db.name or label.endswith("sessions.db")


# ══════════════════════ C4 · ai/status 不回传绝对路径 ══════════════════════


def test_c4_ai_status_does_not_leak_absolute_path(tmp_path, monkeypatch):
    """C4：/api/v1/ai/status 原来回传 ``source`` = 服务端 ai_scores.json 的**绝对路径**
    ⇒ 改成相对/逻辑路径（键集合不变，只改值）。"""
    from web.routers import ai as ai_router

    abs_path = str(tmp_path / "predictions" / "ai_scores.json")
    monkeypatch.setattr("web.services.ai.ai_status",
                        lambda: {"available": True, "matches": 3,
                                 "by_league": {"英超": 3}, "source": abs_path})
    out = ai_router.ai_get_status(request=None, _=None)
    assert set(out) == {"available", "matches", "by_league", "source"}, "键集合不许变"
    assert not Path(out["source"]).is_absolute(), f"不许回传绝对路径：{out['source']!r}"
    assert out["source"] == "ai_scores.json"


def test_c4_sanitizer_keeps_relative_and_non_path_values():
    """C4：脱敏只动「绝对路径」⇒ 相对路径 / 模型名等原样透传。"""
    from web.routers.ai import _sanitize_paths

    payload = {"source": "predictions/ai_scores.json", "model": "gpt-4o-mini",
               "items": [{"path": "/opt/app/predictions/x.json"}]}
    out = _sanitize_paths(payload)
    assert out["source"] == "predictions/ai_scores.json"
    assert out["model"] == "gpt-4o-mini"
    assert out["items"][0]["path"] == "x.json"


# ══════════════════════ C5 · 登录限速计数跨重启存活 ══════════════════════


def test_c5_failure_counter_survives_restart_and_is_monotonic(tmp_path, monkeypatch):
    """C5：限速计数原来在**进程内存** ⇒ 运维一重启就清零 ⇒ 暴力破解窗口重开。
    ⇒ 计数落盘、跨重启恢复、**单调**（不被重启重置）。"""
    from web import auth

    _session_db_path(tmp_path, monkeypatch)
    monkeypatch.setattr("web.config.LOGIN_MAX_FAILURES", 3)
    monkeypatch.setattr("web.config.LOGIN_LOCKOUT_SECONDS", 60)
    auth._failures.clear()

    ip = "198.51.100.7"
    auth._record_failure(ip)
    auth._record_failure(ip)
    assert auth._failures[ip]["count"] == 2

    # 模拟「服务重启」：进程内存清空（落盘那份还在）
    auth._failures.clear()
    assert ip not in auth._failures

    # 登录流程必定先过限速闸门 ⇒ 第一次尝试就把计数接上
    auth._check_lockout(ip)  # 还没到阈值 ⇒ 不锁
    assert auth._failures[ip]["count"] == 2, "重启后计数必须从落盘恢复（不能清零）"

    # 第 3 次失败 ⇒ 达阈值 ⇒ 锁定（旧实现要第 5 次才锁）
    auth._record_failure(ip)
    with pytest.raises(Exception) as exc:
        auth._check_lockout(ip)
    assert getattr(exc.value, "http_status", None) == 429


def test_c5_lockout_window_survives_restart(tmp_path, monkeypatch):
    """C5：**锁定窗口**跨重启仍有效（重启不等于解除锁定）。"""
    from web import auth

    _session_db_path(tmp_path, monkeypatch)
    monkeypatch.setattr("web.config.LOGIN_MAX_FAILURES", 1)
    monkeypatch.setattr("web.config.LOGIN_LOCKOUT_SECONDS", 600)
    auth._failures.clear()

    ip = "198.51.100.8"
    auth._record_failure(ip)
    with pytest.raises(Exception):
        auth._check_lockout(ip)

    auth._failures.clear()  # 重启
    with pytest.raises(Exception) as exc:
        auth._check_lockout(ip)
    assert getattr(exc.value, "http_status", None) == 429


def test_c5_successful_login_clears_durable_counter(tmp_path, monkeypatch):
    """C5：登录成功 ⇒ 计数与**落盘那份**一起清（下次重启不会复活旧计数）。"""
    from web import auth, session_store

    _session_db_path(tmp_path, monkeypatch)
    monkeypatch.setattr("web.config.LOGIN_MAX_FAILURES", 5)
    auth._failures.clear()

    ip = "198.51.100.9"
    auth._record_failure(ip)
    assert session_store.load_login_failure(ip) is not None
    auth._clear_failures(ip)
    assert session_store.load_login_failure(ip) is None
    assert ip not in auth._failures


# ══════════════════════ C6 · ingest_log(at) 索引 DDL ══════════════════════


def test_c6_ingest_log_at_index_ddl_exists_and_is_concurrent():
    """C6：ops.ingest_log 没有 (at) 索引 ⇒ 每日保留清理全表扫 419 MB。
    ⇒ 必须有 DDL 文件（**只写文件，不连库**），且用 CONCURRENTLY（避免长时间锁写）。"""
    sql_path = REPO_ROOT / "docs" / "db" / "infra_p2_ingest_log_index.sql"
    assert sql_path.is_file(), "缺少 ingest_log 索引迁移文件"
    text = sql_path.read_text(encoding="utf-8")
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in text
    assert "ops.ingest_log" in text
    assert "(at)" in text
    assert "UTC" not in text, "SQL 常量里不许出现 UTC"
    # 真 schema 的列名/表名要对得上（docs/db/infra_p0_2_schemas.sql）
    schema = (REPO_ROOT / "docs" / "db" / "infra_p0_2_schemas.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS ops.ingest_log" in schema
    assert "at timestamptz NOT NULL DEFAULT now()" in schema


# ══════════════════════ C7 · rows_in / rows_ups 口径 ══════════════════════


def test_c7_caliber_is_documented_at_the_producer():
    """C7：口径必须**写在生产端**（不是只写在某个人的脑子里）。"""
    src = (REPO_ROOT / "scripts" / "ingest" / "jc_topic.py").read_text(encoding="utf-8")
    assert "rows_in" in src and "rows_ups" in src
    assert "读入行数" in src and "落库行数" in src, "口径定义必须写在 jc_topic.py 里"
    assert "幂等跳过" in src and "设计跳过" in src, "两种「未落库」必须区分写清"


def test_c7_ops_caliber_endpoint_splits_read_written_and_skipped(tmp_path, monkeypatch):
    """C7：接口上要能区分「读入」「落库」「未落库（幂等跳过 / 设计跳过）」。"""
    from web.routers import jc as jc_router

    class _FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return list(self._rows)

    class _FakeConn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            if "jsonb_array_length" in sql:
                return _FakeCursor([
                    ("jczq_offer", 10, 100, 90, 2, 8, 2, "2026-10-23 00:00:00+00"),
                    ("jclq_offer", 4, 40, 0, 0, 4, 0, "2026-10-23 00:00:00+00"),
                    ("jc_odds_history", 2, 5, 30, 0, 2, 0, "2026-10-23 00:00:00+00"),
                ])
            return _FakeCursor([("quarantined", 3), ("bad-json", 1)])

    conn = _FakeConn()

    @contextmanager
    def _fake_read_conn(role="ro"):
        yield conn

    import store.pg as pg_mod

    monkeypatch.setattr(pg_mod, "read_conn", _fake_read_conn)
    out = jc_router.ops_ingest_caliber(days=7, _=None)

    by_topic = {r["topic"]: r for r in out["rows"]}
    assert by_topic["jczq_offer"]["rows_in"] == 100
    assert by_topic["jczq_offer"]["rows_ups"] == 90
    assert by_topic["jczq_offer"]["rows_rejected"] == 2
    assert by_topic["jczq_offer"]["rows_skipped"] == 8
    assert by_topic["jczq_offer"]["skipped_kind"] == "idempotent"

    assert by_topic["jclq_offer"]["rows_skipped"] == 40
    assert by_topic["jclq_offer"]["skipped_kind"] == "by-design"

    # 展开口径（一个信封展开成 N 行）⇒ 不许相减
    assert by_topic["jc_odds_history"]["skipped_kind"] == "n/a"
    assert by_topic["jc_odds_history"]["rows_skipped"] is None

    assert out["n_quarantined"] == 4
    assert out["degraded"] is False
    assert set(out) >= {"window_days", "rows", "totals", "quarantine", "n_quarantined",
                         "caliber", "degraded", "note"}


# ══════════════════════ C8 · 异形文件名不许掀翻整批 ══════════════════════


def test_c8_stamp_raises_typed_error_not_attributeerror():
    """C8：``_stamp`` 原来直接 ``m.group(1)``（不校验 m）⇒ AttributeError ⇒ 掀翻整批。
    ⇒ 现在抛**可区分**的类型（沿用 P1 的 UnreadableFileError 范式）。"""
    from ingest import jc_read

    with pytest.raises(jc_read.InvalidFileNameError):
        jc_read._stamp("weird-name.jsonl")
    with pytest.raises(jc_read.InvalidFileNameError):
        jc_read._stamp("2026-13-45T99-99__001.jsonl")  # 非法时间戳


def test_c8_window_isolates_malformed_filename(tmp_path):
    """C8：topic 目录里一个异形文件名 ⇒ **不许掀翻整批** ⇒ 其余文件照常归属，
    异形文件以 state="badname" 交回（⇒ 入台账）。"""
    from ingest import jc_read

    topic_dir = tmp_path / "jczq_offer"
    topic_dir.mkdir(parents=True)
    good = topic_dir / "2026-10-23T10-00Z__001.jsonl"
    good.write_text('{"a":1}\n', encoding="utf-8")
    bad = topic_dir / "接口说明.jsonl"
    bad.write_text('{"a":1}\n', encoding="utf-8")

    marker = tmp_path / "2026-10-23T10-00Z.done"
    marker.write_text("", encoding="utf-8")   # 0 字节 marker ⇒ 走时间戳窗口

    out = jc_read.files_for_batch(tmp_path, marker, ("jczq_offer",))
    states = {p.name: st for p, st in out["jczq_offer"]}
    assert states["接口说明.jsonl"] == "badname", "异形文件名要被标成 badname（留痕）"
    assert states["2026-10-23T10-00Z__001.jsonl"] == "jsonl", "好文件照常归属"


def test_c8_load_topic_records_bad_filename_without_flipping_batch(tmp_path):
    """C8：异形文件 ⇒ **记失败 + 原因**（入台账，ok=false），**不掀翻整批**。"""
    from ingest import jc_topic

    cur = _Cur()
    marker = tmp_path / "m.done"
    marker.write_text("x\n", encoding="utf-8")
    weird = tmp_path / "jczq_offer" / "接口说明.jsonl"
    weird.parent.mkdir(parents=True, exist_ok=True)
    weird.write_text('{"a":1}\n', encoding="utf-8")

    out = jc_topic.load_topic(cur, tmp_path, marker, "jczq_offer", weird, "badname")
    assert set(out) == {"topic", "lines", "ups", "gap"}, "返回键集合不许变"
    assert out["ups"] == 0 and out["gap"] is False

    params = _ingest_params(cur)
    assert params, "异形文件必须入台账"
    assert params[0][5] is False, "状态必须是「已隔离/失败」而不是成功"
    assert "bad-filename" in str(params[0][4])
    assert not any("ops.file_arrival" in s for s, _ in cur.calls), "没装载 ⇒ 不许写「已装载」凭据"


# ══════════════════════ C9 · 隔离事件入台账 + 幂等 + 可见 ══════════════════════


def test_c9_quarantine_record_is_idempotent_by_sql_guard(tmp_path):
    """C9：隔离事件必须入台账，且**不重复记账**（重试幂等）
    ⇒ 去重判定做在 SQL 里（NOT EXISTS + 保留窗口），不是进程内判断。"""
    from ingest import jc_topic

    cur = _Cur()
    jc_topic.record_quarantine(cur, "jczq_offer", "jczq_offer/x.jsonl",
                               "quarantined", "撕裂 JSON")
    sql, params = cur.calls[0]
    assert "ops.ingest_log" in sql
    assert "not exists" in sql.lower(), "去重判定必须在 SQL 里（跨进程有效）"
    assert "at > now()" in sql, "去重窗口必须与保留窗口对齐"
    assert params[5] is False, "状态必须表达「已隔离」而不是「成功」"
    assert "quarantined" in str(params[4])
    assert params[0] == "jczq_offer" and params[1] == "jczq_offer/x.jsonl"


def test_c9_broken_file_is_recorded_not_silently_dropped(tmp_path):
    """C9：坏文件（内容撕裂）⇒ 台账**有痕迹**（ok=false + 原因），
    **不掀翻整批**，且**不写**「已装载」凭据。"""
    from ingest import jc_topic

    cur = _Cur()
    marker = tmp_path / "m.done"
    marker.write_text("x\n", encoding="utf-8")
    broken = tmp_path / "jczq_offer" / "2026-10-23T10-00Z__001.jsonl"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text('{"a":1}\n{"b":\n', encoding="utf-8")   # 撕裂 JSON

    out = jc_topic.load_topic(cur, tmp_path, marker, "jczq_offer", broken, "jsonl")
    assert set(out) == {"topic", "lines", "ups", "gap"}
    assert out["ups"] == 0

    params = _ingest_params(cur)
    assert params, "坏文件必须入台账"
    assert params[0][5] is False
    assert "bad-json" in str(params[0][4])
    assert not any("ops.file_arrival" in s for s, _ in cur.calls)


def test_c9_quarantine_is_queryable_in_ops_view():
    """C9：隔离事件要在 ops 视图里**查得到** ⇒ 口径视图按原因分组给出隔离计数，
    且既有的 /api/jc/ops「rejects」段（ok=false + rejected 非空）天然收得到。"""
    from web.routers import jc as jc_router

    sql = jc_router._QUARANTINE_SQL
    assert "ok = false" in sql, "隔离 = 失败状态（不是成功）"
    assert "ops.ingest_log" in sql
    # 隔离原因清单（作为参数传入 SQL ⇒ 断言清单本身，不在 SQL 文本里）
    for reason in ("quarantined", "bad-filename", "bad-json", "permission-denied"):
        assert reason in jc_router._QUARANTINE_REASONS, \
            f"隔离原因 {reason} 必须能被 ops 视图查到"


def test_c9_cli_entrypoint_exists_for_ops_script():
    """C9：运维脚本（docs/infra/jc-staging-retention.sh，不在本轮文件范围内）
    搬走坏文件时需要**幂等**记账入口 ⇒ 提供 CLI（本轮只提供入口 + 接线建议）。"""
    from ingest import jc_topic

    assert callable(jc_topic._record_quarantine_cli)
    src = (REPO_ROOT / "scripts" / "ingest" / "jc_topic.py").read_text(encoding="utf-8")
    assert "--record-quarantine" in src or "record-quarantine" in src
