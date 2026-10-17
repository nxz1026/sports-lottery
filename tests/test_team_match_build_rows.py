"""R47-9: web/services/team_match.py::build_rows 4 cases direct + D-82 lock.

build_rows (L280-L364, 85 行) 合并竞彩盘口在售场次与预测 -> 每日一图行。
R19 #G2 优化: pred_index_by_pair 反查字典, O(1) 命中 + fuzzy 兜底。

D-74/D-75/D-76/D-79/D-80/D-81 已锁 teams_match / odd_suggestion / algo_suggestion
/ jc_gap / poisson_hhad / nba_rows, 但 build_rows 自身核心编排 (play_type 聚合
+ had 锚/hhad 兜底 + goal_line 3 字段名兼容 + pred_index 命中/fuzzy 兜底 +
algo/hhad_model/haf_model/odd 装配) 0 直测。

D-82 锁定 4 契约:
- D-82-1: 主流程 happy (1 fixture had 锚 + 1 pred 字典命中) -> row 含 algo +
  odd + matched=True + 完整 9 字段
- D-82-2: had 缺, hhad 兜底 (L315 `plays.get('had') or plays.get('hhad')`)
- D-82-3: pred 不命中 (pred_index 空) -> matched=False + 仅有 odd, 无 algo/
  hhad_model/haf_model
- D-82-4: 缺 match_num -> 跳过该 fixture

依赖 stub (避免触发真实 IO + 隔离已测 helper):
- pred_index_by_pair (D-60 已锁, web.services.store 顶部引用)
- teams_match (D-74 已锁)
- algo_suggestion (D-76 已锁)
- poisson_hhad (D-80 已锁)
- odd_suggestion (D-75 已锁)
- web.services.haf.compute_haf (新 stub, 因为 build_rows lazy import)
- web.services.store 顶层引用 (避免 module-level 解析失败)
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def build_env(monkeypatch):
    """构造 build_rows 隔离环境:
    - 预注册 web.services.store + web.services.haf 模块 (避免 module-level 解析失败)
    - 预 stub 所有依赖 (返回受控占位符)
    - spec-load team_match.py
    """
    # 1. 预注册 web.services.store + web.services.haf stub
    store_stub = types.ModuleType("web.services.store")

    def fake_pred_index():
        return getattr(monkeypatch, "_pred_index", {})

    store_stub.pred_index_by_pair = fake_pred_index
    monkeypatch.setitem(sys.modules, "web.services.store", store_stub)

    haf_stub = types.ModuleType("web.services.haf")

    def fake_compute_haf(*args, **kwargs):
        return {"line": "0", "pick": "主胜", "probs": {"主胜": 0.5, "平": 0.3, "客胜": 0.2}}

    haf_stub.compute_haf = fake_compute_haf
    monkeypatch.setitem(sys.modules, "web.services.haf", haf_stub)

    # 2. spec-load team_match.py
    spec = importlib.util.spec_from_file_location(
        "team_match_build_rows_test",
        REPO_ROOT / "web" / "services" / "team_match.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 3. monkeypatch 内部依赖 (返回受控占位符)
    monkeypatch.setattr(mod, "teams_match",
                        lambda h1, a1, h2, a2: False)  # 默认 fuzzy miss
    monkeypatch.setattr(mod, "algo_suggestion",
                        lambda p: {"pick": "主胜", "stars": 0, "score": "",
                                    "direction": ""})
    monkeypatch.setattr(mod, "poisson_hhad",
                        lambda lh, la, ln: {"line": str(ln), "pick": "让胜",
                                              "probs": {"让胜": 0.5}})
    monkeypatch.setattr(mod, "odd_suggestion",
                        lambda had: {"pick": "主胜", "odds": "1.50"})
    return mod


def _set_pred_index(monkeypatch, pred_index: dict):
    """设置 stub pred_index_by_pair 返受控 dict。"""
    monkeypatch._pred_index = pred_index


def _fixture(*, match_num=7001, home="阿森纳", away="切尔西", league="epl",
              play_type="had", options=None, kickoff="2026-07-15T15:00:00+08:00") -> dict:
    """构造 fixture_rows 项。"""
    return {
        "match_num": match_num,
        "league_cn": league,
        "home_cn": home,
        "away_cn": away,
        "kickoff_bj": kickoff,
        "play_type": play_type,
        "options": options or {"h": "1.50", "d": "3.40", "a": "4.50"},
    }


# ──────────────────────── D-82 四契约 ────────────────────────


def test_build_rows_d82_1_happy_path_dict_hit(monkeypatch, build_env):
    """R47-9 D-82-1: 主流程 happy - had 锚 + 字典命中 pred_index。

    1 fixture (had) + 1 pred 字典命中 -> 1 row 含 9 字段 + algo + odd +
    matched=True。
    """
    mod = build_env
    _set_pred_index(monkeypatch, {("阿森纳", "切尔西"): {
        "home": "阿森纳", "away": "切尔西",
        "lambda_home": 1.5, "lambda_away": 1.0,
    }})

    rows = mod.build_rows([_fixture()], pred_by_league={})
    assert len(rows) == 1
    r = rows[0]
    # 9 字段完整
    assert r["match_num"] == 7001
    assert r["league_cn"] == "epl"
    assert r["home_cn"] == "阿森纳"
    assert r["away_cn"] == "切尔西"
    assert r["kickoff_bj"] == "2026-07-15T15:00:00+08:00"
    assert r["had"] == {"h": "1.50", "d": "3.40", "a": "4.50"}
    assert r["hhad"] == {}  # 没 hhad 玩法
    assert r["goal_line"] == ""  # hhad 玩法缺
    assert r["matched"] is True
    # algo / odd 由 stub 提供
    assert r["algo"]["pick"] == "主胜"
    assert r["odd"]["pick"] == "主胜"
    # goal_line 空 -> 无 hhad_model
    assert "hhad_model" not in r


def test_build_rows_d82_2_had_missing_hhad_fallback(monkeypatch, build_env):
    """R47-9 D-82-2: had 缺, hhad 兜底 (L315)。

    fixture 只有 hhad 玩法, 无 had -> had = hhad (兜底)。
    goalLine = -0.5 (主让 0.5 球) -> hhad_model 计算。
    """
    mod = build_env
    _set_pred_index(monkeypatch, {("阿森纳", "切尔西"): {
        "home": "阿森纳", "away": "切尔西",
        "lambda_home": 1.5, "lambda_away": 1.0,
    }})

    rows = mod.build_rows([_fixture(play_type="hhad",
                                       options={"h": "1.95", "d": "3.30", "a": "3.80",
                                                 "goalLine": -0.5})],
                            pred_by_league={})
    assert len(rows) == 1
    r = rows[0]
    # had 兜底用 hhad options
    assert r["had"] == {"h": "1.95", "d": "3.30", "a": "3.80", "goalLine": -0.5}
    # hhad 也是 hhad options (L319 plays.get('hhad'))
    assert r["hhad"]["goalLine"] == -0.5
    # goal_line 解析: -0.5
    assert r["goal_line"] == "-0.5"
    # matched True + goal_line != "" -> hhad_model 计算
    assert r["matched"] is True
    assert r["hhad_model"]["line"] == "-0.5"  # 来自 stub


def test_build_rows_d82_3_pred_index_empty_matched_false(monkeypatch, build_env):
    """R47-9 D-82-3: pred 不命中 (pred_index 空) -> matched=False。

    pred_index 空 + teams_match stub 返 False -> fuzzy miss -> best=None。
    matched=False 时无 algo / hhad_model / haf_model, 仅有 odd。
    """
    mod = build_env
    _set_pred_index(monkeypatch, {})  # 空 pred_index

    rows = mod.build_rows([_fixture()], pred_by_league={})
    assert len(rows) == 1
    r = rows[0]
    assert r["matched"] is False
    # matched=False 时无 algo/hhad_model/haf_model
    assert "algo" not in r
    assert "hhad_model" not in r
    assert "haf_model" not in r
    # odd 仍生成 (odd_suggestion 不依赖预测)
    assert r["odd"]["pick"] == "主胜"


def test_build_rows_d82_4_missing_match_num_skipped(monkeypatch, build_env):
    """R47-9 D-82-4: 缺 match_num -> 跳过该 fixture (L294-296)。

    2 fixture, 1 无 match_num -> 1 row。
    """
    mod = build_env
    _set_pred_index(monkeypatch, {})

    fixtures = [
        _fixture(match_num=7001),
        # 缺 match_num
        {"league_cn": "epl", "home_cn": "X", "away_cn": "Y",
         "play_type": "had", "options": {"h": "2.0", "d": "3.0", "a": "4.0"}},
    ]
    rows = mod.build_rows(fixtures, pred_by_league={})
    assert len(rows) == 1
    assert rows[0]["match_num"] == 7001
    # 第二 fixture 无 match_num 被跳过