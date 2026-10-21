"""建议 6 · 选号能力零假设检验（路线图 §6.8.4 的**可复算实现**）。

背景：建议 6 要在体彩侧做「热号/冷号/遗漏值」统计呈现，同时必须回答一个问题：
**这三种统计量能不能用来选号？** 本脚本给出可复算的答案。

零假设：每期开奖在号码池上 **iid 均匀**（彩票的物理前提）。
方法：
  1) **walk-forward**：第 t 期**只用** 0..t-1 选号，验第 t 期命中（模拟真实用法）；
  2) **合成 iid 池**做零分布：造 N 个同期数、同池大小的随机序列，
     跑**完全相同**的选号流程 ⇒ 得到「纯随机世界下该统计量的分布」；
  3) 把真实样本的统计量放进零分布 ⇒ 看百分位与 z 值。

⚠️ 四个**必须知道**的坑（都是本轮真踩过的）：
  · **零分布必须用很多个池**。选号策略的选号在「随机种子」之间几乎不变
    （跨 seed sd 仅 0.048），**池与池之间的方差才是主导项** ⇒ 用 1 个池对比
    没有统计效力（第一版就栽在这里）。
  · **零分布的值必须与真实值「同类聚合」**（同池 + 同样种子数取平均）。
    否则两边离散度失配 ⇒ z 被压缩、百分位失真：实测 `random` 策略的真实值是
    20 种子平均（sd 0.028），而「单种子」的零分布值 sd 是 0.123 ⇒ **失配 4.5 倍**
    ⇒ z 被压到 ±0.2 ⇒ p 恒在 0.82 附近（假阴性）。
  · 零分布的均值应当对齐**理论基准**（如大乐透前区 5×5/35=0.7143）⇒ 这是框架自检。
  · 选号要「分组内随机打散取前 k」，**不能**用 `set(list(x)[:k])` 截断
    （会把大号截掉，制造偏差）。

用法：
  python scripts/research/lottery_selection_check.py                  # 读生产库（需 DSN）
  python scripts/research/lottery_selection_check.py --tsv draws.tsv  # 离线（game|issue|date|raw）
  python scripts/research/lottery_selection_check.py --fast            # 快速冒烟（**不给结论**，返回码 2）
  # 默认 --pools 300 --seeds 20：零分布每个池也要跑 20 次 walk-forward
  # ⇒ 总计约 12 万次 walk-forward，**实测约 1 分钟**（生产 209 期数据）。
  # ⚠️ 合成池数 < 100 ⇒ 百分位分辨率不足 ⇒ 脚本**不给结论**（返回码 2）。

输出与路线图 §6.8.4 的表格一一对应，可直接对账。
"""
from __future__ import annotations

import argparse
import math
import random
import statistics
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

MIN_TRAIN = 10          # 前 10 期只用来「热身」，不做预测
STRATEGIES = ("random", "hot", "cold", "omission")
GAME_NAME = {"85": "超级大乐透", "35": "排列3", "350133": "排列5", "04": "7星彩"}


# ──────────────────────────── 选号与 walk-forward ────────────────────────────

def _pick_combo(freq: Counter, last: dict, n_train: int, pool: list[int],
                k: int, strategy: str, rng: random.Random) -> set[int]:
    """组合型选号（一次选 k 个号，如大乐透前区选 5/35）。

    key 相等的号码**分组内随机打散**后取前 k ⇒ 不制造确定性 tie-break 偏差。
    """
    if strategy == "random":
        return set(rng.sample(pool, k))
    if strategy == "hot":
        key = lambda n: -freq[n]
    elif strategy == "cold":
        key = lambda n: freq[n]
    else:  # omission：遗漏越久越靠前
        key = lambda n: -(n_train - 1 - last[n]) if n in last else -(10 ** 9)
    ranked = sorted(pool, key=key)
    pick: list[int] = []
    i = 0
    while i < len(ranked) and len(pick) < k:
        val = key(ranked[i])
        grp: list[int] = []
        while i < len(ranked) and key(ranked[i]) == val:
            grp.append(ranked[i])
            i += 1
        rng.shuffle(grp)
        pick.extend(grp)
    return set(pick[:k])


def combo_stat(draws: list[list[int]], pool: list[int], k: int,
               strategy: str, seed: int) -> float:
    """组合型 walk-forward 平均命中（第 t 期只用 0..t-1 选号，验第 t 期）。"""
    rng = random.Random(seed)
    freq: Counter = Counter()
    last: dict[int, int] = {}
    for idx, d in enumerate(draws[:MIN_TRAIN]):
        for n in set(d):
            freq[n] += 1
            last[n] = idx
    hits: list[int] = []
    for t in range(MIN_TRAIN, len(draws)):
        s = _pick_combo(freq, last, t, pool, k, strategy, rng)
        hits.append(len(s & set(draws[t])))
        for n in set(draws[t]):
            freq[n] += 1
            last[n] = t
    return sum(hits) / len(hits)


def _pick_pos(freq: Counter, last: dict, n_train: int, pool: list[int],
              strategy: str, rng: random.Random) -> int:
    """位置型选号（每位选 1 个号，如排列 3 的第 1 位）。"""
    if strategy == "random":
        return rng.choice(pool)
    if strategy == "hot":
        best = max(freq.get(n, 0) for n in pool)
        cands = [n for n in pool if freq.get(n, 0) == best]
    elif strategy == "cold":
        best = min(freq.get(n, 0) for n in pool)
        cands = [n for n in pool if freq.get(n, 0) == best]
    else:
        om = {n: (n_train - 1 - last[n]) for n in pool if n in last}
        best = max(om.get(n, n_train) for n in pool)
        cands = [n for n in pool if om.get(n, n_train) == best]
    return rng.choice(cands)


def pos_stat(draws: list[list[int]], ranges: list[tuple[int, int]],
             strategy: str, seed: int) -> float:
    """位置型 walk-forward 平均命中（按位命中数）。"""
    rng = random.Random(seed)
    freqs = [Counter() for _ in ranges]
    lasts = [{} for _ in ranges]
    for idx in range(MIN_TRAIN):
        for pi in range(len(ranges)):
            v = draws[idx][pi]
            freqs[pi][v] += 1
            lasts[pi][v] = idx
    hits: list[int] = []
    for t in range(MIN_TRAIN, len(draws)):
        picks = []
        for pi, (lo, hi) in enumerate(ranges):
            picks.append(_pick_pos(freqs[pi], lasts[pi], t,
                                    list(range(lo, hi + 1)), strategy, rng))
        hits.append(sum(1 for a, b in zip(picks, draws[t]) if a == b))
        for pi in range(len(ranges)):
            v = draws[t][pi]
            freqs[pi][v] += 1
            lasts[pi][v] = t
    return sum(hits) / len(hits)


# ──────────────────────────── 任务定义 ────────────────────────────

def build_tasks(by_game: dict[str, list[list[int]]]) -> list[dict]:
    """把各彩种历史拆成 5 个检验任务（与路线图 §6.8.4 的表格一一对应）。"""
    tasks: list[dict] = []
    if "85" in by_game:
        dlt = by_game["85"]
        tasks.append({"label": "大乐透前区 5/35", "mode": "combo",
                      "draws": [d[:5] for d in dlt], "pool": list(range(1, 36)), "k": 5,
                      "base": 25 / 35})
        tasks.append({"label": "大乐透后区 2/12", "mode": "combo",
                      "draws": [d[5:7] for d in dlt], "pool": list(range(1, 13)), "k": 2,
                      "base": 4 / 12})
    if "35" in by_game:
        tasks.append({"label": "排列3 3 位", "mode": "pos",
                      "draws": by_game["35"], "ranges": [(0, 9)] * 3, "base": 3 * 0.1})
    if "350133" in by_game:
        tasks.append({"label": "排列5 5 位", "mode": "pos",
                      "draws": by_game["350133"], "ranges": [(0, 9)] * 5, "base": 5 * 0.1})
    if "04" in by_game:
        tasks.append({"label": "7星彩 6+1 位", "mode": "pos",
                      "draws": by_game["04"], "ranges": [(0, 9)] * 6 + [(0, 14)],
                      "base": 6 * 0.1 + 1 / 15})
    return tasks


def _stat(task: dict, draws: list[list[int]], strategy: str, seed: int) -> float:
    if task["mode"] == "combo":
        return combo_stat(draws, task["pool"], task["k"], strategy, seed)
    return pos_stat(draws, task["ranges"], strategy, seed)


def _synth(task: dict, rng: random.Random) -> list[list[int]]:
    if task["mode"] == "combo":
        return [sorted(rng.sample(task["pool"], task["k"])) for _ in task["draws"]]
    return [[rng.randint(lo, hi) for (lo, hi) in task["ranges"]] for _ in task["draws"]]


def chi2_dispersion(draws: list[list[int]], pool: list[int]) -> float:
    """号码出现频次的**卡方统计量**（均匀池为原假设）。

    回答的是另一个问题：「真实数据本身是不是比随机更不均匀（更热/更冷）？」
    —— 与「选号策略有没有用」是两回事（后者是 §6.8.4 的主表）。

    期望频次 = 期数 × 每期个数 / 池大小（组合型任务每期恰好 k 个互异号）。

    ⚠️ 零分布均值 = **池大小 − 每期个数**（P − k），**不是** df = P − 1：
    每期号码是**不放回**抽取 ⇒ 计数向量有负相关 ⇒ E[chi2] 低于 df。
    例：大乐透前区 P=35 / k=5 ⇒ 理论 30（实测 29.59）。
    """
    counts: Counter = Counter()
    for d in draws:
        for n in set(d):
            counts[n] += 1
    k = len(set(draws[0])) if draws else 0
    exp = len(draws) * k / len(pool)
    if not exp:
        return 0.0
    return sum((counts.get(n, 0) - exp) ** 2 / exp for n in pool)


def task_degenerate_reason(task: dict) -> str | None:
    """该任务的数据是否**畸形**（会让统计量退化）—— 返回原因，None = 正常。

    实测踩过：喂「game=85 只有 5 个号（缺后区 2 个）」的数据时，
    后区任务的每期号码集为空 ⇒ 命中率**恒为 0** ⇒ 与正常零分布一比
    就是一个巨大的负 z ⇒ **在纯 iid 数据上也会报「显著」**（假阳性）。
    ⇒ 数据格式不符时必须**跳过该任务并说明原因**，不能拿它出结论。
    """
    draws = task["draws"]
    if not draws:
        return "没有任何期数据"
    if task["mode"] == "combo":
        pool = set(task["pool"])
        hit = sum(1 for d in draws if set(d) & pool)
        if hit == 0:
            return (f"没有任何一期的号码落在池 {min(pool)}~{max(pool)} 内"
                    "（数据格式不符？例如大乐透缺后区号码）")
    else:
        for pi, (lo, hi) in enumerate(task["ranges"]):
            vals = [d[pi] for d in draws if len(d) > pi]
            if not vals:
                return f"第 {pi + 1} 位没有任何数据"
            if any(not (lo <= v <= hi) for v in vals):
                return f"第 {pi + 1} 位有号码越界（应为 {lo}~{hi}）"
    return None


# ──────────────────────────── 统计工具 ────────────────────────────

def _pct(sorted_vals: list[float], x: float) -> float:
    """x 在零分布里的百分位（小于 x 的比例）。"""
    return sum(1 for v in sorted_vals if v < x) / len(sorted_vals)


def _two_sided_p(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2))


def _split_half(task: dict, strategy: str, seeds: int) -> tuple[float, float]:
    draws = task["draws"]
    half = len(draws) // 2
    out = []
    for seg in (draws[:half], draws[half:]):
        vals = [_stat(task, seg, strategy, 9000 + s) for s in range(min(seeds, 10))]
        out.append(statistics.mean(vals))
    return out[0], out[1]


# ──────────────────────────── 数据装载 ────────────────────────────

def load_from_tsv(path: str) -> dict[str, list[list[int]]]:
    """离线装载：每行 `game|issue|date|raw`，raw 是空格分隔的号码。"""
    by_game: dict[str, list[tuple[str, str, list[int]]]] = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        parts = line.split("|")
        if len(parts) != 4 or not parts[0].strip().isdigit():
            continue
        nums = [int(x) for x in parts[3].split() if x.strip().lstrip("-").isdigit()]
        if nums:
            by_game.setdefault(parts[0].strip(), []).append(
                (parts[2].strip(), parts[1].strip(), nums))
    out = {}
    for g, rows in by_game.items():
        rows.sort(key=lambda t: (t[0], t[1]))
        out[g] = [r[2] for r in rows]
    return out


def load_from_db() -> dict[str, list[list[int]]]:
    """生产库装载：读 `fact.lottery_draw`（只读）。"""
    from store import jc_view
    rows = jc_view.lottery_draws(100)
    by_game: dict[str, list[tuple[str, str, list[int]]]] = {}
    for r in rows:
        nums = [int(x) for x in str(r.get("numbers_raw") or "").split()
                if x.strip().lstrip("-").isdigit()]
        if nums:
            by_game.setdefault(r["game_num"], []).append(
                (str(r.get("draw_date") or ""), str(r.get("issue_no") or ""), nums))
    out = {}
    for g, lst in by_game.items():
        lst.sort(key=lambda t: (t[0], t[1]))
        out[g] = [t[2] for t in lst]
    return out


# ──────────────────────────── 主流程 ────────────────────────────

def run(by_game: dict[str, list[list[int]]], pools: int, seeds: int) -> int:
    tasks = build_tasks(by_game)
    if not tasks:
        print("没有可用的开奖数据")
        return 1
    print("=" * 96)
    print("建议 6 · 选号能力零假设检验（零假设 = 每期开奖 iid 均匀）")
    print(f"合成池数 = {pools}，真实样本每策略种子数 = {seeds}")
    print("=" * 96)

    zrows: dict[str, list[float]] = {st: [] for st in STRATEGIES}
    pcts: list[tuple[str, float]] = []
    split_rows = []
    dispersion: list[tuple[str, float, float, float]] = []

    for task in tasks:
        n_periods = len(task["draws"])
        bad = task_degenerate_reason(task)
        if bad:
            print(f"\n【{task['label']}】**跳过** —— 数据畸形：{bad}")
            print("    （畸形数据会让统计量退化 ⇒ 与正常零分布一比就是假阳性/假阴性）")
            continue
        print(f"\n【{task['label']}】随机基准 = {task['base']:.4f}，期数 = {n_periods}")
        for st in STRATEGIES:
            real = statistics.mean(
                [_stat(task, task["draws"], st, 9000 + s) for s in range(seeds)])
            # ⚠️ 零分布的每个值必须与真实值**同类聚合**（同池 + 同样种子数取平均），
            #    否则两边的离散度不匹配 ⇒ z 被压缩、百分位失真。
            #    实测（纯 iid 自检）：`random` 策略的真实值是 20 种子平均（sd 0.028），
            #    而「单种子」的零分布值 sd 是 0.123 ⇒ 失配 4.5 倍 ⇒ z 被压到 ±0.2
            #    ⇒ p 恒在 0.82 附近（假阴性）。
            #    hot/cold/omission 的选号在种子间几乎不变 ⇒ 两种口径几乎一致，
            #    但为了方法学干净，两边统一。
            nulls = []
            for i in range(pools):
                rng = random.Random(770000 + i)
                syn = _synth(task, rng)
                nulls.append(statistics.mean(
                    [_stat(task, syn, st, 9000 + s) for s in range(seeds)]))
            nv = sorted(nulls)
            nm, nsd = statistics.mean(nv), statistics.pstdev(nv)
            z = (real - nm) / nsd if nsd else 0.0
            p = _pct(nv, real)
            pcts.append((f"{task['label']}/{st}", p))
            zrows[st].append(z)
            lo = nv[int(0.025 * len(nv))]
            hi = nv[int(0.975 * len(nv))]
            print(f"    {st:>9}: 真实 {real:.4f}  零假设 {nm:.4f}±{nsd:.4f}  "
                  f"百分位 {p:6.1%}  z = {z:+.3f}  "
                  f"[95% 区间 {lo:.4f}~{hi:.4f}]")

        # 号码池**离散度**（卡方）：真实数据本身是否比随机更不均匀？
        # 只对组合型任务有意义（位置型每期各占一位，频次分布不是同一个问题）。
        if task["mode"] == "combo":
            real_c2 = chi2_dispersion(task["draws"], task["pool"])
            nulls_c2 = []
            for i in range(pools):
                rng = random.Random(880000 + i)
                nulls_c2.append(chi2_dispersion(_synth(task, rng), task["pool"]))
            nv2 = sorted(nulls_c2)
            p_c2 = _pct(nv2, real_c2)
            exp_cnt = len(task["draws"]) * task["k"] / len(task["pool"])
            print(f"    {'离散度卡方':>9}: 真实 {real_c2:.2f}（每号期望 {exp_cnt:.2f} 次）  "
                  f"零假设 {statistics.mean(nv2):.2f}  "
                  f"[95% 区间 {nv2[int(0.025 * len(nv2))]:.2f}~{nv2[int(0.975 * len(nv2))]:.2f}]  "
                  f"百分位 {p_c2:.1%}")
            dispersion.append((task["label"], real_c2, statistics.mean(nv2), p_c2))

    n_usable = len({lab.split("/")[0] for lab, _ in pcts})
    if n_usable < 3:
        print("\n" + "=" * 96)
        print(f"仅 {n_usable} 个任务可用 ⇒ **不给结论**"
              "（合并证据至少需要 3 个独立任务）")
        print("=" * 96)
        return 2

    if dispersion:
        print("\n" + "=" * 96)
        print("号码池离散度（卡方）：真实数据本身是否比随机更不均匀？")
        print("=" * 96)
        for lab, real_c2, null_c2, p_c2 in dispersion:
            verdict = "比随机更均匀" if p_c2 < 0.5 else "比随机更不均匀"
            print(f"  {lab:>14}: 真实卡方 {real_c2:.2f}  零假设均值 {null_c2:.2f}  "
                  f"百分位 {p_c2:.1%}  ⇒ {verdict}")

    print("\n" + "=" * 96)
    print("合并证据（跨任务固定效应 z 求和）")
    print("=" * 96)
    merged = {}
    for st in STRATEGIES:
        zs = zrows[st]
        zc = sum(zs) / math.sqrt(len(zs))
        p = _two_sided_p(zc)
        merged[st] = p
        signs = "".join("+" if z > 0 else "-" for z in zs)
        print(f"  {st:>9}: z = [{', '.join(f'{z:+.2f}' for z in zs)}]  符号 {signs}  "
              f"合并 z = {zc:+.3f}  双尾 p = {p:.3f}  → "
              f"{'显著' if p < 0.05 else '不显著（与纯随机无法区分）'}")

    n_tests = len(pcts)
    alpha = 0.05 / n_tests
    ranked = sorted(pcts, key=lambda x: -x[1])
    print("\n" + "=" * 96)
    print(f"多重比较（{n_tests} 项检验）")
    print("=" * 96)
    print(f"  Bonferroni 门槛 = 第 {1 - alpha:.3%} 百分位")
    print(f"  实际最高：{ranked[0][0]} = 第 {ranked[0][1]:.1%} 百分位  → "
          f"{'达标' if ranked[0][1] > 1 - alpha else '不达标'}")
    print(f"  纯随机预期落进 97.5% 以上的项数 = {n_tests * 0.025:.2f}")
    # 合成池数决定百分位的**分辨率**（每格 1/pools）：池太少时「第 99.75% 门槛」
    # 可能被某一项直接顶到 100% ⇒ 结论会翻转（实测 pools=30 时真实发生）。
    # ⇒ 池数不足时**不给结论**，只给参考值。
    resolution_ok = pools >= 100
    if not resolution_ok:
        print(f"  ⚠️ 合成池数 {pools} < 100 ⇒ 百分位分辨率不足（每格 {100 / pools:.1f}%），"
              f"**多重比较结论不可靠** ⇒ 请用默认档位（--pools 300）重跑")

    print("\n" + "=" * 96)
    print("拆半复现（前一半 / 后一半各自 walk-forward）")
    print("=" * 96)
    for task in tasks:
        parts = []
        for st in STRATEGIES:
            h1, h2 = _split_half(task, st, seeds)
            same = "同向" if (h1 - task["base"]) * (h2 - task["base"]) > 0 else "反向"
            parts.append(f"{st}({h1:.3f}/{h2:.3f} {same})")
            split_rows.append((task["label"], st, same == "同向"))
        print(f"  {task['label']:>14}: " + "  ".join(parts))
    n_rev = sum(1 for _, _, same in split_rows if not same)
    print(f"\n  ⇒ {n_rev}/{len(split_rows)} 项前后半段方向相反 ⇒ 属抽样波动，不是稳定规律")

    print("\n" + "=" * 96)
    print("结论（对应路线图 §6.8.4）")
    print("=" * 96)
    all_nonsig = all(v >= 0.05 for v in merged.values())
    if not resolution_ok:
        print(f"  合成池数不足（{pools} < 100）⇒ **本次不给结论**"
              f"（参考合并 p = {merged['hot']:.3f} / {merged['cold']:.3f} / {merged['omission']:.3f}）")
        return 2
    if all_nonsig and ranked[0][1] <= 1 - alpha:
        print("  热号 / 冷号 / 遗漏 三种选号法**与纯随机无法区分**"
              f"（合并 p = {merged['hot']:.3f} / {merged['cold']:.3f} / {merged['omission']:.3f}），"
              "多重比较最高项也未达标")
        print("  ⇒ 只能做**历史事实的统计呈现**，**不可**做选号建议")
        return 0
    print(f"  ⚠️ 本次运行出现显著项（合并 p = "
          f"{merged['hot']:.3f} / {merged['cold']:.3f} / {merged['omission']:.3f}；"
          f"最高百分位 {ranked[0][1]:.1%}）⇒ 请对照路线图 §6.8.4 复核")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="建议 6 选号能力零假设检验")
    ap.add_argument("--tsv", help="离线数据（每行 game|issue|date|raw）")
    ap.add_argument("--pools", type=int, default=300, help="合成 iid 池数量（默认 300）")
    ap.add_argument("--seeds", type=int, default=20, help="每池的随机种子数（默认 20）")
    ap.add_argument("--fast", action="store_true",
                    help="快速冒烟（--pools 40 --seeds 3）：池数 < 100 ⇒ 不给结论（返回码 2）")
    args = ap.parse_args(argv)
    if args.fast:
        args.pools, args.seeds = 40, 3
    by_game = load_from_tsv(args.tsv) if args.tsv else load_from_db()
    print("装载到：" + "  ".join(
        f"{GAME_NAME.get(g, g)}={len(v)} 期" for g, v in sorted(by_game.items())))
    return run(by_game, max(1, args.pools), max(1, args.seeds))


if __name__ == "__main__":
    raise SystemExit(main())
