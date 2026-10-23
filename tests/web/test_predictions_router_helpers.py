"""R24 #1: web/routers/predictions.py helpers + 2 业务流端点专项测试。

predictions.py 8 端点中 6 个已有完整业务流测（test_m2_api.py 13 个），
但 helpers（_validated_model_probs / _market_projection / _prediction_summary
/ _run_metadata / _bball_extras / _group_for_day / _league_names）**0 直接单测**，
仅靠集成测间接覆盖。/prediction-metadata + /calibration 也只有 R23 #2 401
守门，缺业务流 happy path。

本轮补 7 helpers + 2 端点业务流。
"""
from __future__ import annotations

import importlib
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ──────────────────────── 7 helpers 直接 unit 测 ────────────────────────


def test_validated_model_probs_list_of_three():
    """R24 #1：list/tuple of 3 floats → 归一化为 home/draw/away dict。

    P0（2026-10-23）：``ml_proba`` **不再**被接受 —— 它是 ML 分类器原始输出
    （未与规则模型融合），不是「模型概率」；塌成 [0.978,0.008,0.014] 时算出的
    edge/EV 全是假账。唯一真源是 ``probs``（融合后最终值）。
    """
    from web.routers.predictions import _validated_model_probs
    # 标准 1x2 列表输入
    out = _validated_model_probs({"model_probs": [0.5, 0.3, 0.2]})
    assert out == {"home": 0.5, "draw": 0.3, "away": 0.2}
    # tuple 输入同样接受
    out = _validated_model_probs({"model_probs": (0.6, 0.25, 0.15)})
    assert out == {"home": 0.6, "draw": 0.25, "away": 0.15}
    # 真源：probs（h/d/a 键）
    out = _validated_model_probs({"probs": {"h": 0.6, "d": 0.25, "a": 0.15}})
    assert out == {"home": 0.6, "draw": 0.25, "away": 0.15}
    # ml_proba（分类器原始输出）→ 必须拒绝
    assert _validated_model_probs({"ml_proba": (0.6, 0.25, 0.15)}) is None


def test_validated_model_probs_rejects_wrong_length_list():
    """R24 #1：list 长度 ≠ 3 → None。"""
    from web.routers.predictions import _validated_model_probs
    assert _validated_model_probs({"model_probs": [0.5, 0.3]}) is None
    assert _validated_model_probs({"model_probs": [0.5, 0.3, 0.1, 0.1]}) is None


def test_validated_model_probs_rejects_dict_with_wrong_keys():
    """R24 #1：dict 缺键 / 多键 → None。"""
    from web.routers.predictions import _validated_model_probs
    # 缺 away
    assert _validated_model_probs({"model_probs": {"home": 0.5, "draw": 0.3}}) is None
    # 多键
    assert _validated_model_probs({"model_probs": {
        "home": 0.5, "draw": 0.3, "away": 0.2, "extra": 0.0}}) is None
    # 错误键名
    assert _validated_model_probs({"model_probs": {
        "h": 0.5, "d": 0.3, "a": 0.2}}) is None


def test_validated_model_probs_rejects_out_of_range_or_nan():
    """R24 #1：值非 finite 或不在 [0, 1] → None。"""
    from web.routers.predictions import _validated_model_probs
    # 负值
    assert _validated_model_probs({"model_probs": {"home": -0.1, "draw": 0.5, "away": 0.6}}) is None
    # > 1
    assert _validated_model_probs({"model_probs": {"home": 1.5, "draw": 0.0, "away": -0.5}}) is None
    # NaN
    assert _validated_model_probs({"model_probs": {
        "home": float("nan"), "draw": 0.5, "away": 0.5}}) is None
    # Inf
    assert _validated_model_probs({"model_probs": {
        "home": float("inf"), "draw": 0.5, "away": 0.5}}) is None
    # 字符串不可 coerce → None（模型侧惯例：model_probs 应是数值）
    assert _validated_model_probs({"model_probs": {
        "home": "abc", "draw": 0.3, "away": 0.2}}) is None


def test_validated_model_probs_rejects_not_normalized():
    """P0（2026-10-23）：容差从 1e-6 放宽到 **1e-3**。

    融合后概率写入预测文件时按 **4 位小数**四舍五入（predictor._assemble_result），
    三项求和的舍入误差上界 ≈ 1.5e-4 ⇒ 旧容差 1e-6 把 **99/162（61%）** 的真实场次
    误判为「非法向量」⇒ edge_prob / ev_per_unit 整体缺失、卡片静默不显示
    「比庄家高/低 X 个点」（用户看不出缺了什么）。
    """
    from web.routers.predictions import _validated_model_probs
    # 和 1.0 偏差 0.1 ⇒ 越界 → None
    assert _validated_model_probs({"model_probs": {
        "home": 0.5, "draw": 0.3, "away": 0.1}}) is None
    # 和 1.0 偏差 1e-4（4 位小数四舍五入的正常误差）→ 接受
    out = _validated_model_probs({"model_probs": {
        "home": 0.5, "draw": 0.3, "away": 0.2001}})
    assert out is not None
    assert abs(out["home"] + out["draw"] + out["away"] - 1.0) <= 1e-3


def test_market_projection_missing_when_no_market():
    """R24 #1：缺 market 字段 → status=missing。"""
    from web.routers.predictions import _market_projection
    assert _market_projection({}) == {"status": "missing"}
    assert _market_projection({"market": "not a dict"}) == {"status": "missing"}


def test_market_projection_partial_when_keys_incomplete():
    """R24 #1：market 缺 source/captured_at/market_type/selections → status=partial。"""
    from web.routers.predictions import _market_projection
    # 缺 source
    out = _market_projection({"market": {"captured_at": "x", "market_type": "1x2",
                                          "selections": {"home": {}}}})
    assert out["status"] == "partial"
    # selections 非 dict
    out = _market_projection({"market": {"source": "odds_api", "captured_at": "x",
                                          "market_type": "1x2", "selections": "bad"}})
    assert out["status"] == "partial"


def test_market_projection_complete_1x2_with_model_edge():
    """R24 #1：完整 1x2 + model_probs → status=complete + edge_prob / ev_per_unit。"""
    from web.routers.predictions import _market_projection
    doc = {
        "model_probs": [0.55, 0.25, 0.20],   # 模型概率
        "market": {
            "source": "odds_api",
            "captured_at": "2026-10-15T10:00:00Z",
            "market_type": "1x2",
            "odds_format": "decimal",
            "selections": {
                "home": 1.80,   # decimal odds
                "draw": 3.40,
                "away": 4.50,
            },
        },
    }
    out = _market_projection(doc)
    assert out["status"] == "complete"
    # 1/1.8 + 1/3.4 + 1/4.5 ≈ 0.5556 + 0.2941 + 0.2222 = 1.0719 → margin ≈ 0.0719
    assert abs(out["margin"] - 0.0719) < 1e-3
    # devig_probs 归一化后模型：home=0.5556/1.0719 ≈ 0.518
    assert abs(out["devig_probs"]["home"] - 0.518) < 1e-2
    # model_probs 已 normalize
    assert out["model_probs"] == {"home": 0.55, "draw": 0.25, "away": 0.20}
    # edge_prob = model - market
    assert out["edge_prob"]["home"] == pytest.approx(0.55 - out["devig_probs"]["home"], abs=1e-6)
    # ev_per_unit = model * odds - 1
    assert out["ev_per_unit"]["home"] == pytest.approx(0.55 * 1.80 - 1.0)


def test_market_projection_complete_rejects_odds_le_1():
    """R24 #1：odds ≤ 1 视为非法 → status=partial（不会算 devig）。"""
    from web.routers.predictions import _market_projection
    doc = {
        "market": {
            "source": "x", "captured_at": "x", "market_type": "1x2",
            "selections": {"home": 0.5, "draw": 3.0, "away": 4.0},  # home odds=0.5 非法
        },
    }
    out = _market_projection(doc)
    assert out["status"] == "partial", f"odds ≤ 1 应拒绝 complete；实际 {out}"


def test_prediction_summary_extracts_known_fields():
    """R24 #1：契约 §2.2 字段透传 + bball extras。"""
    from web.routers.predictions import _prediction_summary
    doc = {
        "match": "A vs B",
        "home": "A", "away": "B",
        "direction": "主胜", "stars": "2-star",
        "confidence_score": 0.6, "predicted_score": "2-0",
        "over_under": "Over 2.5", "btts": "Yes",
        "kickoff_utc": "2026-10-15T12:00:00Z", "data_window": "20261001-20261014",
        "spread_pred": "Lakers -3.5",     # bball
        "total_pred": "210.5",
        "win_prob": "0.65",
    }
    out = _prediction_summary(doc, day=date(2026, 10, 15))
    # 必传字段
    assert out["match"] == "A vs B"
    assert out["home"] == "A" and out["away"] == "B"
    assert out["direction"] == "主胜"
    assert out["confidence_score"] == 0.6
    # bball extras 出现
    assert out["spread_pred"] == "Lakers -3.5"
    assert out["sport"] == "basketball"


def test_prediction_summary_no_bball_extras_when_missing():
    """R24 #1：缺 spread_pred/total_pred/win_prob → 无 bball_extras（sport 不应是 basketball）。"""
    from web.routers.predictions import _prediction_summary
    doc = {"home": "A", "away": "B"}
    out = _prediction_summary(doc, day=date(2026, 10, 15))
    assert "sport" not in out, "无 bball 字段时不应有 sport=basketball"
    assert "spread_pred" not in out


def test_run_metadata_extracts_provenance():
    """R24 #1：契约字段 + n_predictions 数 predictions。"""
    from web.routers.predictions import _run_metadata
    doc = {
        "name": "prediction_2026-10-15_10_epl.json",
        "data": {
            "generated_at": "2026-10-15T10:00:00+08:00",
            "data_window": "20261001-20261014",
            "status": "ok",
            "data_source": "espn",
            "tournament_type": "league",
            "dixon_coles_enabled": True,
            "dixon_coles_rho": 0.12,
            "predictions": [{"x": 1}, {"x": 2}, {"x": 3}],
        },
    }
    out = _run_metadata("epl", doc)
    assert out["league"] == "epl"
    assert out["file"] == "prediction_2026-10-15_10_epl.json"
    assert out["n_predictions"] == 3
    assert out["dixon_coles_rho"] == 0.12
    assert out["dixon_coles_enabled"] is True


def test_run_metadata_handles_missing_predictions_field():
    """R24 #1：data 缺 predictions 字段 → n_predictions=0。"""
    from web.routers.predictions import _run_metadata
    doc = {"name": "x.json", "data": {"generated_at": "y"}}
    out = _run_metadata("epl", doc)
    assert out["n_predictions"] == 0
    assert out["generated_at"] == "y"


def test_group_for_day_filters_by_window(monkeypatch):
    """R24 #1：covers_date 不通过的 doc → 该 doc 整组过滤；_league_names 之外的 league 被收口。"""
    from web.routers.predictions import _group_for_day, _league_names
    from web.services import store as ws

    # 准备 fixtures
    epl_doc = {
        "data": {
            "data_window": "20261010-20261020",
            "predictions": [{"home": "A", "away": "B"}, {"home": "C", "away": "D"}],
        }
    }
    laliga_doc = {
        "data": {
            "data_window": "20260101-20260105",  # 不覆盖目标日
            "predictions": [{"home": "X", "away": "Y"}],
        }
    }
    # 一份不在 _league_names 白名单的 league doc；应被 _group_for_day 收口丢弃
    extra_doc = {
        "data": {
            "data_window": "20261010-20261020",
            "predictions": [{"home": "Z1", "away": "Z2"}],
        }
    }
    monkey_target = date(2026, 10, 15)
    # covers_date(monkey_target): epl=10-10~10-20 包含 10-15 -> True; laliga 不含
    # ⚠️ **P1（2026-10-23）**：必须用 `monkeypatch.setattr` —— 直接 `ws.covers_date = ...`
    #    会让替身**在整个 pytest session 里一直生效**（不走 monkeypatch 就没有还原）⇒
    #    后续所有用到 `store.covers_date` / `store.latest_by_league` 的用例测的都是这个
    #    lambda、不是生产代码（**假阴性源**）；默认字母序下受害者先跑 ⇒ 污染完全隐形，
    #    换成逆序/乱序就冒出 **15 个 order-dependent 失败**。
    monkeypatch.setattr(ws, "covers_date",
                        lambda data, day: "20261010" in data.get("data_window", ""))
    monkeypatch.setattr(ws, "latest_by_league", lambda: {
        "epl": epl_doc, "laliga": laliga_doc, "extra_league": extra_doc,
    })

    out = _group_for_day(monkey_target)
    # epl 命中 window -> 2 场；laliga 不命中 -> 空
    assert len(out["epl"]) == 2
    assert out["laliga"] == []
    # 验证 _group_for_day 收口到 _league_names 白名单
    assert set(out) == set(_league_names())
    assert "extra_league" not in out


# ──────────────────────── 2 业务流端点测 ────────────────────────


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")

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
    return TestClient(app, client=("127.0.0.1", 5000))


def _login(client):
    r = client.post("/api/v1/login",
                    json={"username": "unit-test-user", "password": "unit-test-pass"})
    assert r.status_code == 200, r.text
    return r.cookies


def test_prediction_metadata_endpoint_returns_leagues(client, monkeypatch):
    """R24 #1：/api/v1/prediction-metadata 业务流 — 返回每 league 的 _run_metadata。"""
    from web.services import store as ws

    def fake_latest_by_league():
        return {
            "epl": {
                "name": "prediction_2026-10-15_10_epl.json",
                "data": {
                    "generated_at": "2026-10-15T10:00:00+08:00",
                    "status": "ok",
                    "data_source": "espn",
                    "dixon_coles_enabled": True,
                    "predictions": [{"x": 1}, {"x": 2}],
                },
            }
        }
    monkeypatch.setattr(ws, "latest_by_league", fake_latest_by_league)

    cookies = _login(client)
    r = client.get("/api/v1/prediction-metadata", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "epl" in body["leagues"]
    epl = body["leagues"]["epl"]
    assert epl["league"] == "epl"
    assert epl["file"] == "prediction_2026-10-15_10_epl.json"
    assert epl["n_predictions"] == 2


def test_calibration_endpoint_merges_states_with_latest(client, monkeypatch):
    """R24 #1：/api/v1/calibration 业务流 — calibration_states + latest_by_league 合并。"""
    from web.services import store as ws

    def fake_calibration_states():
        return {
            "epl": {"sample_size": 100, "home_correction": 1.05},
        }
    def fake_latest_by_league():
        return {
            "epl": {
                "data": {
                    "generated_at": "2026-10-15T10:00:00+08:00",
                    "data_window": "20261001-20261014",
                }
            },
            "laliga": {  # 只有 latest，无 state
                "data": {"generated_at": "2026-10-15T10:00:00+08:00"}
            },
        }
    monkeypatch.setattr(ws, "calibration_states", fake_calibration_states)
    monkeypatch.setattr(ws, "latest_by_league", fake_latest_by_league)

    cookies = _login(client)
    r = client.get("/api/v1/calibration", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    # epl：state + latest data 都齐
    epl = body["leagues"]["epl"]
    assert epl["state"]["sample_size"] == 100
    assert epl["generated_at"] == "2026-10-15T10:00:00+08:00"
    # laliga：只有 latest，无 state
    laliga = body["leagues"]["laliga"]
    assert laliga["state"] is None
    assert laliga["generated_at"] == "2026-10-15T10:00:00+08:00"
