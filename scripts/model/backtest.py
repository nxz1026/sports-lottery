"""P0-BACKTEST1：预测 vs 真结果 —— 逐选项回测（纯计算与落库分离，真跑由队长执行）。

口径（照抄，不自创）：
  · label 一律取 `fact.fixture_result` 的 `source='api_football'` 行（设计 §6：比分 AF 权威、FD 只交叉核对），
    可见范围逐字照抄 model/walk.py `_SCOPE` 的训练谓词 `f.status='ft' AND f.round <> 'Relegation Round'`；
    无 label 的场**计入返回值的 `no_label`**，不静默丢。
  · 中奖项由派生层定：had/ttg/jqc 用各自 KEYS 常量、crs 用 `scores31._key`（官方 31 选项映射的唯一实现，
    复制一份必然漂移；derive 冻结 ⇒ 只 import）；jqc 两侧各恰一个中奖项。
  · `clv` 一律写 NULL：官方收盘价（CLV 的分母）还没有来源，写 0.0 会被 README §2-D2 的核心指标当真。
  · 幂等：同一 `src_run` 重跑先删本 run 旧行再写，**只删自己的 src_run**（analysis 是可重算层，别的 DELETE 禁止）。
  · 指标（score_options）与 store/query.py 的汇总 SQL 是同一口径的两处实现（跨层不互相 import），
    由 tests/test_model_backtest.py 在同批合成数据上逐值对账 —— 口径漂移会被测出来。
"""

from __future__ import annotations

from collections.abc import Sequence
from math import log

from core.log import logger
from derive import scores31
from derive.handicap import KEYS as HAD_KEYS
from derive.totals import KEYS as TTG_KEYS
from derive.totals import TOP as TTG_TOP

EPS = 1e-6  # log_loss 的显式下界：p 为精确 0 是真会发生的，不许悄悄 clamp 成别的值
JQC_KEYS = ("0", "1", "2", "3+")  # 竞彩官方 4 档（设计 §6.1）：一队进球 ≥3 全并进 "3+"
JQC_SIDES = ("h", "a")  # 与 model/sides.py 的 option_code 前缀同序

LABEL_SQL = """SELECT p.fixture_id, r.ft_h, r.ft_a
  FROM model.pred_fixture p
  JOIN fact.fixture f ON f.fixture_id = p.fixture_id
  JOIN fact.fixture_result r ON r.fixture_id = f.fixture_id AND r.source = 'api_football'
 WHERE p.run_id = %s AND f.status = 'ft' AND f.round <> 'Relegation Round'"""
MARKET_SQL = ("SELECT fixture_id, play_type, option_code, p FROM model.pred_market"
              " WHERE run_id = %s ORDER BY fixture_id, play_type, option_code")
DELETE_SQL = "DELETE FROM analysis.backtest_market WHERE src_run = %s"
INSERT_SQL = ("INSERT INTO analysis.backtest_market"
              " (fixture_id, play_type, option_code, p_pred, outcome, clv, src_run)"
              " VALUES (%s, %s, %s, %s, %s, NULL, %s)")  # clv 恒 NULL：还没有收盘价来源


def winners(play_type: str, ft_h: int, ft_a: int) -> list[str]:
    """本场本玩法的中奖 option_code；jqc 两个侧别各一个（长度 2），其余玩法恰 1。"""
    if play_type == "jqc":
        return [f"{side}:{JQC_KEYS[min(goals, 3)]}" for side, goals in zip(JQC_SIDES, (ft_h, ft_a))]
    if play_type == "had":
        return [HAD_KEYS[0 if ft_h > ft_a else 1 if ft_h == ft_a else 2]]
    if play_type == "ttg":
        return [TTG_KEYS[min(ft_h + ft_a, TTG_TOP)]]
    if play_type == "crs":
        return [scores31._key(ft_h, ft_a)]
    raise KeyError(f"玩法 {play_type!r} 没有判奖口径（未注册玩法不该出现在 pred_market）")


def score_options(rows: Sequence[tuple[float, int]], *, eps: float = EPS) -> dict:
    """一个 (场, 玩法[, jqc 侧别]) 组内**全部**选项的 Brier / log_loss / hit。

    Brier（多类）= Σ_i (p_i − o_i)²（含未中奖项）；log_loss = −ln(max(p_中奖, eps))，eps 显式回显
    （p=0 是真出现过的：ρ 收缩前的 1:1 格、官方不开盘的 crs 档 ⇒ 不许悄悄 clamp 成别的数）；
    hit = argmax(p) 是否中奖（并列取首现；多档玩法一律判最大概率选项，不许"命中任一非零概率"）。
    n=0 ⇒ 只有 {"n": 0, "eps": eps}，不崩；单选项组由 Σp=1 决定其必中 ⇒ 指标恒 0 且标 degenerate。
    """
    if not rows:
        return {"n": 0, "eps": eps}
    top_outcome = max(rows, key=lambda row: row[0])[1]  # 并列取首现（汇总 SQL 同分时顺序不定，见报告 §5-11）
    if len(rows) == 1:
        return {"n": 1, "brier": 0.0, "log_loss": 0.0, "hit": int(top_outcome), "eps": eps,
                "degenerate": True}
    p_win = max(p for p, outcome in rows if outcome == 1)  # 无中奖项＝组坏了：宁可炸，也不给假指标
    return {"n": len(rows), "brier": sum((p - outcome) ** 2 for p, outcome in rows),
            "log_loss": -log(max(p_win, eps)), "hit": int(top_outcome), "eps": eps, "degenerate": False}


def run(conn, src_run: int) -> dict:
    """一个 run 的回测：读 pred_market + label，逐 (场, 玩法, 选项) 写 analysis.backtest_market（幂等）。"""
    labels = {fid: (ft_h, ft_a) for fid, ft_h, ft_a in conn.execute(LABEL_SQL, (src_run,))}
    groups: dict[tuple[int, str], list[tuple[str, float]]] = {}
    for fixture_id, play, code, p in conn.execute(MARKET_SQL, (src_run,)):
        groups.setdefault((fixture_id, play), []).append((code, p))
    rows, plays, scored, unlabelled = [], {}, set(), set()
    for (fixture_id, play), options in groups.items():
        if fixture_id not in labels:
            unlabelled.add(fixture_id)
            continue
        hit = winners(play, *labels[fixture_id])
        if sum(1 for code, _ in options if code in hit) != len(hit):  # T3 前置：中奖项必须逐一对上
            raise ValueError(f"fixture {fixture_id} {play}: 中奖项 {hit} 与 pred_market 选项对不上")
        rows += [(fixture_id, play, code, p, int(code in hit), src_run) for code, p in options]
        plays[play] = plays.get(play, 0) + len(options)
        scored.add(fixture_id)
    conn.execute(DELETE_SQL, (src_run,))  # 幂等：只删本 run 旧行，别的 run 一行不碰
    with conn.cursor() as cur:
        cur.executemany(INSERT_SQL, rows)
    logger.info(f"[backtest] run={src_run} 写 {len(rows)} 行 / {len(scored)} 场；无 label {len(unlabelled)} 场")
    return {"run_id": src_run, "n_rows": len(rows), "n_fixtures": len(scored), "plays": plays,
            "no_label": len(unlabelled)}
