"""CLV 基线报告：用 The Odds API 当前 NBA 盘口（收盘候选）+ bball 模型 胜率 → CLV 基线。

被 D9/本次打开后果：odds_api 已开（LEAGUE_SOURCE_ODDS_API=on + LEAGUE_ALLOW_PAID=on），
本脚本取 The Odds 当前 NBA h2h 盘口当市场（p_close），对 bball.predict_game 的模型 win_prob
算 CLV = ln(p_model / p_close)。

口径（≤红线）：
  · p_close = 双方向 h2h 去水概率（1/顺；两家 devig 用简单等比例归一，不做专业去水——注明基线近似）
  · 只在 p_model、p_close 都在 (0,1) 内才计 CLV，给不合法样本数（not 0.0/1.0 边界）
  · 覆盖= 能拿到 市场+模型 双边的场数 / 市场场数
  · 输出 JSON 落 scripts/clv_baseline.json + 打印人类可读表

用法：ODDS_API_KEY=... python -m clv_baseline [--ahead-days N] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bball.elo_bball import load_ratings  # noqa: E402
from bball.odds_api import fetch_odds_games, parse_odds  # noqa: E402
from bball.predictor import predict_game  # noqa: E402

_BJT = timezone.utc  # 报告只写 UTC 时刻，不本地化


def _devig_home(odds_home, odds_away):
    """h2h 双方向去水 → 主胜概率（等比例归一；二者都 >1 才算，否则 None）。"""
    try:
        oh, oa = float(odds_home), float(odds_away)
    except (TypeError, ValueError):
        return None
    if not (oh > 1 and oa > 1):
        return None
    inv = 1 / oh + 1 / oa
    if inv <= 0:
        return None
    return (1 / oh) / inv


def _clv(p_model, p_close):
    """ln(p_model/p_close)；都在 (0,1) 才算，否则 None。"""
    if not (isinstance(p_model, (int, float)) and isinstance(p_close, (int, float))):
        return None
    if not (0 < float(p_model) < 1 and 0 < float(p_close) < 1):
        return None
    try:
        return math.log(float(p_model) / float(p_close))
    except (ValueError, ZeroDivisionError):
        return None


def run(ahead_days: int = 5, out: Path | None = None) -> dict:
    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        raise SystemExit("ODDS_API_KEY 未设置（脚本取 .env，运行前设好）")

    games = fetch_odds_games(key, "basketball_nba", ahead_days)
    ratings = load_ratings()
    now = datetime.now(timezone.utc)

    rows = []
    skipped = {"no_h2h": 0, "no_model": 0}
    for g in games:
        home, away = g.get("home_team", ""), g.get("away_team", "")
        if not home or not away:
            continue
        odds_home, odds_away, _sp, _tot = parse_odds(g, home)
        p_close = _devig_home(odds_home, odds_away)
        if p_close is None:
            skipped["no_h2h"] += 1
            continue
        pred = predict_game(home, away, ratings, _sp, _tot, odds_home, None)
        p_model = pred.get("win_prob")
        if not isinstance(p_model, (int, float)) or not (0 < float(p_model) < 1):
            skipped["no_model"] += 1
            continue
        clv = _clv(p_model, p_close)
        rows.append({
            "home": pred.get("home"), "away": pred.get("away"),
            "commence": g.get("commence_time"),
            "p_model": round(float(p_model), 4), "p_close": round(float(p_close), 4),
            "clv": round(clv, 4) if clv is not None else None,
        })

    valid = [r for r in rows if r["clv"] is not None]
    mean_clv = (sum(r["clv"] for r in valid) / len(valid)) if valid else None
    report = {
        "title": "NBA CLV 基线（The Odds API 当前 h2h 盘口 ≈ 市场收盘）",
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "method": "clv=ln(p_model/p_close)；p_close=h2h 等比例去水（基线近似，非专业去水）",
        "games_with_market": len(rows) + skipped["no_h2h"],
        "games_with_model_and_market": len(rows),
        "valid_clv": len(valid),
        "skipped": skipped,
        "mean_clv": round(mean_clv, 4) if mean_clv is not None else None,
        "positive_clv_pct": round(100 * sum(1 for r in valid if r["clv"] > 0) / len(valid), 1) if valid else None,
        "rows": rows,
    }
    if out is not None:
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ahead-days", type=int, default=5)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = Path(a.out) if a.out else (Path(__file__).resolve().parent / "clv_baseline.json")
    rep = run(a.ahead_days, out)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    print(f"\nCLV 基线已写 {out}")


if __name__ == "__main__":
    main()