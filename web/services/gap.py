"""web.services.gap —— 错位三分量的 web 封装（建议 5 子项 ①）。

与 `web/services/recommend.py` 同一套访问约定：本文件**顶层不 import psycopg / scripts**
（T5 免 psycopg 环境），只在函数内延迟 import；模块级自举一次 `scripts/`。

**纪律**：老端点 `/api/jc/gap` 的**既有字段一字未动** —— 本模块只**增量附加**三个新字段
（`gap_decide` / `gap_now` / `drift_pp`），用来把「真实错位」与「盘口漂移」拆开
（路线图 §6.7.2 ①）。

读不出来一律**降级为 null**（页面显示「—」，不 5xx、不编数）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("web.gap")

# T3 启动链 `PYTHONPATH=. uvicorn web.api:app` 不含 scripts/：自举一次（追加到末尾）。
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.append(_SCRIPTS_DIR)


def attach_triples(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给错位榜 items **增量**附上三分量。

    - 任何异常 ⇒ 原样返回（**不 5xx**），并把新字段置 `None`（页面显示「—」）。
    - 只查榜上那几场（`match_num`）⇒ 不做全表扫描。
    """
    nums = [it.get("match_num") for it in items if it.get("match_num") is not None]
    if not nums:
        for it in items:
            it.setdefault("gap_decide", None)
            it.setdefault("gap_now", None)
            it.setdefault("drift_pp", None)
        return items
    try:
        from market.gap_calc import compute_gaps
        from store import pg

        with pg.read_conn("ro") as conn:
            gaps = compute_gaps(conn, match_nums=nums)
    except Exception as exc:  # noqa: BLE001 - 只读增强，任何异常降级
        logger.warning("web.gap 读错位三分量失败（降级：不附加）：%s", exc)
        for it in items:
            it["gap_decide"] = None
            it["gap_now"] = None
            it["drift_pp"] = None
        return items

    by_num = {g.get("match_num"): g for g in gaps}
    for it in items:
        g = by_num.get(it.get("match_num"))
        it["gap_decide"] = g.get("gap_decide") if g else None
        it["gap_now"] = g.get("gap_now") if g else None
        it["drift_pp"] = g.get("drift_pp") if g else None
        it["p_decide"] = g.get("p_decide") if g else None
        it["p_now"] = g.get("p_now") if g else None
    return items


def triples_note() -> str:
    """口径说明（页面/接口都带这句，避免数字被误读）。"""
    return ("gap_decide = 决策时刻盘口去水概率 vs 模型 p（固定量，决策当时就算出来了）；"
            "gap_now = 当前盘口去水概率 vs 同一个模型 p（滚动量）；"
            "drift_pp = 当前 − 决策时 的逐侧隐含概率差（百分点）⇒ 盘口自己走了多少。"
            "两者混在一起就没法归因 —— 老 gap 字段保留不动，新字段是增量。")
