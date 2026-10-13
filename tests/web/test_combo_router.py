"""web/routers/combo 端到端测试：/api/v1/combo/eval 鉴权 + 装配 + 评估。

背景：web/routers/combo.py 当前 11% 覆盖 — 路由层（_assemble_legs /
teams_match_safe / combo_eval 端点）完全无单测；test_combo.py 只测了
纯函数 web.services.combo.evaluate_combo。

覆盖点：
  · _assemble_legs：fixture 命中取 odds + 预测命中取 p_model + match_num
    反查 + fuzzy match 退化 + 缺 fixtures 降级
  · teams_match_safe：fuzzy match 命中 / 异常退化字符串相等
  · combo_eval 端点：
    - legs 模式 200 / >32 → 400
    - selections 模式 200 / >32 → 400 / 缺 legs+selections → 400
    - joint_prob 范围校验 [0,1] / stake 默认 2.0

鉴权：TestClient(client=("127.0.0.1", 5000)) 模拟 nginx + Basic Auth；
依赖 web.auth 单账号（auth_users fixture from test_m1_auth）。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """轻量 app fixture：env + reload config；不依赖 jobs/quota（combo 路由不读）。"""
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")
    # 注：不 setenv TRUSTED_PROXY_IPS，保持 web.config 默认 tuple（含 127.0.0.1 + ::1）
    # 防止污染后续 test_client_ip.py::test_trusted_proxy_ips_default_includes_loopback

    import web.config as config
    import web.session_store as session_store
    config = importlib.reload(config)
    session_store = importlib.reload(session_store)
    import web.auth as auth_mod
    importlib.reload(auth_mod)

    from web.api import create_app
    return create_app()


@pytest.fixture
def client(app):
    """TestClient(client=("127.0.0.1", 5000)) 模拟 nginx 反代直连。"""
    return TestClient(app, client=("127.0.0.1", 5000))


def _login(client):
    """JSON body 登录拿 session cookie（web.auth.login 走 body，不走 Basic Auth）。"""
    r = client.post("/api/v1/login",
                    json={"username": "unit-test-user", "password": "unit-test-pass"})
    assert r.status_code == 200, r.text
    return r.cookies


def _fake_fixtures(monkeypatch, rows):
    """mock jc_view.fixtures_on(None) 返回指定 rows。"""
    from web.routers import combo as combo_mod
    monkeypatch.setattr(combo_mod, "_assemble_legs",
                        lambda sels: _build_legs_from_fixtures(sels, rows))


def _build_legs_from_fixtures(sels, fixtures):
    """仿 _assemble_legs 逻辑（不依赖 store），从 fixtures 配 odds/p_model。"""
    fix_by_pair = {}
    for f in fixtures:
        h = (f.get("home_cn") or "").strip()
        a = (f.get("away_cn") or "").strip()
        if not h or not a:
            continue
        m = fix_by_pair.setdefault((h, a), {"home_cn": h, "away_cn": a,
                                            "plays": {}})
        pt = f.get("play_type")
        if pt and pt not in m["plays"]:
            m["plays"][pt] = f.get("options") or {}

    side_key = {"主胜": "h", "平": "d", "客胜": "a", "home": "h", "draw": "d", "away": "a"}

    out = []
    for sel in sels:
        side = str(sel.get("side") or "")
        home = sel.get("home") or ""
        away = sel.get("away") or ""
        if (not home or not away) and sel.get("match_num") is not None:
            for f in fixtures:
                if str(f.get("match_num")) == str(sel.get("match_num")):
                    home = home or f.get("home_cn") or ""
                    away = away or f.get("away_cn") or ""
                    break
        key = side_key.get(side, side.lower() if isinstance(side, str) else "")
        m = fix_by_pair.get((home, away))
        odds = 0.0
        if m:
            opts = (m["plays"] or {}).get("had") or (m["plays"] or {}).get("hhad") or {}
            try:
                odds = float(opts.get(key) or 0.0)
            except (TypeError, ValueError):
                odds = 0.0
        p_model = 0.0
        if m and (m.get("home_cn") or m.get("away_cn")):
            for e in (sel.get("_preds") or []):
                rf = e.get("reasoning_factors") or {}
                p_field = {"h": "home_ml_true_prob", "d": "draw_true_prob",
                           "a": "away_ml_true_prob"}.get(key)
                if p_field:
                    try:
                        p_model = float(rf.get(p_field) or 0.0)
                    except (TypeError, ValueError):
                        p_model = 0.0
                    break
        out.append({"side": side, "odds": odds, "p_model": p_model})
    return out


# ────────────────────────── _assemble_legs 单元 ──────────────────────────


def test_assemble_legs_picks_odds_and_p_model_from_fixtures(monkeypatch):
    """fixture 命中 had + 预测命中 → odds/p_model 都填上。"""
    from web.routers import combo as combo_mod

    fake_fixtures = [
        {"match_num": 7001, "home_cn": "A", "away_cn": "B", "play_type": "had",
         "options": {"h": "1.8", "d": "3.4", "a": "4.5"}},
    ]
    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: fake_fixtures)
    from web.services import store as ws
    monkeypatch.setattr(ws, "load_prediction_docs", lambda: [
        {"data": {"predictions": [
            {"home": "A", "away": "B",
             "reasoning_factors": {"home_ml_true_prob": 0.55,
                                   "draw_true_prob": 0.25,
                                   "away_ml_true_prob": 0.20}}
        ]}}
    ])

    legs = combo_mod._assemble_legs([
        {"home": "A", "away": "B", "side": "主胜"},
    ])
    assert len(legs) == 1
    assert legs[0]["odds"] == 1.8
    assert legs[0]["p_model"] == 0.55


def test_assemble_legs_match_num_reverse_lookup(monkeypatch):
    """无 home/away + 有 match_num → 反查 fixture。"""
    from web.routers import combo as combo_mod

    fake_fixtures = [
        {"match_num": 7001, "home_cn": "利物浦", "away_cn": "曼联", "play_type": "had",
         "options": {"h": "2.1", "d": "3.3", "a": "3.4"}},
    ]
    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: fake_fixtures)
    from web.services import store as ws
    monkeypatch.setattr(ws, "load_prediction_docs", lambda: [])

    legs = combo_mod._assemble_legs([{"match_num": 7001, "side": "主胜"}])
    assert legs[0]["odds"] == 2.1


def test_assemble_legs_fixtures_failure_returns_empty_odds(monkeypatch):
    """jc_view.fixtures_on 抛错 → 不崩，每腿 odds=0/p_model=0。"""
    from web.routers import combo as combo_mod

    def boom(d=None):
        raise RuntimeError("ro db down")

    monkeypatch.setattr("store.jc_view.fixtures_on", boom)
    from web.services import store as ws
    monkeypatch.setattr(ws, "load_prediction_docs", lambda: [])

    legs = combo_mod._assemble_legs([{"home": "X", "away": "Y", "side": "主胜"}])
    assert legs[0]["odds"] == 0.0
    assert legs[0]["p_model"] == 0.0


def test_assemble_legs_load_predictions_failure(monkeypatch):
    """load_prediction_docs 抛错 → 不崩，p_model=0 但 odds 正常取。"""
    from web.routers import combo as combo_mod

    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [
        {"home_cn": "A", "away_cn": "B", "play_type": "had",
         "options": {"h": "1.5", "d": "4.0", "a": "6.0"}},
    ])
    from web.services import store as ws

    def boom():
        raise RuntimeError("predictions read failed")

    monkeypatch.setattr(ws, "load_prediction_docs", boom)

    legs = combo_mod._assemble_legs([{"home": "A", "away": "B", "side": "主胜"}])
    assert legs[0]["odds"] == 1.5
    assert legs[0]["p_model"] == 0.0


def test_assemble_legs_unknown_side_yields_zero(monkeypatch):
    """side 不在映射表（h/d/a）→ key="" → opts.get("")=0。"""
    from web.routers import combo as combo_mod

    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [
        {"home_cn": "A", "away_cn": "B", "play_type": "had",
         "options": {"h": "2.0", "d": "3.0", "a": "4.0"}},
    ])
    from web.services import store as ws
    monkeypatch.setattr(ws, "load_prediction_docs", lambda: [])

    legs = combo_mod._assemble_legs([{"home": "A", "away": "B", "side": "无效"}])
    assert legs[0]["odds"] == 0.0


def test_assemble_legs_hhad_fallback_when_had_missing(monkeypatch):
    """had 缺赔率时回退 hhad 取数。"""
    from web.routers import combo as combo_mod

    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [
        {"home_cn": "A", "away_cn": "B", "play_type": "hhad",
         "options": {"h": "1.95", "d": "3.5", "a": "3.8"}},
    ])
    from web.services import store as ws
    monkeypatch.setattr(ws, "load_prediction_docs", lambda: [])

    legs = combo_mod._assemble_legs([{"home": "A", "away": "B", "side": "主胜"}])
    assert legs[0]["odds"] == 1.95


# ────────────────────────── teams_match_safe 单元 ──────────────────────────


def test_teams_match_safe_delegates_to_team_match(monkeypatch):
    """正常路径：复用 web.services.team_match.teams_match。"""
    from web.routers import combo as combo_mod
    from web.services import team_match as tm
    monkeypatch.setattr(tm, "teams_match", lambda h1, a1, h2, a2: True)
    assert combo_mod.teams_match_safe("A", "B", "C", "D") is True


def test_teams_match_safe_falls_back_to_string_eq_on_exception(monkeypatch):
    """teams_match 抛错 → 退化为字符串相等比较。"""
    from web.routers import combo as combo_mod
    from web.services import team_match as tm

    def boom(*a, **kw):
        raise RuntimeError("match logic failed")

    monkeypatch.setattr(tm, "teams_match", boom)
    # 完全相等 → True
    assert combo_mod.teams_match_safe("A", "B", "A", "B") is True
    # 不等 → False
    assert combo_mod.teams_match_safe("A", "B", "C", "D") is False


# ────────────────────────── combo_eval 端点 ──────────────────────────


def test_combo_eval_unauthenticated_returns_401_or_redirect(client):
    """未登录 → 401（require_auth 守护）。"""
    r = client.post("/api/v1/combo/eval", json={"legs": []})
    assert r.status_code in (401, 403), r.text


def test_combo_eval_legs_mode_200(client):
    """legs 模式合法 → 200，body 包含 parlay_ev/joint_prob/legs 等。"""
    cookies = _login(client)
    r = client.post("/api/v1/combo/eval",
                    json={"legs": [{"side": "主胜", "odds": 1.8, "p_model": 0.55}]},
                    cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "parlay_ev" in body
    assert "joint_prob" in body
    assert "legs" in body
    assert body["n_legs"] == 1


def test_combo_eval_legs_too_many_returns_400(client):
    """legs > 32 → 400（防组合爆炸）。"""
    cookies = _login(client)
    legs = [{"side": "主胜", "odds": 1.5, "p_model": 0.5}] * 33
    r = client.post("/api/v1/combo/eval", json={"legs": legs}, cookies=cookies)
    assert r.status_code == 400, r.text
    assert "最多 32" in r.json()["message"]


def test_combo_eval_missing_both_returns_400(client):
    """legs + selections 都缺 → 400。"""
    cookies = _login(client)
    r = client.post("/api/v1/combo/eval", json={}, cookies=cookies)
    assert r.status_code == 400, r.text


def test_combo_eval_selections_too_many_returns_400(client, monkeypatch):
    """selections > 32 → 400。"""
    from web.routers import combo as combo_mod
    monkeypatch.setattr(combo_mod, "_assemble_legs", lambda s: [{"side": "主胜", "odds": 1.5, "p_model": 0.5}] * len(s))
    cookies = _login(client)
    sels = [{"match_num": i, "side": "主胜"} for i in range(33)]
    r = client.post("/api/v1/combo/eval",
                    json={"selections": sels}, cookies=cookies)
    assert r.status_code == 400, r.text


def test_combo_eval_selections_mode_200(client, monkeypatch):
    """selections 模式合法 → 200。"""
    from web.routers import combo as combo_mod
    monkeypatch.setattr(combo_mod, "_assemble_legs",
                        lambda s: [{"side": "主胜", "odds": 1.8, "p_model": 0.55}] * len(s))
    cookies = _login(client)
    r = client.post("/api/v1/combo/eval",
                    json={"selections": [{"match_num": 7001, "side": "主胜"},
                                          {"match_num": 7002, "side": "客胜"}]},
                    cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["n_legs"] == 2


def test_combo_eval_joint_prob_out_of_range_returns_400(client):
    """joint_prob > 1 → 400。"""
    cookies = _login(client)
    r = client.post("/api/v1/combo/eval",
                    json={"legs": [{"side": "主胜", "odds": 1.8, "p_model": 0.5}],
                          "joint_prob": 1.5}, cookies=cookies)
    assert r.status_code == 400, r.text
    assert "[0, 1]" in r.json()["message"]


def test_combo_eval_joint_prob_negative_returns_400(client):
    """joint_prob < 0 → 400。"""
    cookies = _login(client)
    r = client.post("/api/v1/combo/eval",
                    json={"legs": [{"side": "主胜", "odds": 1.8, "p_model": 0.5}],
                          "joint_prob": -0.1}, cookies=cookies)
    assert r.status_code == 400, r.text


def test_combo_eval_joint_prob_non_numeric_returns_400(client):
    """joint_prob 非数字 → 400。"""
    cookies = _login(client)
    r = client.post("/api/v1/combo/eval",
                    json={"legs": [{"side": "主胜", "odds": 1.8, "p_model": 0.5}],
                          "joint_prob": "not a number"}, cookies=cookies)
    assert r.status_code == 400, r.text


def test_combo_eval_stake_zero_falls_back_to_default(client):
    """stake <= 0 → 回退 stake=2.0。"""
    cookies = _login(client)
    r = client.post("/api/v1/combo/eval",
                    json={"legs": [{"side": "主胜", "odds": 1.8, "p_model": 0.55}],
                          "stake": 0}, cookies=cookies)
    assert r.status_code == 200, r.text
    # 纯函数 evaluate_combo 返回 stake 默认 2.0
    assert r.json()["stake"] == 2.0


def test_combo_eval_stake_string_cast(client):
    """stake 是 str 数字 → 仍接受（前端可能传字符串）。"""
    cookies = _login(client)
    r = client.post("/api/v1/combo/eval",
                    json={"legs": [{"side": "主胜", "odds": 1.8, "p_model": 0.55}],
                          "stake": "5"}, cookies=cookies)
    assert r.status_code == 200, r.text


def test_combo_eval_stake_non_numeric_falls_back(client):
    """stake 非数字 → 回退 stake=2.0。"""
    cookies = _login(client)
    r = client.post("/api/v1/combo/eval",
                    json={"legs": [{"side": "主胜", "odds": 1.8, "p_model": 0.55}],
                          "stake": "abc"}, cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["stake"] == 2.0
