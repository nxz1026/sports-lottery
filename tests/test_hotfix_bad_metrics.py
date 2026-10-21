"""顺手修 A/B 的回归测试：两处「无数据被当成 0 / 条件恒假」型缺陷。

A. `/api/jc/daily-image` 的「主面板空 + 有临时赛事」提示条**永远不显示**
   —— 根因：VM 里 `rows` 就等于 `main_rows`（web/app/jc.py:247-248），
   而 `static/dailyimage.html` 的提示条条件写的是
   `d.main_rows.length===0 && (d.rows||[]).length` ⇒ **恒假**
   ⇒ 用户只看到一张空白主图，**没有任何解释**。

B. 校准的 `odds_accuracy` 在**无赔率样本**时返回 0 ⇒ predict.py 印出
   "Odds favorite accuracy: 0% (0/0)"，读起来像「赔率热门从没赢过」，
   实际是「**根本没有赔率样本**」（生产实测：historical_past_matches.json
   的 76 场历史样本**没有一场**带 home_true_prob）
   ⇒ 违反建议 3 定的「n 不足不显示均值」纪律。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

DAILYIMAGE = REPO_ROOT / "static" / "dailyimage.html"


# ─────────────────────── A. daily-image 提示条 ───────────────────────

@pytest.fixture()
def vm_temp_only(monkeypatch):
    """造出「五大联赛空 + 临时赛事有」的 daily-image VM 输出。

    与 `tests/app/test_jc_daily_image.py::test_football_temp_only_with_note`
    同一形态；这里额外把 note 里的联赛名保留，供断言。
    """
    monkeypatch.setenv("SESSION_DB_PATH", str(REPO_ROOT / ".pytest_sessions_hotfix"))
    monkeypatch.setattr("web.config.LEAGUE_TEMP_LEAGUES", ("亚运会女足",))
    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [{"match_id": "1"}])
    monkeypatch.setattr(
        "web.services.team_match.build_rows",
        lambda fs, pl: [{"league_cn": "亚运会女足", "matched": False,
                         "home_cn": "中国女足", "away_cn": "菲律宾女足"}],
    )
    monkeypatch.setattr("web.services.store.bjt_today", lambda: date(2026, 9, 21))
    monkeypatch.setattr(
        "web.services.accuracy.league_accuracy_web",
        lambda league_key, days=30: {"reconciled": 0, "direction_accuracy": 0.0},
    )

    from web.app.jc import daily_image_vm
    return daily_image_vm(sport="football")


def test_vm_rows_is_main_rows_contract(vm_temp_only):
    """锁死 VM 契约：`rows` == `main_rows`（**不是** main+temp）。

    这正是旧提示条条件恒假的根因 ⇒ 必须显式锁住，
    免得后人把 `rows` 改成「全部行」后，前端条件又悄悄失效。
    """
    d = vm_temp_only
    assert d["rows"] == d["main_rows"]
    assert d["main_rows"] == []
    assert len(d["temp_rows"]) == 1


def test_vm_temp_only_case_has_note_and_temp_rows(vm_temp_only):
    """temp-only 场景：available=True + main_rows 空 + temp_rows 有 + note 非空。"""
    d = vm_temp_only
    assert d["available"] is True
    assert d["main_rows"] == []
    assert len(d["temp_rows"]) == 1
    assert d["note"] and "五大联赛与欧冠均无在售" in d["note"]


def test_dailyimage_note_bar_condition_is_not_dead(vm_temp_only):
    """回归：提示条条件在「主面板空 + 有临时赛事」时**必须为真**。

    旧写法 `d.main_rows.length===0 && (d.rows||[]).length` 恒假（rows 就是 main_rows）。
    """
    d = vm_temp_only
    # 新写法：看 temp_rows
    new_cond = (d["main_rows"] is not None and len(d["main_rows"]) == 0
                and len(d.get("temp_rows") or []) and bool(d.get("note")))
    assert new_cond, "temp-only 场景下新条件必须成立，否则提示条仍不显示"
    # 旧写法：看 rows —— 恒假（这正是要修的 bug）
    old_cond = (d["main_rows"] is not None and len(d["main_rows"]) == 0
                and len(d.get("rows") or []) and bool(d.get("note")))
    assert not old_cond, "旧条件（看 rows）在本场景恒假 —— 说明 bug 的根因"


def test_dailyimage_html_condition_uses_temp_rows():
    """守门：dailyimage.html 的提示条条件必须看 temp_rows，不能看 rows。

    只认**真正的 if 语句行**（以 `if(` 开头）—— 注释里可以引用旧写法做说明，
    不能因此误判。
    """
    html = DAILYIMAGE.read_text(encoding="utf-8")
    code_lines = [
        ln.strip() for ln in html.splitlines()
        if "d.main_rows.length===0" in ln and ln.strip().startswith("if(")
    ]
    assert code_lines, "找不到提示条条件的 if 语句行"
    for ln in code_lines:
        assert "d.temp_rows" in ln, f"提示条条件没有用 temp_rows：{ln[:160]}"
        assert "(d.rows||[]).length" not in ln, f"提示条条件仍在用 rows（恒假）：{ln[:160]}"


# ─────────────────────── B. 校准的 odds_accuracy ───────────────────────

def _load_predict():
    spec = importlib.util.spec_from_file_location(
        "predict_hotfix_test", REPO_ROOT / "scripts" / "predict.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_build_calibration_odds_accuracy_is_none_without_odds_samples():
    """无 home_true_prob ⇒ odds_accuracy 是 None（无数据）+ odds_note 标记。"""
    from core.calibration import build_calibration
    result = build_calibration([{"score": "2-1"}, {"score": "1-1"}])
    assert result["favored_by_odds"] == 0
    assert result["odds_accuracy"] is None
    assert result["odds_note"] == "no_odds_samples"


def test_predict_summary_prints_na_not_zero_percent(capsys):
    """回归：favored_by_odds=0 时必须印 N/A，**不许**印 "0% (0/0)"。"""
    mod = _load_predict()
    cal = {"total_matches": 76, "home_win_rate": 0.461, "draw_rate": 0.289,
           "away_win_rate": 0.25, "odds_accuracy": None,
           "favored_won": 0, "favored_by_odds": 0}
    mod._print_summary([], cal, None, None, 0, None)
    err = capsys.readouterr().err
    assert "Odds favorite accuracy: N/A" in err
    assert "0% (0/0)" not in err, "不许把「无赔率样本」印成 0% (0/0)"


def test_predict_summary_still_prints_pct_with_odds_samples(capsys):
    """有赔率样本时行为不变：仍印百分比与分子/分母。"""
    mod = _load_predict()
    cal = {"total_matches": 10, "home_win_rate": 0.5, "draw_rate": 0.2,
           "away_win_rate": 0.3, "odds_accuracy": 0.6,
           "favored_won": 6, "favored_by_odds": 10}
    mod._print_summary([], cal, None, None, 0, None)
    err = capsys.readouterr().err
    assert "Odds favorite accuracy: 60% (6/10)" in err
    assert "N/A" not in err


def test_core_dashboard_renders_na_when_odds_accuracy_none(tmp_path):
    """回归：dashboard.py 模板遇 odds_accuracy=None 不能抛 TypeError，显示 N/A。"""
    spec = importlib.util.spec_from_file_location(
        "core_dashboard_hotfix", REPO_ROOT / "scripts" / "core" / "dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    pd = {
        "predictions": [],
        "monte_carlo": {},
        "calibration": {"total_matches": 76, "home_win_rate": 0.461, "draw_rate": 0.289,
                        "away_win_rate": 0.25, "odds_accuracy": None},
        "calibration_offset": None,
        "backtest": {},
        "generated_at": "2026-10-21T00:00:00+00:00",
        "league": "epl",
    }
    out = tmp_path / "dash.html"
    html = mod.generate_dashboard(pd, out)
    assert "Odds Accuracy" in html
    assert ">N/A<" in html, "odds_accuracy=None 时应显示 N/A"
    assert out.is_file()
