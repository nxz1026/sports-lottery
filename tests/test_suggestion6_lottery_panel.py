"""建议 6（体彩侧独立小产品）：遗漏值 / 拆法对比 的服务层 + 端点 + 守门测试。

背景（路线图 §6.8）：
  · 建议 6 验收 ① 遗漏值统计可查可解释 ② 复式拆法给出注数与成本对比。
  · 诚实底线：只做统计呈现，不做收益承诺 —— 选号能力实测与纯随机无法区分
    （§6.8.4 合并 p = 0.273 / 0.323 / 0.200），故 **不提供任何选号推荐**。

覆盖点：
  服务层纯函数（无 DB/IO，可手算对账）
    · omission_stats —— cur/avg/max/count 四字段口径（含「从未出现」与首/中/尾三段）
    · lottery_pools —— 4 个彩种各自的号码池切分
    · omission_report —— 热号/冷号/最大遗漏 排序 + DISCLAIMER
    · compound_splits —— 复式/拆单式/胆拖 三方案（注数·成本·出票张数·覆盖）
      + 两条恒等式（拆单式≡复式、胆拖⊂复式）
  端点
    · /api/v1/lottery/omission —— 200 / 401 / 422 / unknown_game / 空数据降级
    · /api/v1/lottery/splits —— 200 / 401 / 400 / 非大乐透降级
    · 老端点 /lottery/{stats,cost,dantuo,prize} 一字未动（回归）
  守门
    · DISCLAIMER 规范句逐字出现在 服务层 / 看板 / 路线图 三处
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import web.services.lottery as L  # noqa: E402

ROADMAP = REPO_ROOT / "docs" / "project" / "产品路线图-竞彩闭环-20261020.md"
DASHBOARD = REPO_ROOT / "static" / "dashboard.html"


# ─────────────────────────── 服务层：omission_stats ───────────────────────────

def test_omission_stats_hand_computed_four_fields():
    """手算对账：periods=[[1],[2],[1],[3]]，池 1..3，共 4 期。

    号码 1：出现于第 0、2 期 → count=2；cur=4-1-2=1；avg=4/2=2.0；
            中段间隔=2-0-1=1；首段=0；尾段=1 ⇒ max=1
    号码 2：出现于第 1 期 → count=1；cur=4-1-1=2；avg=4.0；首段=1；max=2
    号码 3：出现于第 3 期 → count=1；cur=0；avg=4.0；首段=3；max=3
    """
    st = L.omission_stats([[1], [2], [1], [3]], 1, 3)
    assert set(st) == {1, 2, 3}
    assert st[1] == {"cur": 1, "avg": 2.0, "max": 1, "count": 2}
    assert st[2] == {"cur": 2, "avg": 4.0, "max": 2, "count": 1}
    assert st[3] == {"cur": 0, "avg": 4.0, "max": 3, "count": 1}


def test_omission_stats_never_seen_number():
    """从未出现的号码：count=0、avg=None、cur=max=总期数。"""
    st = L.omission_stats([[1], [1]], 1, 5)
    assert st[5] == {"cur": 2, "avg": None, "max": 2, "count": 0}


def test_omission_stats_max_uses_three_segments_not_index_gap():
    """max 必须是「连续未出现期数」，**不是**相邻两次出现的索引差（两者差 1）。

    号码 7 出现于第 0 期与第 3 期 ⇒ 索引差 3，连续未出现 2 期（idx 1、2）。
    若错用索引差会得到 3 ⇒ 这里锁死 2。
    """
    st = L.omission_stats([[7], [1], [2], [7]], 1, 8)
    assert st[7]["max"] == 2


def test_omission_stats_empty_periods():
    """空样本 → 每号 count=0 / cur=0 / avg=None / max=0，不抛错。"""
    st = L.omission_stats([], 1, 3)
    assert st[1] == {"cur": 0, "avg": None, "max": 0, "count": 0}


def test_omission_stats_is_unit_period_not_day():
    """口径是「期」：期数变多 ⇒ cur 按期数增长（与日历天无关）。"""
    periods = [[1]] + [[9]] * 9          # 号码 1 只在第 0 期出现
    st = L.omission_stats(periods, 1, 9)
    assert st[1]["cur"] == 9              # 9 期没出现
    assert st[1]["count"] == 1
    assert st[1]["avg"] == 10.0           # 总期数 10 ÷ 出现 1


# ─────────────────────────── 服务层：lottery_pools ───────────────────────────

def test_lottery_pools_dlt_front_and_back():
    """大乐透：前区（5 个 1-35）+ 后区（2 个 1-12）两个池。"""
    draws = [[1, 2, 3, 4, 5, 6, 7], [8, 9, 10, 11, 12, 1, 2]]
    pools = L.lottery_pools("85", draws)
    assert [p["pool"] for p in pools] == ["front", "back"]
    assert pools[0]["periods"] == [[1, 2, 3, 4, 5], [8, 9, 10, 11, 12]]
    assert pools[1]["periods"] == [[6, 7], [1, 2]]
    assert (pools[0]["lo"], pools[0]["hi"]) == (1, 35)
    assert (pools[1]["lo"], pools[1]["hi"]) == (1, 12)


def test_lottery_pools_qxc_six_digits_plus_special():
    """7星彩：6 个基本位（各 0-9）+ 特别号（0-14）= 7 个池。"""
    draws = [[1, 2, 3, 4, 5, 6, 14], [0, 9, 8, 7, 6, 5, 3]]
    pools = L.lottery_pools("04", draws)
    assert len(pools) == 7
    assert pools[-1]["pool"] == "special"
    assert (pools[-1]["lo"], pools[-1]["hi"]) == (0, 14)
    assert pools[0]["periods"] == [[1], [0]]
    assert pools[5]["periods"] == [[6], [5]]


@pytest.mark.parametrize("game,digits", [("35", 3), ("350133", 5)])
def test_lottery_pools_positional_games(game, digits):
    """排列3/排列5：按位各成一池，每池 0-9。"""
    draws = [[i % 10 for i in range(digits)], [1] * digits]
    pools = L.lottery_pools(game, draws)
    assert len(pools) == digits
    assert all(p["lo"] == 0 and p["hi"] == 9 for p in pools)


def test_lottery_pools_unknown_game_returns_empty():
    assert L.lottery_pools("999", [[1, 2]]) == []


# ─────────────────────────── 服务层：omission_report ───────────────────────────

def test_omission_report_shape_and_sorted_lists():
    """报告结构：pools / items / hot / cold / max_omission + DISCLAIMER。"""
    draws = [[1, 2, 3, 4, 5, 6, 7], [1, 2, 3, 4, 5, 1, 2], [8, 9, 10, 11, 12, 3, 4]]
    rep = L.omission_report("85", draws)
    assert rep["game"] == "85"
    assert rep["game_name"] == "超级大乐透"
    assert rep["n_periods"] == 3
    assert rep["unit"] == "期"
    assert rep["note"] == L.DISCLAIMER
    front = rep["pools"][0]
    assert front["pool"] == "front" and front["size"] == 35
    assert len(front["items"]) == 35
    assert front["items"][0]["num"] == 1
    # 号码 1 在 3 期里出现 2 次 → 出现最多
    assert front["hot"][0]["num"] == 1
    assert front["hot"][0]["count"] == 2
    # 冷号 = 出现次数最少（并列按号码升序取前 5）
    assert all(x["count"] == 0 for x in front["cold"])
    # 遗漏最久 = 按 cur 降序
    assert front["max_omission"][0]["cur"] >= front["max_omission"][-1]["cur"]


def test_omission_report_unknown_game_degrades():
    """未识别彩种 → 200 + note=unknown_game + pools=[]（不抛错）。"""
    rep = L.omission_report("999", [[1, 2]])
    assert rep["pools"] == []
    assert rep["note"] == "unknown_game"


def test_omission_report_no_draws_returns_empty_pools():
    """库内无该彩种历史 → n_periods=0 + pools=[]。"""
    rep = L.omission_report("85", [])
    assert rep["n_periods"] == 0
    assert rep["pools"] == []
    assert rep["note"] == L.DISCLAIMER


# ─────────────────────────── 服务层：compound_splits ───────────────────────────

def test_splits_compound_equals_split_singles_in_cost_and_coverage():
    """恒等式 ①：拆单式与复式覆盖同一批组合 ⇒ 注数与成本必然相同。"""
    r = L.compound_splits("85", [1, 2, 3, 4, 5, 6], [1, 2, 3])
    rows = {x["scheme"]: x for x in r["rows"]}
    comp = rows["复式（1 张票）"]
    singles = rows["拆单式"]
    assert comp["tickets"] == 18 and comp["cost"] == 36 and comp["ticket_count"] == 1
    assert singles["tickets"] == 18 and singles["cost"] == 36 and singles["ticket_count"] == 18
    assert comp["coverage"] == singles["coverage"] == 18
    assert comp["coverage_ratio"] == singles["coverage_ratio"] == 1.0
    assert r["formula"] == "复式注数 = C(6,5)×C(3,2) = 18"


def test_splits_dan_tuo_is_strict_subset_of_compound():
    """恒等式 ②：胆拖注数更少，但覆盖是复式的真子集（胆码须全中）。"""
    r = L.compound_splits("85", [1, 2, 3, 4, 5, 6], [1, 2, 3])
    rows = {x["scheme"]: x for x in r["rows"]}
    dan = rows["胆拖 前1胆5拖/后1胆2拖"]
    assert dan["tickets"] == 10 and dan["cost"] == 20
    assert dan["coverage"] == 10
    assert dan["coverage_ratio"] == pytest.approx(10 / 18, rel=1e-4)
    assert "子集" in dan["note"]


@pytest.mark.parametrize("front,back,tickets,cost", [
    ([1, 2, 3, 4, 5, 6], [1, 2, 3], 18, 36),
    ([1, 2, 3, 4, 5, 6, 7, 8], [1, 2, 3, 4], 336, 672),
    ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], [1, 2, 3], 756, 1512),
])
def test_splits_note_count_is_c_m_times_c_n_2(front, back, tickets, cost):
    """复式注数 = C(m,5)×C(n,2)，**不是 m×n**（订正的建议 6 原文口径）。"""
    from math import comb
    m, n = len(front), len(back)
    r = L.compound_splits("85", front, back)
    comp = next(x for x in r["rows"] if x["scheme"].startswith("复式"))
    assert comp["tickets"] == tickets == comb(m, 5) * comb(n, 2)
    assert comp["cost"] == cost


def test_splits_old_m_times_n_formula_would_be_wrong():
    """旧口径 m×n 会算错：m=8/n=4 ⇒ 正确 336，m×n 只有 32（差 10 倍）。"""
    from math import comb
    m, n = 8, 4
    r = L.compound_splits("85", list(range(1, m + 1)), list(range(1, n + 1)))
    comp = next(x for x in r["rows"] if x["scheme"].startswith("复式"))
    assert comp["tickets"] == comb(m, 5) * comb(n, 2) == 336
    assert comp["tickets"] != m * n == 32


def test_splits_dan_tuo_zero_dan_equals_compound():
    """全拖（0 胆）= 复式本身 ⇒ 不单列（避免重复行）。"""
    r = L.compound_splits("85", [1, 2, 3, 4, 5, 6], [1, 2, 3])
    schemes = [x["scheme"] for x in r["rows"]]
    assert not any(s.startswith("胆拖 前0胆") and "后0胆" in s for s in schemes)


def test_splits_dan_tuo_rows_sorted_by_tickets_desc():
    r = L.compound_splits("85", [1, 2, 3, 4, 5, 6], [1, 2, 3])
    dan = [x for x in r["rows"] if x["scheme"].startswith("胆拖")]
    assert [x["tickets"] for x in dan] == sorted([x["tickets"] for x in dan], reverse=True)


def test_splits_too_few_numbers_returns_empty_rows_with_note():
    """选号不足以成注 → rows=[] + note 说明（不抛错）。"""
    r = L.compound_splits("85", [1, 2, 3], [1])
    assert r["rows"] == []
    assert "至少" in r["note"]
    assert r["note"].startswith(L.DISCLAIMER)


def test_splits_non_dlt_game_degrades_with_note():
    """排列/星彩只有直选 → rows=[] + note 说明。"""
    r = L.compound_splits("35", [1, 2, 3], [])
    assert r["rows"] == []
    assert "直选" in r["note"]


def test_splits_unknown_game_returns_unknown_game_note():
    r = L.compound_splits("999", [1, 2, 3], [1])
    assert r["rows"] == []
    assert r["note"] == "unknown_game"


def test_splits_deduplicates_and_clamps_out_of_range():
    """重复号去重、越界号忽略。"""
    r = L.compound_splits("85", [1, 1, 2, 3, 4, 5, 6, 99], [1, 1, 2, 44])
    assert r["front_count"] == 6
    assert r["back_count"] == 2


def test_splits_note_starts_with_disclaimer():
    r = L.compound_splits("85", [1, 2, 3, 4, 5, 6], [1, 2, 3])
    assert r["note"].startswith(L.DISCLAIMER)
    assert "出票张数" in r["note"]


# ─────────────────────────── 端点 ───────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")

    import web.config as config
    import web.session_store as session_store
    importlib.reload(config)
    importlib.reload(session_store)
    import web.auth as auth_mod
    importlib.reload(auth_mod)
    import web.api as api
    importlib.reload(api)

    # mock store.lottery_draws（避免走真 DB）：故意给**降序**的 issue_no，
    # 用来验证端点内部会重排成升序（遗漏值对顺序敏感）。
    import store.jc_view as jcv

    def _fake(per_type=20):
        return [
            {"game_num": "85", "issue_no": "26119", "draw_date": "2026-10-19",
             "numbers_raw": "03 06 20 24 32 04 07"},
            {"game_num": "85", "issue_no": "26118", "draw_date": "2026-10-17",
             "numbers_raw": "02 09 21 26 32 06 12"},
            {"game_num": "85", "issue_no": "26117", "draw_date": "2026-10-14",
             "numbers_raw": "04 13 19 29 31 02 12"},
        ]

    monkeypatch.setattr(jcv, "lottery_draws", _fake)
    return TestClient(api.create_app(), client=("127.0.0.1", 5000))


def _login(client):
    r = client.post("/api/v1/login",
                    json={"username": "unit-test-user", "password": "unit-test-pass"})
    assert r.status_code == 200, r.text
    return r.cookies


def test_omission_endpoint_unauthenticated_returns_401(client):
    r = client.get("/api/v1/lottery/omission", params={"game": "85"})
    assert r.status_code in (401, 403), r.text


def test_omission_endpoint_returns_200_with_pools(client):
    cookies = _login(client)
    r = client.get("/api/v1/lottery/omission", params={"game": "85", "n": 100},
                   cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["game"] == "85"
    assert body["n_periods"] == 3
    assert body["unit"] == "期"
    assert body["note"] == L.DISCLAIMER
    assert [p["pool"] for p in body["pools"]] == ["front", "back"]
    assert len(body["pools"][0]["items"]) == 35


def test_omission_endpoint_unknown_game_returns_200(client):
    cookies = _login(client)
    r = client.get("/api/v1/lottery/omission", params={"game": "999"}, cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["pools"] == []
    assert r.json()["note"] == "unknown_game"


def test_omission_endpoint_n_out_of_range_returns_422(client):
    cookies = _login(client)
    for n in (0, 200):
        r = client.get("/api/v1/lottery/omission", params={"game": "85", "n": n},
                       cookies=cookies)
        assert r.status_code == 422, r.text


def test_omission_endpoint_orders_draws_ascending(client):
    """遗漏值对顺序敏感 ⇒ 端点必须把 issue_no 降序的 store 结果重排成升序。"""
    cookies = _login(client)
    r = client.get("/api/v1/lottery/omission", params={"game": "85"}, cookies=cookies)
    assert r.status_code == 200, r.text
    front = r.json()["pools"][0]
    # 3 期：03/06/20/24/32 → 02/09/21/26/32 → 04/13/19/29/31
    # 号码 32 出现于第 1、2 期（升序）⇒ cur = 3-1-2 = 0
    assert front["items"][31]["cur"] == 0
    assert front["items"][31]["count"] == 2


def test_splits_endpoint_unauthenticated_returns_401(client):
    r = client.get("/api/v1/lottery/splits",
                   params={"game": "85", "front": "1,2,3,4,5,6", "back": "1,2,3"})
    assert r.status_code in (401, 403), r.text


def test_splits_endpoint_returns_rows(client):
    cookies = _login(client)
    r = client.get("/api/v1/lottery/splits",
                   params={"game": "85", "front": "1,2,3,4,5,6", "back": "1,2,3"},
                   cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["front_count"] == 6 and body["back_count"] == 3
    rows = {x["scheme"]: x for x in body["rows"]}
    assert rows["复式（1 张票）"]["tickets"] == 18
    assert rows["拆单式"]["ticket_count"] == 18
    assert rows["胆拖 前1胆5拖/后1胆2拖"]["tickets"] == 10
    assert body["note"].startswith(L.DISCLAIMER)


def test_splits_endpoint_non_numeric_returns_400(client):
    cookies = _login(client)
    r = client.get("/api/v1/lottery/splits",
                   params={"game": "85", "front": "1,x,3", "back": "1,2"}, cookies=cookies)
    assert r.status_code == 400, r.text
    assert "front" in r.json()["message"]


def test_splits_endpoint_no_numbers_returns_200_with_empty_rows(client):
    cookies = _login(client)
    r = client.get("/api/v1/lottery/splits", params={"game": "85"}, cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == []


# ─────────────────────────── 老端点回归（一字未动） ───────────────────────────

def test_old_stats_endpoint_unchanged(client):
    """老端点 /lottery/stats 返回结构不变（stats 的老字段一字未动）。"""
    cookies = _login(client)
    r = client.get("/api/v1/lottery/stats", params={"game": "85"}, cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"game", "n_periods", "stats"}
    assert body["n_periods"] == 3
    for k in ("count", "unique", "most", "odd_even", "sum_min", "sum_max", "avg"):
        assert k in body["stats"], k


def test_old_cost_endpoint_unchanged(client):
    cookies = _login(client)
    r = client.get("/api/v1/lottery/cost",
                   params={"game": "85", "front": "1,2,3,4,5,6", "back": "1,2,3"},
                   cookies=cookies)
    assert r.status_code == 200, r.text
    cost = r.json()["cost"]
    assert cost["注数"] == 18
    assert cost["每注2元成本"] == 36
    assert cost["拆法"] == "C(6,5)×C(3,2)"


def test_old_dantuo_endpoint_unchanged(client):
    cookies = _login(client)
    r = client.get("/api/v1/lottery/dantuo",
                   params={"dan": 2, "tuo": 8, "choose": 5}, cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["注数"] == 56      # C(8, 5-2) = C(8,3)


def test_old_prize_endpoint_unchanged(client):
    """老端点 /lottery/prize 判定不变：前 5 中 + 后 1 中 = 二等奖。"""
    cookies = _login(client)
    r = client.get("/api/v1/lottery/prize",
                   params={"game": "85",
                           "ticket": "1,2,3,4,5,6,7",
                           "draw": "1,2,3,4,5,6,8"}, cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["front_hit"] == 5 and body["back_hit"] == 1
    assert body["prize"] == "二等奖"


# ─────────────────────────── 守门：诚实提示逐字三处 ───────────────────────────

def test_disclaimer_literal_in_service_dashboard_and_roadmap():
    """规范句必须逐字出现在：服务层常量 / 看板面板 / 路线图 §6.8.6。"""
    dash = DASHBOARD.read_text(encoding="utf-8")
    road = ROADMAP.read_text(encoding="utf-8")
    assert L.DISCLAIMER in dash, "看板面板缺少诚实提示规范句"
    assert L.DISCLAIMER in road, "路线图缺少诚实提示规范句"
    assert "lotPanelOmission" in dash and "lotPanelSplits" in dash


def test_roadmap_has_suggestion6_section_with_key_measured_numbers():
    """路线图 §6.8 必须写清实测口径与关键数字（防止有人只改代码不改文档）。

    ⚠️ 零假设检验的数字**按 §6.8.4 段落**校验，不能全文匹配 ——
    变更记录里会合法引用旧口径数字做「订正前 → 订正后」对照。
    """
    road = ROADMAP.read_text(encoding="utf-8")
    assert "### 6.8 建议 6 的实测订正与落地版" in road
    i684 = road.find("#### 6.8.4")
    i685 = road.find("#### 6.8.5")
    sec684 = road[i684:i685] if 0 <= i684 < i685 else ""
    for token in ("C(m,5)×C(n,2)", "36 期", "24 期"):
        assert token in road, token
    for token in ("0.273", "0.323", "0.200", "99.750%", "Bonferroni"):
        assert token in sec684, f"§6.8.4 缺少 {token}"
    for stale in ("0.348", "0.411", "0.278", "99.667%"):
        assert stale not in sec684, f"§6.8.4 仍残留旧口径 {stale}"


def test_dashboard_panel_has_both_buttons_and_note():
    dash = DASHBOARD.read_text(encoding="utf-8")
    for el in ("lotPanelGame", "lotPanelN", "lotPanelOmissionBtn", "lotPanelSplitsBtn",
               "lotPanelFront", "lotPanelBack", "lotPanelOmission", "lotPanelSplits"):
        assert el in dash, el


def test_no_betting_advice_words_in_new_panel():
    """新面板不得出现「推荐/必中/稳赚/回本」这类承诺词。"""
    dash = DASHBOARD.read_text(encoding="utf-8")
    for word in ("必中", "稳赚", "回本", "包中", "盈利"):
        assert word not in dash, word
