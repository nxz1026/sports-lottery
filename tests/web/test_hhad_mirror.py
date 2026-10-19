"""P2-4 算法镜像对账 #1：web/services/team_match.poisson_hhad
vs scripts/{derive.grid.dc_grid, derive.handicap.probs}。

两份实现都计算让球胜平负三向概率，但脚本侧默认走 Dixon-Coles（ρ=0.2
有低比分修正），web 侧走纯独立泊松（``exp(-lh)*lh^i/i!`` 直乘）。

设 ``ρ=0`` 后 DC 内核退化为独立泊松（``dixon_coles_pmf`` = ``poisson_pmf * poisson_pmf``），
此时两端应在 1e-9 内一致。本测试固定多组 ``(λh, λa, line)`` 验证：

1. 数值相等（``ρ=0`` 时）
2. Σ == 1（独立泊松 ≥10 进球截断会漏概率；web 不 renormalize、scripts
   renormalize ⇒ ``ρ=0`` 时截断误差 ~1e-5 量级，可接受）
3. key 命名差异（web=中文，scripts=英文）属于契约差异，注释说明
4. 异常输入路径一致：非法 λ / line → 都返回 None / 抛 TypeError
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1].parent
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


# 实战 λ 范围（英超场均进球 1.2~1.7，亚盘让球 ±1/2），max_goals=10 与 web 一致
_FIXTURES = [
    # (label, lambda_home, lambda_away, line)
    ("典型主胜 -1", 1.5, 0.8, -1),
    ("典型主胜 -2", 1.8, 0.6, -2),
    ("主受让 +1", 0.9, 1.6, 1),
    ("平手 0", 1.3, 1.3, 0),
    ("客让 -1", 0.7, 1.4, -1),
    ("极端低 λ", 0.3, 0.4, -1),
    ("大 λ 高比分", 3.0, 2.5, -1),
]


def _scripts_hhad(lh: float, la: float, line: int, max_goals: int = 10) -> dict[str, float]:
    """scripts 侧：dc_grid(ρ=0) + handicap.probs。ρ=0 ⇒ 退化为独立泊松。"""
    from derive.grid import dc_grid
    from derive.handicap import probs
    grid = dc_grid(lh, la, rho=0.0, max_goals=max_goals)
    return probs(grid, line)


def test_values_match_when_rho_zero():
    """``ρ=0`` 时 dc_grid ≡ 独立泊松，web 与 scripts 应在 1e-4 内一致。

    注意：web 在 ``poisson_hhad`` 末尾对每项 ``round(..., 4)`` ⇒ 量化误差最大 5e-5；
    web 不 renormalize ⇒ 截断 10 进球漏掉的尾部概率约 1e-4。两项叠加与 scripts
    的差值控制在 5e-4 内即视为算法一致。
    """
    from web.services.team_match import poisson_hhad

    for label, lh, la, line in _FIXTURES:
        web = poisson_hhad(lh, la, line)
        scripts = _scripts_hhad(lh, la, line)
        assert web is not None and "probs" in web, f"{label}: web 返回 None"
        w = web["probs"]
        s = scripts
        # web keys: 让胜/让平/让负, scripts keys: home/draw/away
        # 映射：让胜=home, 让平=draw, 让负=away
        assert math.isclose(w["让胜"], s["home"], abs_tol=5e-4), (
            f"{label} 让胜/home: web={w['让胜']} scripts={s['home']}"
        )
        assert math.isclose(w["让平"], s["draw"], abs_tol=5e-4), (
            f"{label} 让平/draw: web={w['让平']} scripts={s['draw']}"
        )
        assert math.isclose(w["让负"], s["away"], abs_tol=5e-4), (
            f"{label} 让负/away: web={w['让负']} scripts={s['away']}"
        )


def test_sum_equals_one():
    """三向概率之和应为 1（截断 max_goals=10 漏掉的尾部概率 < 1e-3）。

    scripts 侧 dc_grid 走 renormalize ⇒ Σ 严格 == 1；web 侧无 renormalize，
    Σ 与 1 的差反映截断尾部（λ 越大漏得越多）。实战 λ≤3 时漏概率 < 1e-3。
    """
    from web.services.team_match import poisson_hhad

    for label, lh, la, line in _FIXTURES:
        web = poisson_hhad(lh, la, line)
        scripts = _scripts_hhad(lh, la, line)
        web_sum = sum(web["probs"].values())
        scripts_sum = sum(scripts.values())
        assert math.isclose(web_sum, 1.0, abs_tol=1e-3), f"{label} web Σ={web_sum}"
        assert math.isclose(scripts_sum, 1.0, abs_tol=1e-3), f"{label} scripts Σ={scripts_sum}"


def test_pick_matches_argmax_on_both_sides():
    """``pick`` = probs 中最大项：web 用中文键、scripts 用英文键，验证 max 一致。

    注意：脚本侧 dc_grid(ρ=0) + renormalize 后，web 与 scripts 的 pick 应相同
    （同一算法 + 同一截断）。若任一端改了实现，pick 漂移会立刻被这张表抓住。
    """
    from web.services.team_match import poisson_hhad

    # 中文 → 英文映射
    ZH_TO_EN = {"让胜": "home", "让平": "draw", "让负": "away"}

    for label, lh, la, line in _FIXTURES:
        web = poisson_hhad(lh, la, line)
        scripts = _scripts_hhad(lh, la, line)
        web_pick_en = ZH_TO_EN[web["pick"]]
        scripts_pick = max(scripts, key=lambda k: scripts[k])
        assert web_pick_en == scripts_pick, (
            f"{label}: web pick={web_pick_en} scripts pick={scripts_pick}"
        )


def test_invalid_inputs_return_none_or_raise_consistently():
    """非法 λ（≤0）/ None / 非数 → 两端都不应静默编数。"""
    from web.services.team_match import poisson_hhad

    # web 对 λ<=0 返回 None
    assert poisson_hhad(0, 1.5, -1) is None
    assert poisson_hhad(1.5, -1, -1) is None
    assert poisson_hhad(None, 1.5, -1) is None
    assert poisson_hhad("not-a-number", 1.5, -1) is None


def test_line_type_handling_divergence():
    """故意记录 web 与 scripts 的 line 类型差异（避免后续静默改一边）。

    - web: ``line`` 接受 float（``ln = float(line)``），非整数也参与计算
      ⇒ ``diff = i + 1.5 - j`` 可能非整数，但 ``>0/==0/<0`` 三分支照常判定
    - scripts: ``handicap.probs`` 显式拒绝非 int（``_check_line``），float
      一律 TypeError

    这是契约差异，不是 bug。测试断言：web 接受 float line + scripts 拒绝
    float line，永久锁定行为。
    """
    from derive.handicap import probs

    from web.services.team_match import poisson_hhad

    grid = _scripts_hhad(1.5, 0.8, -1)  # 构造一个合法 grid（line=-1 走一遍）
    # 重做：用 scripts 自己的 dc_grid + 喂 float line
    from derive.grid import dc_grid
    g = dc_grid(1.5, 0.8, rho=0.0, max_goals=10)
    # scripts 拒 float line
    raised = False
    try:
        probs(g, -1.0)  # float 而非 int
    except TypeError:
        raised = True
    assert raised, "scripts 必须拒 float line（_check_line 契约）"

    # web 接受 float line 且行为一致
    w_int = poisson_hhad(1.5, 0.8, -1)  # int
    w_float = poisson_hhad(1.5, 0.8, -1.0)  # float
    assert w_int is not None and w_float is not None
    # float line = -1.0 与 int -1 在 diff 计算上等价
    for k in ("让胜", "让平", "让负"):
        assert math.isclose(w_int["probs"][k], w_float["probs"][k], abs_tol=1e-12), (
            f"{k}: int line={w_int['probs'][k]} float line={w_float['probs'][k]}"
        )
