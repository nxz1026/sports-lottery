"""test_jc_daily_image.py — ``web.app.jc.daily_image_vm`` 纯函数单测。

本套用例是 ``tests/web/test_jc_daily_image.py``（端到端契约）的**双胞胎**：
通过相同的输入 stub，验证 ``/api/jc/daily-image`` 端点背后的 VM 与端点层
行为一致。区别：

  - tests/web/test_jc_daily_image.py：FastAPI TestClient，需要登录、鉴权、
    session；侧重端到端契约；
  - tests/app/test_jc_daily_image.py：直接调 ``daily_image_vm()``，无 FastAPI
    依赖、无鉴权开销；侧重 VM 编排逻辑（白名单、分组、空态、basketball 窗口）。

两套共用 ``store.jc_view.fixtures_on`` / ``web.services.store`` 的 monkeypatch 形态。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------- fixtures ----------------------------------------------------

@pytest.fixture()
def patch_today(monkeypatch):
    """固定 today=BJT 业务日，绕开 ``web.services.store.bjt_today``。"""
    fixed = date(2026, 9, 21)
    monkeypatch.setattr("web.services.store.bjt_today", lambda: fixed)
    return fixed


@pytest.fixture()
def empty_accuracy(monkeypatch):
    """VM 内 ``_hit_stats`` 走 ``league_accuracy_web``；空态（reconciled=0）

    返回 ``{"available": False, ...}``，避免单测触碰 ``scripts/predictions/`` 真实文件。
    """
    monkeypatch.setattr(
        "web.services.accuracy.league_accuracy_web",
        lambda league_key, days=30: {"reconciled": 0, "direction_accuracy": 0.0},
    )


@pytest.fixture()
def filled_accuracy(monkeypatch):
    """填一组固定命中率，便于断言 stats.hit.n / stats.hit.hit。"""
    monkeypatch.setattr(
        "web.services.accuracy.league_accuracy_web",
        lambda league_key, days=30: {"reconciled": 100, "direction_accuracy": 0.5},
    )


# ---------- Step 1：football 空态 --------------------------------------

def test_football_empty_returns_available_false(
    patch_today, empty_accuracy, monkeypatch
):
    """五大联赛 + 临时赛事都为空：available=False + 空 rows + 友好 note。"""
    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [])
    monkeypatch.setattr("web.services.team_match.build_rows", lambda fs, pl: [])

    from web.app.jc import daily_image_vm
    d = daily_image_vm(sport="football")
    assert d["sport"] == "football"
    assert d["available"] is False
    assert d["rows"] == []
    assert d["main_rows"] == []
    assert d["temp_rows"] == []
    assert d["date"] == "2026-09-21"
    assert "无在售" in d["note"]
    assert d["stats"]["main_on_sale"] == 0
    assert d["stats"]["temp_on_sale"] == 0


# ---------- Step 2：仅五大联赛 ---------------------------------------

def test_football_big5_only(patch_today, empty_accuracy, monkeypatch):
    """只返五大联赛：rows == main_rows, temp_rows=[], note=None。"""
    monkeypatch.setattr(
        "store.jc_view.fixtures_on",
        lambda d: [{"match_id": "1", "play_type": "had"}],
    )
    monkeypatch.setattr(
        "web.services.team_match.build_rows",
        lambda fs, pl: [{"league_cn": "英格兰超级联赛", "matched": True,
                         "home_cn": "A", "away_cn": "B"}],
    )

    from web.app.jc import daily_image_vm
    d = daily_image_vm(sport="football")
    assert d["available"] is True
    assert len(d["rows"]) == 1
    assert len(d["main_rows"]) == 1
    assert d["temp_rows"] == []
    assert d["stats"]["main_on_sale"] == 1
    assert d["stats"]["main_recommend"] == 1
    # 五大联赛有赛事时不打空 note
    assert d["note"] is None


# ---------- Step 3：仅临时赛事（五大联赛空）---------------------------

def test_football_temp_only_with_note(patch_today, empty_accuracy, monkeypatch):
    """五大联赛空 + 临时赛事有：note 提示主面板空 + 临时赛事数量。"""
    monkeypatch.setattr("web.config.LEAGUE_TEMP_LEAGUES", ("亚运会女足",))

    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [{"match_id": "1", "play_type": "had"}])
    monkeypatch.setattr(
        "web.services.team_match.build_rows",
        lambda fs, pl: [{"league_cn": "亚运会女足", "matched": False,
                         "home_cn": "中国女足", "away_cn": "菲律宾女足"}],
    )
    monkeypatch.setattr("web.services.store.bjt_today", lambda: date(2026, 9, 21))

    from web.app.jc import daily_image_vm
    d = daily_image_vm(sport="football")
    assert d["available"] is True
    assert d["main_rows"] == []
    assert len(d["temp_rows"]) == 1
    assert d["temp_rows"][0]["league_cn"] == "亚运会女足"
    assert d["stats"]["main_on_sale"] == 0
    assert d["stats"]["temp_on_sale"] == 1
    assert d["note"] is not None
    assert "五大联赛无在售" in d["note"]
    assert "1 场临时赛事" in d["note"]
    assert "亚运会女足" in d["note"]


# ---------- Step 4：五大联赛 + 临时赛事同在 ---------------------------

def test_football_big5_and_temp_split(patch_today, empty_accuracy, monkeypatch):
    """五大 + 临时同在：rows=main_rows, temp_rows 单独暴露（避免前端同场重复）。"""
    monkeypatch.setattr(
        "web.config.LEAGUE_TEMP_LEAGUES", ("亚运会女足", "亚运会男足"),
    )

    monkeypatch.setattr(
        "store.jc_view.fixtures_on",
        lambda d: [{"match_id": str(i), "play_type": "had"} for i in range(3)],
    )
    monkeypatch.setattr(
        "web.services.team_match.build_rows",
        lambda fs, pl: [
            {"league_cn": "英格兰超级联赛", "matched": True, "home_cn": "A", "away_cn": "B"},
            {"league_cn": "亚运会女足", "matched": False, "home_cn": "X", "away_cn": "Y"},
            {"league_cn": "亚运会男足", "matched": False, "home_cn": "P", "away_cn": "Q"},
        ],
    )
    monkeypatch.setattr("web.services.store.bjt_today", lambda: date(2026, 9, 21))

    from web.app.jc import daily_image_vm
    d = daily_image_vm(sport="football")
    assert d["available"] is True
    # rows 字段 = main_rows（兜底主面板），temp_rows 单独暴露，避免前端同场重复
    assert len(d["rows"]) == 1
    assert d["rows"][0]["league_cn"] == "英格兰超级联赛"
    assert len(d["main_rows"]) == 1
    assert d["main_rows"][0]["league_cn"] == "英格兰超级联赛"
    assert len(d["temp_rows"]) == 2
    temp_lgs = {r["league_cn"] for r in d["temp_rows"]}
    assert temp_lgs == {"亚运会女足", "亚运会男足"}
    assert d["note"] is None  # 五大联赛有赛事 → note=None
    assert d["stats"]["main_recommend"] == 1
    assert d["stats"]["temp_on_sale"] == 2


# ---------- Step 5：白名单过滤（非 big5 + 非 temp 被过滤）-------------

def test_football_filters_non_whitelist(patch_today, empty_accuracy, monkeypatch):
    """白名单外 (五大联赛 ∪ LEAGUE_TEMP_LEAGUES) 的联赛被过滤，不出现在 rows。"""
    monkeypatch.setattr(
        "store.jc_view.fixtures_on",
        lambda d: [{"match_id": "1", "play_type": "had"}],
    )
    monkeypatch.setattr(
        "web.services.team_match.build_rows",
        lambda fs, pl: [{"league_cn": "葡萄牙超级联赛", "matched": True,
                         "home_cn": "A", "away_cn": "B"}],
    )

    from web.app.jc import daily_image_vm
    d = daily_image_vm(sport="football")
    assert d["available"] is False
    assert d["rows"] == []
    assert d["main_rows"] == []
    assert d["temp_rows"] == []


# ---------- Step 6：basketball 休赛期 ---------------------------------

def test_basketball_no_predictions_returns_available_false(
    patch_today, empty_accuracy, monkeypatch
):
    """NBA 无预测：available=False + 休赛期 note。"""
    monkeypatch.setattr("web.services.store.latest_by_league", lambda: {"nba": {"data": {}}})
    monkeypatch.setattr("web.services.team_match.nba_rows", lambda preds: [])

    from web.app.jc import daily_image_vm
    d = daily_image_vm(sport="basketball")
    assert d["sport"] == "basketball"
    assert d["available"] is False
    assert d["rows"] == []
    assert "NBA" in d["note"]


def test_basketball_with_recent_preds_returns_available_true(
    patch_today, empty_accuracy, monkeypatch
):
    """NBA 预测有最近 7 天内的排期：available=True。"""
    today_iso = date(2026, 9, 21)
    monkeypatch.setattr("web.services.store.latest_by_league", lambda: {
        "nba": {"data": {"predictions": [
            {"home": "LAL", "away": "BOS", "kickoff_date": "2026-09-22",
             "direction": "LAL 胜", "spread_prediction": -3.5,
             "total_prediction": 220.5, "predicted_margin": 5, "predicted_score": "110-105"},
        ]}},
    })
    monkeypatch.setattr("web.services.team_match.nba_rows", lambda preds: [
        {"home": "LAL", "away": "BOS", "match": "LAL vs BOS", "direction": "LAL 胜"},
    ])

    from web.app.jc import daily_image_vm
    d = daily_image_vm(sport="basketball", today=today_iso)
    assert d["available"] is True
    assert len(d["rows"]) == 1
    assert d["rows"][0]["match"] == "LAL vs BOS"
    assert d["date"] == "2026-09-21"


def test_basketball_out_of_window_returns_available_false(
    patch_today, empty_accuracy, monkeypatch
):
    """NBA 预测全部 >7 天外（纯赛程预估）：available=False。"""
    today_iso = date(2026, 9, 21)
    monkeypatch.setattr("web.services.store.latest_by_league", lambda: {
        "nba": {"data": {"predictions": [
            {"home": "LAL", "away": "BOS", "kickoff_date": "2026-11-01"},  # 41 天后
        ]}},
    })
    monkeypatch.setattr("web.services.team_match.nba_rows", lambda preds: [
        {"home": "LAL", "away": "BOS", "match": "LAL vs BOS"},
    ])

    from web.app.jc import daily_image_vm
    d = daily_image_vm(sport="basketball", today=today_iso)
    assert d["available"] is False


# ---------- Step 7：stats.hit 透传 ------------------------------------

def test_football_hit_stats_reflects_accuracy(patch_today, filled_accuracy, monkeypatch):
    """填了 accuracy 100×0.5 → 5 大联赛各贡献 100/50，累计 n=500, hit=250。"""
    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [])
    monkeypatch.setattr("web.services.team_match.build_rows", lambda fs, pl: [])

    from web.app.jc import daily_image_vm
    d = daily_image_vm(sport="football")
    assert d["stats"]["hit"]["available"] is True
    assert d["stats"]["hit"]["n"] == 500
    assert d["stats"]["hit"]["hit"] == 250