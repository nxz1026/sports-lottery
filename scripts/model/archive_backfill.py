"""建议 3 子项 ③ 的**回填**：把 `scripts/predictions/` 里保留的历史预测文件补进归档表。

为什么需要回填：归档是 2026-10-21 才上线的，而 `scripts/predictions/` 里还留着
2026-09-27 ~ 2026-10-20 的历史预测文件（实测 72 份 / 6 个联赛）⇒ 那些「当时的模型意见」
本来就在硬盘上，只是没进库。回填它们，CLV 今天就能算出真实样本（而不是从明天才开始积累）。

⚠️ 回填**只补模型侧**（p_model / 星级 / 置信度 / 队名），收盘价是**查出来的**（不用回填）
—— 收盘价在 `fact.jc_odds_history` 里，本来就在库。

幂等：归档表的唯一键是 (league_key, generated_at)，重跑不会产生重复行。

用法：
    PYTHONPATH=scripts python scripts/model/archive_backfill.py --dry     # 只报计划
    PYTHONPATH=scripts python scripts/model/archive_backfill.py --execute  # 真写库
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path

from core.log import logger
from store import pg

from model.archive import archive_output

PREDICTIONS_DIR = Path(__file__).resolve().parents[1] / "predictions"


def _league_of(name: str) -> str | None:
    """从文件名取联赛 key（`prediction_2026-10-20_09_epl.json` → 'epl'）。"""
    stem = name[:-len(".json")] if name.endswith(".json") else name
    if "_" not in stem:
        return None
    tail = stem.rsplit("_", 1)[-1]
    return tail if tail and not tail[0].isdigit() else None


def load_docs(directory: Path) -> list[tuple[str, str, dict]]:
    """读全部预测文件 → ``[(league_key, 文件名, 产出 dict)]``；坏文件跳过（不炸整批）。"""
    out: list[tuple[str, str, dict]] = []
    for path in sorted(glob.glob(str(directory / "prediction_*.json"))):
        name = os.path.basename(path)
        league = _league_of(name)
        if league is None:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:  # noqa: BLE001 - 坏文件跳过
            logger.warning("archive_backfill: 跳过坏文件 %s（%s）", name, exc)
            continue
        if not isinstance(data, dict) or not data.get("predictions"):
            continue
        out.append((league, name, data))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把历史预测文件回填进 model.pred_archive*")
    ap.add_argument("--dry", action="store_true", help="只报计划，不写库（默认行为）")
    ap.add_argument("--execute", action="store_true", help="真写库")
    ap.add_argument("--dir", type=Path, default=PREDICTIONS_DIR, help="预测文件目录")
    args = ap.parse_args(argv)

    docs = load_docs(args.dir)
    n_matches = sum(len(d.get("predictions") or []) for _, _, d in docs)
    logger.info("archive_backfill: 待回填 %d 份文件、合计 %d 场预测", len(docs), n_matches)
    for league, name, data in docs:
        logger.info("  %-14s %-40s %s  场次=%d", league, name,
                    data.get("generated_at"), len(data.get("predictions") or []))
    if not args.execute:
        logger.info("（只读模式，加 --execute 才写库）")
        return 0

    total_rows = total_linked = 0
    with pg.write_conn("app") as conn:
        for league, name, data in docs:
            res = archive_output(conn, league, data, source_file=name)
            total_rows += res["n_matches"]
            total_linked += res["n_jc_linked"]
    logger.info("archive_backfill: 回填完成，%d 场（关联竞彩 %d 场）", total_rows, total_linked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
