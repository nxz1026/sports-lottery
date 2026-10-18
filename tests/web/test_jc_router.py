"""web/routers/jc 端到端测试：/api/jc/{fixtures,issues,gap,movement,qr}。

背景：web/routers/jc.py 当前 34% 覆盖 — 8 端点 + 私有 _envelope
（test_jc_backtest_api.py 已测 backtest，test_jc_lottery_api.py 已测
lottery，test_jc_daily_image.py 已测 daily-image）。

覆盖点：
  · /fixtures：未鉴权 → 401 / day 校验 / envelope 封装
  · /issues：未鉴权 → 401 / limit 校验（int + 1..200）/ envelope
  · /gap：未鉴权 → 401 / top 钳制 + 五大联赛白名单 / 编排
  · /movement：未鉴权 → 401 / day 校验 / 五大联赛白名单 / envelope
  · /qr：未鉴权 → 401 / url 校验（http/https only）/ PUBLIC_BASE_URL fallback
"""
from __future__ import annotations

import base64
import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com/dashboard/jc/")

    import web.config as config
    import web.session_store as session_store
    config = importlib.reload(config)
    session_store = importlib.reload(session_store)
    import web.auth as auth_mod
    importlib.reload(auth_mod)
    import web.api as api
    importlib.reload(api)

    # mock store 层避免依赖 DSN
    import store.jc_view as jcv
    monkeypatch.setattr(jcv, "fixtures_on", lambda day=None: [
        {"match_num": 7001, "league_cn": "意甲", "home_cn": "A",
         "away_cn": "B", "kickoff_bj": "01-15 20:00", "play_type": "had",
         "snap_ts": "2026-01-15T12:00:00Z", "goal_line": -1.0,
         "options": {"h": "2.0", "d": "3.0", "a": "4.0"}},
    ])
    monkeypatch.setattr(jcv, "issues", lambda n: [
        {"game_num": "90", "issue_no": "26001", "head_source": "jc_issue_result"},
    ])
    monkeypatch.setattr(jcv, "backtest_summary", lambda: [
        {"play_type": "had", "n_fp": 10, "brier": 0.5},
    ])
    monkeypatch.setattr(jcv, "lottery_draws", lambda n: [
        {"game_num": "85", "issue_no": "26001"},
    ])
    monkeypatch.setattr(jcv, "jc_movement",
                        lambda day=None: [
                            {"match_num": 7001, "play_type": "had",
                             "league_cn": "意甲", "home_cn": "A", "away_cn": "B",
                             "opening_odds": {"h": 2.0}, "closing_odds": {"h": 1.8}},
                        ])

    # mock web.services.store.latest_by_league + team_match
    import web.services.store as ws
    monkeypatch.setattr(ws, "latest_by_league", lambda: {})
    import web.services.team_match as tm
    monkeypatch.setattr(tm, "build_rows", lambda fs, pl: [])
    monkeypatch.setattr(tm, "jc_gap", lambda rows, top_n=10: [
        {"match": "A vs B", "gap": 0.15},
    ])

    return api.create_app()


@pytest.fixture
def client(app):
    return TestClient(app, client=("127.0.0.1", 5000))


def _login(client):
    r = client.post("/api/v1/login",
                    json={"username": "unit-test-user", "password": "unit-test-pass"})
    assert r.status_code == 200, r.text
    return r.cookies


# ────────────────────────── /api/jc/fixtures ──────────────────────────


def test_fixtures_unauthenticated_returns_401(client):
    r = client.get("/api/jc/fixtures")
    assert r.status_code in (401, 403), r.text


def test_fixtures_returns_envelope(client):
    """合法调用 → 200 + rows/sql_note/degraded。"""
    cookies = _login(client)
    r = client.get("/api/jc/fixtures", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "rows" in body
    assert "sql_note" in body
    assert body["degraded"] is False
    assert isinstance(body["rows"], list)


def test_fixtures_invalid_day_returns_400(client):
    """day 非 ISO 日期 → 400（不静默降级成空表）。

    web.errors 把 StarletteHTTPException 包装成 {code:"http_error", message: <detail>}。
    """
    cookies = _login(client)
    r = client.get("/api/jc/fixtures?day=not-a-date", cookies=cookies)
    assert r.status_code == 400, r.text
    msg = r.json()["message"]
    assert "ISO" in msg or "date" in msg.lower()


def test_fixtures_valid_day_passes_through(client):
    """day=2026-01-15 → 走 _DAY_BY 分支（fixtures_on 被调）。"""
    import store.jc_view as jcv
    seen = []
    monkeypatch_ = None  # noqa
    cookies = _login(client)
    # 直接调用，看 fixture_rows 是否返回（mock 已设 fixtures_on）
    r = client.get("/api/jc/fixtures?day=2026-01-15", cookies=cookies)
    assert r.status_code == 200, r.text
    assert len(r.json()["rows"]) >= 1


# ────────────────────────── /api/jc/issues ──────────────────────────


def test_issues_unauthenticated_returns_401(client):
    r = client.get("/api/jc/issues")
    assert r.status_code in (401, 403), r.text


def test_issues_default_limit_20(client):
    cookies = _login(client)
    r = client.get("/api/jc/issues", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "rows" in body
    assert isinstance(body["rows"], list)


def test_issues_limit_non_integer_returns_400(client):
    cookies = _login(client)
    r = client.get("/api/jc/issues?limit=abc", cookies=cookies)
    assert r.status_code == 400, r.text
    msg = r.json()["message"]
    assert "integer" in msg.lower() or "1..200" in msg


def test_issues_limit_below_1_returns_400(client):
    cookies = _login(client)
    r = client.get("/api/jc/issues?limit=0", cookies=cookies)
    assert r.status_code == 400, r.text


def test_issues_limit_above_200_returns_400(client):
    cookies = _login(client)
    r = client.get("/api/jc/issues?limit=201", cookies=cookies)
    assert r.status_code == 400, r.text


def test_issues_limit_at_boundaries_accepted(client):
    """limit=1 / limit=200 边界合法。"""
    cookies = _login(client)
    for n in (1, 200):
        r = client.get(f"/api/jc/issues?limit={n}", cookies=cookies)
        assert r.status_code == 200, f"limit={n} 应合法"


# ────────────────────────── /api/jc/gap ──────────────────────────


def test_gap_unauthenticated_returns_401(client):
    r = client.get("/api/jc/gap")
    assert r.status_code in (401, 403), r.text


def test_gap_default_top_10(client):
    cookies = _login(client)
    r = client.get("/api/jc/gap", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sport"] == "football"
    assert body["scope"] == "五大联赛"
    assert "items" in body
    assert "note" in body
    assert "count" in body


def test_gap_top_above_30_clamped(client, monkeypatch):
    """top > 30 → min(max(int(top), 1), 30) = 30。"""
    import web.services.team_match as tm
    seen_top = []
    monkeypatch.setattr(tm, "jc_gap",
                        lambda rows, top_n=10: seen_top.append(top_n) or [])
    cookies = _login(client)
    r = client.get("/api/jc/gap?top=100", cookies=cookies)
    assert r.status_code == 200, r.text
    assert seen_top == [30]


def test_gap_top_below_1_clamped(client, monkeypatch):
    """top < 1 → 钳制到 1。"""
    import web.services.team_match as tm
    seen_top = []
    monkeypatch.setattr(tm, "jc_gap",
                        lambda rows, top_n=10: seen_top.append(top_n) or [])
    cookies = _login(client)
    r = client.get("/api/jc/gap?top=0", cookies=cookies)
    assert r.status_code == 200, r.text
    assert seen_top == [1]


def test_gap_handles_load_predictions_failure(client, monkeypatch):
    """latest_by_league 抛错 → _jc_gap(rows, top_n) 仍被调（rows 已空）。"""
    import web.services.store as ws

    def boom():
        raise RuntimeError("predictions read failed")

    monkeypatch.setattr(ws, "latest_by_league", boom)
    import web.services.team_match as tm
    monkeypatch.setattr(tm, "build_rows", lambda fs, pl: [])
    monkeypatch.setattr(tm, "jc_gap", lambda rows, top_n=10: [])
    cookies = _login(client)
    r = client.get("/api/jc/gap", cookies=cookies)
    # 异常被吞（latest_by_league → pred_by_league = {}），items=[]
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


# ────────────────────────── /api/jc/movement ──────────────────────────


def test_movement_unauthenticated_returns_401(client):
    r = client.get("/api/jc/movement")
    assert r.status_code in (401, 403), r.text


def test_movement_default_latest(client):
    """day 缺省 → day="latest"。"""
    cookies = _login(client)
    r = client.get("/api/jc/movement", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sport"] == "football"
    assert body["scope"] == "五大联赛"
    assert body["day"] == "latest"
    assert isinstance(body["items"], list)


def test_movement_with_day(client):
    cookies = _login(client)
    r = client.get("/api/jc/movement?day=2026-01-15", cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["day"] == "2026-01-15"


def test_movement_filters_non_big5_leagues(client, monkeypatch):
    """league_cn 不在五大联赛白名单 → 过滤掉。

    白名单全名（web.config.BIG5）：英格兰超级联赛 / 西班牙甲级联赛 /
    意大利甲级联赛 / 德国甲级联赛 / 法国甲级联赛。
    """
    import store.jc_view as jcv
    monkeypatch.setattr(jcv, "jc_movement", lambda day=None: [
        {"match_num": 7001, "play_type": "had", "league_cn": "意大利甲级联赛",
         "home_cn": "A", "away_cn": "B", "opening_odds": {}, "closing_odds": {}},
        {"match_num": 7002, "play_type": "had", "league_cn": "英格兰冠军联赛",  # 非五大
         "home_cn": "C", "away_cn": "D", "opening_odds": {}, "closing_odds": {}},
    ])
    cookies = _login(client)
    r = client.get("/api/jc/movement", cookies=cookies)
    body = r.json()
    assert body["count"] == 1
    assert body["items"][0]["league_cn"] == "意大利甲级联赛"


# ────────────────────────── /api/jc/qr ──────────────────────────


def test_qr_unauthenticated_returns_401(client):
    r = client.get("/api/jc/qr")
    assert r.status_code in (401, 403), r.text


def test_qr_default_uses_public_base_url(client):
    """url 缺省 → PUBLIC_BASE_URL env。"""
    cookies = _login(client)
    r = client.get("/api/jc/qr", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "qr_data_url" in body
    assert body["url"] == "https://example.com/dashboard/jc/"
    # data:image/svg+xml;base64, 前缀
    assert body["qr_data_url"].startswith("data:image/svg+xml;base64,")
    # 解码验证是合法 base64
    b64_part = body["qr_data_url"].split(",", 1)[1]
    decoded = base64.b64decode(b64_part).decode("utf-8")
    assert "<svg" in decoded


def test_qr_accepts_http_url(client):
    cookies = _login(client)
    r = client.get("/api/jc/qr?url=http%3A%2F%2Fexample.com%2Fpath",
                   cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "http://example.com/path"


def test_qr_rejects_javascript_scheme(client):
    """javascript: 注入应被拒绝（避免 XSS）。"""
    cookies = _login(client)
    # url=javascript:... → 走 PUBLIC_BASE_URL fallback（不报错，但不渲染 js）
    r = client.get("/api/jc/qr?url=javascript%3Aalert(1)", cookies=cookies)
    assert r.status_code == 200, r.text
    # 应回退到 PUBLIC_BASE_URL（不嵌入 js 链接）
    assert r.json()["url"] == "https://example.com/dashboard/jc/"


def test_qr_accepts_https_with_query_string(client):
    """url 含 query string → 正确入码（unquote 后）。"""
    cookies = _login(client)
    encoded = "https%3A%2F%2Fexample.com%2Fdashboard%3Ftab%3Dfixtures"
    r = client.get(f"/api/jc/qr?url={encoded}", cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://example.com/dashboard?tab=fixtures"


# ────────────────────────── /api/jc/daily-news（R18 + R21） ──────────────────────────


def _patch_llm(monkeypatch, *, news: str, pick_reason: str):
    """mock ai.llm_client.generate 返回固定 news/pick_reason（web → ai 契约 §5 例外）。"""
    import ai.llm_client as llm
    monkeypatch.setattr(llm, "generate",
                        lambda prompt, **kw: {"news": news, "pick_reason": pick_reason})


def test_daily_news_unauthenticated_returns_401(client):
    """/api/jc/daily-news 未登录 → 401（统一鉴权守门）。注：路由在 router(prefix=/api/jc) 下。"""
    r = client.get("/api/jc/daily-news")
    assert r.status_code in (401, 403), r.text


def test_daily_news_default_sport_football(client, monkeypatch):
    """默认 sport=football：news/pick_reason 经 web.news 缓存 + 长度截断后回传。"""
    import web.services.news as news_svc
    news_svc._NEWS_CACHE.clear()
    _patch_llm(monkeypatch, news="今日多场五大联赛在售，详情见下表",
               pick_reason="7001 A vs B")
    cookies = _login(client)
    r = client.get("/api/jc/daily-news", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sport"] == "football"
    assert isinstance(body["news"], str) and body["news"]
    assert body["pick_reason"] == "7001 A vs B"
    assert len(body["news"]) <= 78
    assert len(body["pick_reason"]) <= 60


def test_daily_news_sport_basketball(client):
    """sport=basketball：直接走到占位文案（picks/context 空，LLM 调用可选）。"""
    import web.services.news as news_svc
    news_svc._NEWS_CACHE.clear()
    cookies = _login(client)
    r = client.get("/api/jc/daily-news?sport=basketball", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sport"] == "basketball"


def test_daily_news_falls_back_when_llm_returns_empty(client, monkeypatch):
    """P5：LLM 返回双空 → 经 P5 守门回退 fallback（非空体彩语境）→ news 是 fallback 内容。

    旧行为：双空 → router 占位文案「今日有多场五大联赛在售，详情见下表」。
    P5 行为：双空 → web.services.news._fallback_news 替换（label + 三行体彩角度），
    router 占位分支不再触发（news 已非空）。
    """
    import web.services.news as news_svc
    news_svc._NEWS_CACHE.clear()
    _patch_llm(monkeypatch, news="", pick_reason="")
    cookies = _login(client)
    r = client.get("/api/jc/daily-news", cookies=cookies)
    assert r.status_code == 200
    body = r.json()
    # P5 fallback 内容（label + 3 行体彩角度）
    assert "竞彩足球" in body["news"], "football 双空应走 P5 fallback（含 label）"
    assert any(kw in body["news"] for kw in news_svc._BETTING_KEYWORDS), \
        "P5 fallback 必须含体彩关键词"
    # 旧 router 占位文案不应再出现（已被 P5 守门接管）
    assert "五大联赛在售" not in body["news"], "P5 接管后 router 占位文案不再生效"


def test_daily_news_cache_hit_avoids_second_llm_call(client, monkeypatch):
    """R21 #1 验收：同一天同一 sport 第二次调 → 命中 web.news._NEWS_CACHE，LLM 不再被调。

    P5 守门：news 必须含 _BETTING_KEYWORDS 之一，否则被替换为 fallback；
    fallback 同样进缓存，第二次调用仍命中。测试用「让球」前缀保证原值不被替换。
    """
    import web.services.news as news_svc
    news_svc._NEWS_CACHE.clear()
    calls = {"n": 0}

    def gen(prompt, **kw):
        calls["n"] += 1
        return {"news": "让球深盘 n1", "pick_reason": "p1"}
    import ai.llm_client as llm
    monkeypatch.setattr(llm, "generate", gen)

    cookies = _login(client)
    r1 = client.get("/api/jc/daily-news", cookies=cookies)
    r2 = client.get("/api/jc/daily-news", cookies=cookies)
    assert r1.status_code == 200 and r2.status_code == 200
    assert calls["n"] == 1, f"同一天同 sport 第二次应命中缓存，LLM 只调 1 次；实际 {calls['n']}"
