"""建议 6 · 零假设检验脚本（`scripts/research/lottery_selection_check.py`）的单测。

目的：让路线图 §6.8.4 的结论**可复算**，并保证脚本本身**没算错**。

覆盖点：
  · 框架自检：**合成 iid 池**上的零分布均值必须对齐**理论基准**
    （大乐透前区 5×5/35=0.7143、排列3 3×0.1=0.3）⇒ 框架没算错；
  · 真实样本的统计量能落进零分布并给出百分位 / z；
  · 合并证据的 z 求和与双尾 p 计算正确；
  · 选号实现**没有** `set(list(x)[:k])` 那种截断偏差
    （分组内打散 ⇒ 选号集合大小恒为 k、且不含越界号）；
  · 离线 TSV 装载按期升序（遗漏/选号对顺序敏感）。
"""
from __future__ import annotations

import importlib.util
import random
import statistics
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

SCRIPT = SCRIPTS / "research" / "lottery_selection_check.py"


def _load_mod():
    spec = importlib.util.spec_from_file_location("lottery_selection_check_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load_mod()


# ─────────────────────────── 纯函数口径 ───────────────────────────

def test_combo_pick_returns_exactly_k_distinct_numbers():
    """选号必须恰好 k 个**互不相同**且在池内的号（不能被截断成别的个数）。"""
    rng = random.Random(7)
    pool = list(range(1, 36))
    freq = Counter({n: 5 for n in range(1, 6)})
    last = {n: 0 for n in range(1, 6)}
    for strategy in ("random", "hot", "cold", "omission"):
        s = M._pick_combo(freq, last, 30, pool, 5, strategy, rng)
        assert len(s) == 5, f"{strategy} 选了 {len(s)} 个号（应为 5）"
        assert all(n in pool for n in s)


def test_combo_pick_hot_prefers_frequent_numbers():
    """热号选号：出现次数多的号必须被优先选中（分组打散不改变「频次优先」）。"""
    rng = random.Random(11)
    pool = list(range(1, 36))
    freq = Counter({1: 9, 2: 9, 3: 8, 4: 7})
    last = {n: 0 for n in (1, 2, 3, 4)}
    picked = set()
    for _ in range(30):
        picked |= M._pick_combo(freq, last, 30, pool, 5, "hot", rng)
    assert 1 in picked and 2 in picked
    assert 1 not in M._pick_combo(freq, last, 30, pool, 5, "cold", rng)


def test_combo_stat_is_deterministic_for_same_seed():
    """同 seed 必须完全可复现（否则结论没法对账）。"""
    draws = [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10], [1, 2, 3, 11, 12]] * 6
    pool = list(range(1, 36))
    a = M.combo_stat(draws, pool, 5, "hot", 42)
    b = M.combo_stat(draws, pool, 5, "hot", 42)
    assert a == b


def test_combo_stat_on_synthetic_iid_is_near_theoretical_baseline():
    """框架自检：iid 数据上的 hot 选号命中均值必须 ≈ 5×5/35 = 0.7143。

    这条是「检验框架本身没算错」的锚点：真实数据若偏离而合成池不偏离，
    才说明差异来自数据；两者都偏离 ⇒ 框架有问题。
    """
    pool = list(range(1, 36))
    rng = random.Random(2024)
    vals = []
    for i in range(40):
        syn = [sorted(rng.sample(pool, 5)) for _ in range(44)]
        vals.append(M.combo_stat(syn, pool, 5, "hot", 9000 + (i % 20)))
    mean = statistics.mean(vals)
    assert abs(mean - 25 / 35) < 0.05, f"零假设均值 {mean:.4f} 偏离理论基准 {25/35:.4f} 太多"


def test_pos_stat_on_synthetic_iid_is_near_baseline():
    """位置型框架自检：iid 数据上按位命中均值 ≈ 3×0.1 = 0.3。"""
    ranges = [(0, 9)] * 3
    rng = random.Random(2025)
    vals = []
    for i in range(40):
        syn = [[rng.randint(lo, hi) for (lo, hi) in ranges] for _ in range(61)]
        vals.append(M.pos_stat(syn, ranges, "hot", 9000 + (i % 20)))
    mean = statistics.mean(vals)
    assert abs(mean - 0.3) < 0.03, f"零假设均值 {mean:.4f} 偏离理论基准 0.3000 太多"


def test_percentile_and_two_sided_p_math():
    """百分位与双尾 p 的算术正确性。"""
    vals = [float(i) for i in range(101)]
    assert M._pct(vals, 50.5) == pytest.approx(51 / 101)
    assert M._pct(vals, 0.0) == 0.0
    assert M._pct(vals, 101.0) == 1.0
    assert abs(M._two_sided_p(0.0) - 1.0) < 1e-9
    assert abs(M._two_sided_p(1.959964) - 0.05) < 1e-4
    assert abs(M._two_sided_p(2.575829) - 0.01) < 1e-4


# ─────────────────────────── 数据装载 ───────────────────────────

def test_load_from_tsv_sorts_ascending_and_groups_by_game(tmp_path):
    """离线装载：按 (date, issue) **升序**、按 game 分组（选号对顺序敏感）。"""
    p = tmp_path / "draws.tsv"
    p.write_text(
        "85|002|2026-01-02|1 2 3 4 5 6 7\n"
        "85|001|2026-01-01|8 9 10 11 12 1 2\n"
        "35|003|2026-01-03|1 2 3\n"
        "85|003|2026-01-03|13 14 15 16 17 3 4\n",
        encoding="utf-8")
    by_game = M.load_from_tsv(str(p))
    assert set(by_game) == {"85", "35"}
    assert by_game["35"] == [[1, 2, 3]]
    assert by_game["85"] == [
        [8, 9, 10, 11, 12, 1, 2],      # 期号 001（最早）
        [1, 2, 3, 4, 5, 6, 7],         # 期号 002
        [13, 14, 15, 16, 17, 3, 4],    # 期号 003（最新）
    ]


def test_build_tasks_maps_each_game_to_expected_task():
    """4 个彩种 ⇒ 5 个检验任务（与路线图 §6.8.4 的表一一对应）。"""
    by_game = {
        "85": [[1, 2, 3, 4, 5, 6, 7]] * 30,
        "35": [[1, 2, 3]] * 30,
        "350133": [[1, 2, 3, 4, 5]] * 30,
        "04": [[1, 2, 3, 4, 5, 6, 7]] * 30,
    }
    tasks = M.build_tasks(by_game)
    labels = [t["label"] for t in tasks]
    assert labels == ["大乐透前区 5/35", "大乐透后区 2/12", "排列3 3 位",
                      "排列5 5 位", "7星彩 6+1 位"]
    assert tasks[0]["base"] == 25 / 35
    assert tasks[1]["base"] == 4 / 12
    assert tasks[2]["base"] == pytest.approx(0.3)
    assert tasks[3]["base"] == pytest.approx(0.5)
    assert tasks[4]["base"] == pytest.approx(6 * 0.1 + 1 / 15)
    # 7星彩特别号池是 0..14（**不是** 0..15）
    assert tasks[4]["ranges"][-1] == (0, 14)


# ─────────────────────────── 留一法自检（框架没算错） ───────────────────────────

def test_leave_one_out_z_is_standard_normal_on_pure_iid_data():
    """留一法自检：在**纯 iid** 数据上，z 必须是标准正态（均值 ≈ 0、sd ≈ 1）。

    这是「检验框架本身没算错」最硬的锚点：
    若 z 的均值偏离 0 或 sd 偏离 1，说明零分布/标准化的口径错了。
    **本轮真踩过**：真实值与零分布值不做「同类聚合」时离散度失配 4.5 倍
    ⇒ z 被压到 ±0.2 ⇒ p 恒在 0.82 附近（假阴性）。

    做法：造 60 个 iid 池，每个池与**其余 59 个池**的分布比（留一）⇒ z；
    再看 z 的均值 / sd / 百分位均值是否落在标准正态上。
    """
    pool = list(range(1, 36))
    rng = random.Random(31337)
    stats = []
    for _ in range(60):
        syn = [sorted(rng.sample(pool, 5)) for _ in range(44)]
        # 与生产口径一致：真实值也是「多种子平均」⇒ 两边同类聚合
        stats.append(statistics.mean(
            [M.combo_stat(syn, pool, 5, "random", 9000 + s) for s in range(5)]))
    zs = []
    for i in range(len(stats)):
        others = sorted(stats[:i] + stats[i + 1:])
        nm, nsd = statistics.mean(others), statistics.pstdev(others)
        zs.append((stats[i] - nm) / nsd)
    mean_z = statistics.mean(zs)
    sd_z = statistics.pstdev(zs)
    pcts = [M._pct(sorted(stats), s) for s in stats]
    mean_pct = statistics.mean(pcts)
    assert abs(mean_z) < 0.4, f"留一法 z 均值 {mean_z:+.3f} 偏离 0 太多"
    assert 0.5 < sd_z < 1.8, f"留一法 z 标准差 {sd_z:.3f} 偏离 1 太多"
    assert abs(mean_pct - 0.5) < 0.15, f"留一法百分位均值 {mean_pct:.3f} 偏离 0.5 太多"


# ─────────────────────────── 号码池离散度（卡方） ───────────────────────────

def test_chi2_dispersion_zero_for_perfectly_uniform_pool():
    """完全均匀的池 ⇒ 卡方 = 0（期望频次与实测频次逐号相等）。"""
    pool = list(range(1, 36))
    # 35 期、每期 5 个号 ⇒ 每号恰好出现 5 次 ⇒ 期望 35×5/35 = 5 ⇒ 卡方 0
    draws = []
    for i in range(0, 35, 5):
        draws.append(list(range(i + 1, i + 6)))
    assert M.chi2_dispersion(draws, pool) == pytest.approx(0.0)


def test_chi2_dispersion_null_mean_matches_theoretical_design_value():
    """框架自检：iid 合成池上的卡方均值应 ≈ **池大小 − 每期个数**（= 30）。

    ⚠️ **不是** df = 池大小 − 1 = 34！因为每期号码是**不放回**抽取的
    （每期 k 个互异号），计数向量有负相关 ⇒ E[chi2] = P − k 而不是 P − 1。
    实测 29.59 ≈ 30，**与文档里的零分布均值 30.0 吻合**。
    """
    pool = list(range(1, 36))
    k, n = 5, 44
    rng = random.Random(777)
    vals = []
    for _ in range(40):
        syn = [sorted(rng.sample(pool, k)) for _ in range(n)]
        vals.append(M.chi2_dispersion(syn, pool))
    mean = statistics.mean(vals)
    theoretical = len(pool) - k
    assert abs(mean - theoretical) < 1.5, (
        f"零假设卡方均值 {mean:.2f} 偏离设计理论值 {theoretical} 太多")


def test_chi2_dispersion_detects_skewed_pool():
    """偏斜的池（号码集中在少数号上）卡方必须显著大于均匀池。"""
    pool = list(range(1, 36))
    skewed = [[1, 2, 3, 4, 5]] * 44
    uniform = [list(range(i + 1, i + 6)) for i in range(0, 35, 5)][:44]
    assert M.chi2_dispersion(skewed, pool) > M.chi2_dispersion(uniform, pool)


# ─────────────────────────── 端到端（快速模式） ───────────────────────────

def test_run_end_to_end_on_synthetic_iid_pool_reports_no_signal(tmp_path, capsys):
    """端到端：喂**纯 iid 合成数据** ⇒ 必须得出「与纯随机无法区分」。

    这是对脚本整体逻辑最强的自检：合成数据按构造没有信号，
    若脚本在这里报「显著」，说明零分布/百分位/合并逻辑写错了。

    ⚠️ game=85 必须给 **7 个号**（前区 5 + 后区 2），否则后区任务数据畸形
    （命中率恒为 0）⇒ 假阳性（实测踩过，见脚本的 task_degenerate_reason）。
    """
    rng = random.Random(99)
    lines = []
    for game, k, n in (("85", 5, 44), ("35", 3, 61)):
        for i in range(n):
            if game == "85":
                nums = (sorted(rng.sample(range(1, 36), 5))
                        + sorted(rng.sample(range(1, 13), 2)))
            else:
                nums = [rng.randint(0, 9) for _ in range(3)]
            lines.append(f"{game}|{i:03d}|2026-01-01|{' '.join(map(str, nums))}")
    p = tmp_path / "syn.tsv"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc = M.main(["--tsv", str(p), "--pools", "120", "--seeds", "3"])
    out = capsys.readouterr().out
    assert "合并证据" in out
    assert "多重比较" in out
    assert "拆半复现" in out
    assert "跳过" not in out, "数据完整时不应有任务被跳过"
    assert rc == 0, f"纯 iid 数据上不应报显著（rc={rc}）\n{out[-1500:]}"
    assert "与纯随机无法区分" in out


def test_fast_mode_refuses_to_conclude_with_too_few_pools(tmp_path, capsys):
    """合成池数 < 100 ⇒ 百分位分辨率不足 ⇒ 脚本**不给结论**（返回码 2）。

    实测：pools=30 时某一项被直接顶到第 100 百分位 ⇒ 结论会翻转 ⇒ 必须挡住。
    """
    rng = random.Random(4242)
    lines = []
    for i in range(44):
        nums = (sorted(rng.sample(range(1, 36), 5))
                + sorted(rng.sample(range(1, 13), 2)))   # 大乐透：前区 5 + 后区 2
        lines.append(f"85|{i:03d}|2026-01-01|{' '.join(map(str, nums))}")
    for i in range(61):
        nums = [rng.randint(0, 9) for _ in range(3)]
        lines.append(f"35|{i:03d}|2026-01-01|{' '.join(map(str, nums))}")
    p = tmp_path / "syn.tsv"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc = M.main(["--tsv", str(p), "--pools", "30", "--seeds", "3"])
    out = capsys.readouterr().out
    assert rc == 2, f"池数不足时应当返回 2（不给结论），实际 {rc}"
    assert "百分位分辨率不足" in out
    assert "本次不给结论" in out
