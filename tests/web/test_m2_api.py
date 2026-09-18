"""M2 端点测试：五数据端点 require_auth 矩阵 + 结构 + 容错（验收点 B/C/D/E）。

fake fixtures 目录（tmp_path）造假预测文件，monkeypatch 后重建 store 指向它。
全部端点用 TestClient 访问；敏感值断言（E）用整串扫描。
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import web.services.store as store_mod  # noqa: E402
from web.services import datasource  # noqa: E402


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _pred_doc(league: str, generated_at: str, data_window="20260724-20260726",
              predictions=None, status="ok", **extra):
    base = {
        "generated_at": generated_at,
        "data_window": data_window,
        "status": status,
        "league": league,
        "predictions": predictions if predictions is not None else [],
    }
    base.update(extra)
    return base


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """临时 env + 临时产物目录 + 应用实例。"""
    from web.api import create_app

    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")

    import web.config as config
    import web.session_store as session_store

    config = importlib.reload(config)
    session_store = importlib.reload(session_store)
    import web.auth as auth
    auth = importlib.reload(auth)

    # store 指向临时目录
    scripts = tmp_path / "scripts"
    monkeypatch.setattr(store_mod, "OUTPUT_DIR", scripts)
    # 重置模块级单例（若存在），确保每个测试全新扫描
    monkeypatch.setattr(store_mod, "iter_prediction_files", store_mod.iter_prediction_files)

    return create_app()


@pytest.fixture()
def client(app):
    return TestClient(app)


def _login(client):
    res = client.post("/api/v1/login", json={
        "username": "unit-test-user", "password": "unit-test-pass"})
    assert res.status_code == 200, res.text
    return res


def _seed_fixture_data(tmp_path):
    """常规 fixture：两联赛各有 today 窗口的预测（today 为 BJT 当天）。"""
    scripts = tmp_path / "scripts"
    today = datetime.now(timezone(timedelta(hours=8))).date()
    win = f"{today:%Y%m%d}-{today:%Y%m%d}"
    pdir = scripts / "predictions"
    _write_json(pdir / "prediction_test1.json", _pred_doc(
        "epl", f"{today.isoformat()}T10:00:00+08:00", data_window=win,
        predictions=[
            {"match": "Arsenal vs Chelsea", "home": "Arsenal", "away": "Chelsea",
             "direction": "Arsenal 胜", "stars": "2-star",
             "confidence_score": 0.65, "predicted_score": "2-1",
             "over_under": "Over 2.5", "btts": "BTTS Yes", "kickoff_utc": "2026-07-26T15:00:00Z"},
        ],
        monte_carlo={"champion_probs": {"Arsenal": 0.3, "Chelsea": 0.2},
                     "round_reach_probs": {"Arsenal": {"QF": 0.8}}, "simulation_count": 10000},
        accuracy_summary={
            "7d": {"window_days": 7, "reconciled": 10, "direction_accuracy": 0.6,
                   "score_accuracy": 0.3, "over_under_accuracy": 0.5},
            "30d": {"window_days": 30, "reconciled": 40, "direction_accuracy": 0.55,
                    "score_accuracy": 0.28, "over_under_accuracy": 0.48}},
    ))
    _write_json(pdir / "prediction_test2.json", _pred_doc(
        "laliga", f"{today.isoformat()}T11:00:00+08:00", data_window=win,
        predictions=[
            {"match": "Real Madrid vs Barcelona", "home": "Real Madrid", "away": "Barcelona",
             "direction": "Real Madrid 胜", "stars": "3-star",
             "confidence_score": 0.72, "predicted_score": "3-1",
             "over_under": "Over 2.5", "btts": "BTTS Yes", "kickoff_utc": "2026-07-26T18:00:00Z"},
        ],
    ))
    return scripts


# --- 验收点 B：未登录 5 端点全 401 ------------------------------------------

def test_unauth_401_matrix(client):
    endpoints = [
        "/api/v1/predictions/today",
        "/api/v1/predictions/2026-07-26",
        "/api/v1/championship",
        "/api/v1/accuracy",
        "/api/v1/accuracy/breakdown",
        "/api/v1/history",
    ]
    for ep in endpoints:
        res = client.get(ep)
        assert res.status_code == 401, f"{ep} → {res.status_code}"
        body = res.json()
        assert body["code"] == "unauthorized"


# --- 验收点 C：today 按联赛分组 + 字段对齐契约 §2 --------------------------

def test_today_grouped_by_league(client, tmp_path):
    _seed_fixture_data(tmp_path)
    _login(client)
    res = client.get("/api/v1/predictions/today")
    assert res.status_code == 200
    body = res.json()
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    assert body["date"] == today
    leagues = body["leagues"]
    # 全部联赛 key 存在（前端契约），fixture 联赛有内容
    for lk in datasource.LEAGUES:
        assert lk in leagues
    assert len(leagues["epl"]) == 1
    p = leagues["epl"][0]
    for field in ("match", "home", "away", "direction", "stars",
                  "confidence_score", "predicted_score", "over_under", "btts"):
        assert field in p
    assert leagues["laliga"][0]["home"] == "Real Madrid"
    # 无数据联赛为空列表
    assert leagues["bundesliga"] == []


def test_today_excludes_other_window(client, tmp_path):
    """data_window 不含今天的联赛不出现在 results 中（空列表）但 key 在。"""
    scripts = tmp_path / "scripts"
    pdir = scripts / "predictions"
    other_win = "20260801-20260803"
    _write_json(pdir / "prediction_fut.json", _pred_doc(
        "seriea", "2026-08-02T10:00:00+08:00", data_window=other_win,
        predictions=[{"match": "Juventus vs Inter"}]))
    _login(client)
    res = client.get("/api/v1/predictions/today")
    body = res.json()
    assert body["leagues"]["seriea"] == []


def test_predictions_by_date_valid(client, tmp_path):
    _seed_fixture_data(tmp_path)
    _login(client)
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    res = client.get(f"/api/v1/predictions/{today}")
    assert res.status_code == 200
    assert res.json()["date"] == today


def test_predictions_by_date_invalid_400(client):
    _login(client)
    res = client.get("/api/v1/predictions/not-a-date")
    assert res.status_code == 400
    assert res.json()["code"] == "invalid_date"


# --- 验收点 D：空目录/坏 JSON 200 + 空态 -----------------------------------

def test_empty_dir_200_empty(client, tmp_path):
    _login(client)
    res = client.get("/api/v1/predictions/today")
    assert res.status_code == 200
    assert all(v == [] for v in res.json()["leagues"].values())
    assert client.get("/api/v1/championship").status_code == 200
    assert client.get("/api/v1/accuracy").status_code == 200
    assert client.get("/api/v1/history").status_code == 200


def test_bad_json_200_empty(client, tmp_path):
    scripts = tmp_path / "scripts"
    pdir = scripts / "predictions"
    pdir.mkdir(parents=True)
    (pdir / "prediction_bad.json").write_text("{oops", encoding="utf-8")
    _login(client)
    for ep in ("/api/v1/predictions/today", "/api/v1/championship",
               "/api/v1/accuracy", "/api/v1/accuracy/breakdown", "/api/v1/history"):
        res = client.get(ep)
        assert res.status_code == 200, ep
        assert "leagues" in res.json()


# --- 验收点 E：响应无敏感值 --------------------------------------------------

def test_no_secrets_in_responses(client, tmp_path):
    _seed_fixture_data(tmp_path)
    # fixture 产物中可以放诱饵键，断言响应不回传
    scripts = tmp_path / "scripts"
    pdir = scripts / "predictions"
    _write_json(pdir / "prediction_secret.json", _pred_doc(
        "ligue1", "2026-08-02T10:00:00+08:00",
        predictions=[{"match": "PSG vs Lyon"}],
        api_key_leak="super-secret-token"))
    _login(client)
    endpoints = ["/api/v1/predictions/today", "/api/v1/championship",
                 "/api/v1/accuracy", "/api/v1/accuracy/breakdown", "/api/v1/history", "/api/v1/sources/status"]
    for ep in endpoints:
        res = client.get(ep)
        assert res.status_code == 200
        raw = res.text
        assert "super-secret-token" not in raw, ep
    # sources 状态不回传 env 值
    res = client.get("/api/v1/sources/status")
    body = res.json()
    assert set(body["configured"]) == {"football-data", "api-football", "espn"}
    assert "default_source" in body
    # 任何值字段都不存在
    raw = res.text
    for key in ("FOOTBALL_DATA_API_KEY", "API_FOOTBALL_KEY"):
        assert key not in raw


def test_sources_status_structure(client):
    _login(client)
    res = client.get("/api/v1/sources/status")
    assert res.status_code == 200
    body = res.json()
    assert body["configured"] == ["football-data", "api-football", "espn"]
    assert body["enabled"]["espn"] is True  # 无 key 需求
    assert isinstance(body["enabled"]["football-data"], bool)
    assert body["leagues"]["epl"]["data_source"] == "football-data"


# --- championship / accuracy 结构 ------------------------------------------

def test_championship_merges_monte_carlo(client, tmp_path):
    _seed_fixture_data(tmp_path)
    _login(client)
    res = client.get("/api/v1/championship")
    assert res.status_code == 200
    body = res.json()
    epl = body["leagues"]["epl"]
    assert epl["champion_probs"]["Arsenal"] == 0.3
    assert epl["simulation_count"] == 10000
    assert "laliga" not in body["leagues"]  # 无 monte_carlo → 不出现


def test_accuracy_merges_summary(client, tmp_path):
    _seed_fixture_data(tmp_path)
    _login(client)
    res = client.get("/api/v1/accuracy")
    assert res.status_code == 200
    body = res.json()
    epl = body["leagues"]["epl"]
    assert epl["7d"]["direction_accuracy"] == 0.6
    assert epl["30d"]["score_accuracy"] == 0.28


def test_accuracy_includes_pending_settlement_counts(client, tmp_path):
    """核查 P0-1：accuracy 恒空时页面要能提示「N 场待结算」。

    fixture 里两联赛各 1 场预测、无对应赛果 → pending 各 1。
    """
    _seed_fixture_data(tmp_path)
    _login(client)
    res = client.get("/api/v1/accuracy")
    assert res.status_code == 200
    body = res.json()
    assert body["pending"] == {"epl": 1, "laliga": 1}


def test_accuracy_breakdown_projects_source_metrics_without_ci(client, tmp_path):
    _seed_fixture_data(tmp_path)
    _login(client)
    res = client.get("/api/v1/accuracy/breakdown")
    assert res.status_code == 200
    epl = res.json()["leagues"]["epl"]
    assert epl["7d"] == {
        "direction_accuracy": 0.6,
        "score_accuracy": 0.3,
        "over_under_accuracy": 0.5,
        "reconciled": 10,
    }
    assert epl["30d"]["reconciled"] == 40
    assert "confidence_interval" not in str(epl)
    assert "sample_count" not in epl["7d"]


def test_accuracy_breakdown_includes_explicit_sample_count_only(client, tmp_path):
    _seed_fixture_data(tmp_path)
    p = tmp_path / "scripts" / "predictions" / "prediction_test1.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["accuracy_summary"]["7d"]["sample_count"] = 10
    _write_json(p, data)
    _login(client)
    body = client.get("/api/v1/accuracy/breakdown").json()
    assert body["leagues"]["epl"]["7d"]["sample_count"] == 10


def test_history_lists_files(client, tmp_path):
    _seed_fixture_data(tmp_path)
    _login(client)
    res = client.get("/api/v1/history")
    assert res.status_code == 200
    body = res.json()
    epl = body["leagues"]["epl"]
    assert len(epl) >= 1
    assert "generated_at" in epl[0]
    assert "name" in epl[0]