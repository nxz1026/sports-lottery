"""backfill_result_home_en.py — 一次性补齐旧 result_*.json 的 home_en/away_en 字段。

背景（web UX 审核 P0-A2 / 2026-10-11）：
  `scripts/core/output.py::save_results` 老版本只写 `home` / `away`（值取自 `home_en`），
  没单独写 `home_en` / `away_en`。reconciliation（`scripts/core/backtest.py`
  的 `_bk_stable_key`）只用 `home_en` / `away_en`，导致 0 个 result 命中、命中率永远 0%。

行为：
  1. 遍历 `scripts/results/result_*.json`
  2. 每个 match 若 `home_en` 缺失则用 `home` 填充；`away_en` 同理
  3. 落盘前先备份到 `scripts/results/.bak-20261011-{ts}/`
  4. 打印 before/after 摘要

幂等：第二次运行不会再改（字段已存在）。

不引入依赖（仅 stdlib），运维单次脚本，下次预测覆盖后即可删。
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# 项目根 = scripts/ 的父目录
PROJECT_ROOT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT_ROOT / "results"


def main() -> int:
    if not RESULTS_DIR.exists():
        print(f"[backfill] {RESULTS_DIR} 不存在，退出")
        return 1

    # 备份目录
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_root = RESULTS_DIR / f".bak-{ts}"
    backup_root.mkdir(parents=True, exist_ok=True)

    files = sorted(RESULTS_DIR.glob("result_*.json"))
    if not files:
        print("[backfill] 无 result_*.json 文件")
        return 0

    total_files = 0
    total_matches = 0
    total_filled_home = 0
    total_filled_away = 0
    total_already_ok = 0

    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[backfill] 跳过 {path.name}: {exc}", file=sys.stderr)
            continue

        matches = data.get("matches") or []
        file_filled = 0
        for m in matches:
            if not isinstance(m, dict):
                continue
            total_matches += 1
            need_save = False
            if not m.get("home_en"):
                m["home_en"] = m.get("home", "")
                total_filled_home += 1
                file_filled += 1
                need_save = True
            if not m.get("away_en"):
                m["away_en"] = m.get("away", "")
                total_filled_away += 1
                file_filled += 1
                need_save = True
            if not need_save:
                total_already_ok += 1
            elif need_save:
                pass  # 已在上面加

        if file_filled > 0:
            # 备份 + 落盘
            shutil.copy2(path, backup_root / path.name)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            total_files += 1
            print(f"[backfill] {path.name}: 填充 {file_filled} 处")
        else:
            print(f"[backfill] {path.name}: 已完整，跳过")

    print("---")
    print(f"[backfill] 文件总数：{len(files)}")
    print(f"[backfill] 修改文件：{total_files}")
    print(f"[backfill] match 总数：{total_matches}")
    print(f"[backfill] 填充 home_en：{total_filled_home}")
    print(f"[backfill] 填充 away_en：{total_filled_away}")
    print(f"[backfill] 已完整无需改：{total_already_ok}")
    print(f"[backfill] 备份目录：{backup_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

