"""test_core_output_home_en.py — output.py 写 result 时必须同时写 home_en/away_en。

背景（web UX 审核 P0-A2 / 2026-10-11）：
  老版本 `save_results` 只设 `home` / `away`（取值自 `home_en`），未单独写
  `home_en` / `away_en` 字段。reconciliation 的 `_bk_stable_key` 只读
  `home_en` / `away_en`，所以 result 全部 key 为空字符串、命中率永远 0%。

本测试覆盖：
  1. 新写入的 match 必须含 home_en / away_en 字段，且与 home / away 一致
  2. 加载老格式文件时（缺 home_en/away_en）触发 backfill：缺失字段沿用 home/away
  3. backfill 幂等：第二次跑无变化
  4. backfill 落盘前备份到 .bak-{ts}/
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# 与 tests/test_*.py 一致：把仓库根 + scripts/ 加进 sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.output import save_results  # noqa: E402

from scripts.backfill_result_home_en import main as backfill_main  # noqa: E402


@pytest.fixture()
def results_dir(tmp_path: Path, monkeypatch) -> Path:
    """重定向 PREDICTIONS_DIR / RESULTS_DIR 到 tmp_path。

    注意：``core.output`` 在模块级 ``from core.config import RESULTS_DIR``，
    所以 monkeypatch 要直接打到 ``core.output.RESULTS_DIR`` 上。
    """
    import core.config as config
    import core.output as out_mod
    rd = tmp_path / "results"
    pd = tmp_path / "predictions"
    monkeypatch.setattr(config, "RESULTS_DIR", rd)
    monkeypatch.setattr(config, "PREDICTIONS_DIR", pd)
    monkeypatch.setattr(out_mod, "RESULTS_DIR", rd)
    monkeypatch.setattr(out_mod, "PREDICTIONS_DIR", pd)
    rd.mkdir(parents=True, exist_ok=True)
    pd.mkdir(parents=True, exist_ok=True)
    return rd


def _match(home: str = "Arsenal FC", away: str = "Coventry City FC",
           score: str = "2-0", name: str | None = None,
           kickoff_utc: str = "2026-08-21T19:00:00Z") -> dict:
    """构造一个完赛场次（带 home_en / away_en）—— 模拟 parse.py 的输出。"""
    return {
        "name": name if name is not None else f"{home} vs {away}",
        "kickoff_utc": kickoff_utc,
        "home": home, "home_en": home,
        "away": away, "away_en": away,
        "score": score, "status": "STATUS_FULL_TIME",
    }


def test_save_results_writes_home_en_and_away_en(results_dir: Path, monkeypatch):
    """writer 必须把 home_en / away_en 字段一起写出。"""
    save_results([_match()])

    files = list(results_dir.glob("result_*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert len(data["matches"]) == 1
    m = data["matches"][0]
    assert m["home"] == "Arsenal FC"
    assert m["away"] == "Coventry City FC"
    assert m["home_en"] == "Arsenal FC", "home_en 缺失会让 reconciliation 永远 0%"
    assert m["away_en"] == "Coventry City FC", "away_en 缺失会让 reconciliation 永远 0%"


def test_save_results_uses_home_en_when_home_is_chinese(results_dir: Path):
    """当 home 为中文、home_en 为英文（parse 的 i18n 行为），writer 应当把 home_en
    拷贝到 home 字段并写入 home_en —— 历史字段 home 不应还是中文。"""
    save_results([{
        "name": "阿森纳 vs 考文垂",
        "kickoff_utc": "2026-08-21T19:00:00Z",
        "home": "阿森纳", "home_en": "Arsenal FC",
        "away": "考文垂", "away_en": "Coventry City FC",
        "score": "2-0", "status": "STATUS_FULL_TIME",
    }])

    files = list(results_dir.glob("result_*.json"))
    m = json.loads(files[0].read_text(encoding="utf-8"))["matches"][0]
    # writer 用 home_en 覆盖 home（这是历史行为，不能让中文干扰 stable key）
    assert m["home"] == "Arsenal FC"
    assert m["home_en"] == "Arsenal FC"
    assert m["away"] == "Coventry City FC"
    assert m["away_en"] == "Coventry City FC"


def test_save_results_backfills_existing_old_file(results_dir: Path):
    """老格式文件（缺 home_en / away_en）再次 save_results 时应自动 backfill。"""
    # writer 写入今天日期；先用「老格式」（缺 home_en/away_en）预填今天的结果文件
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_path = results_dir / f"result_{today}.json"
    today_path.write_text(json.dumps({
        "date": today,
        "matches": [{
            "id": "Arsenal vs Coventry",
            "kickoff_utc": "2026-08-21T19:00:00Z",
            "home": "Arsenal FC",
            "away": "Coventry City FC",
            "home_score": 2, "away_score": 0,
            "status": "STATUS_FULL_TIME",
            # 注意：故意不写 home_en / away_en —— 模拟老格式
        }],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 现在用 writer 加载并合并一个新 match（触发 backfill 逻辑）
    save_results([_match(home="Chelsea FC", away="Liverpool FC", score="1-1")])

    # 读回，检查两份 match 是否都有 home_en / away_en
    data = json.loads(today_path.read_text(encoding="utf-8"))
    by_id = {m["id"]: m for m in data["matches"]}
    assert len(by_id) == 2

    old = by_id["Arsenal vs Coventry"]
    assert old.get("home_en") == "Arsenal FC", "老 match 也应被 backfill"
    assert old.get("away_en") == "Coventry City FC"

    new = by_id["Chelsea FC vs Liverpool FC"]
    assert new.get("home_en") == "Chelsea FC"
    assert new.get("away_en") == "Liverpool FC"


def test_backfill_script_fills_missing_fields(monkeypatch, tmp_path: Path, capsys):
    """一次性 backfill 脚本必须沿用 home/away 填充 home_en/away_en，备份到 .bak-{ts}/。

    注意：backfill 脚本里 ``RESULTS_DIR = PROJECT_ROOT / "results"`` 是脚本自身
    的常量（独立于 core.config），所以 monkeypatch 要打到 ``backfill_result_home_en.RESULTS_DIR``。
    """
    import scripts.backfill_result_home_en as bk
    monkeypatch.setattr(bk, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(bk, "PROJECT_ROOT", tmp_path.parent)

    target = tmp_path / "result_2026-09-27.json"
    target.write_text(json.dumps({
        "date": "2026-09-27",
        "matches": [
            {"id": "m1", "home": "Man United", "away": "Chelsea", "home_score": 1, "away_score": 0},
            {"id": "m2", "home": "Liverpool", "away": "Arsenal", "home_score": 2, "away_score": 2,
             "home_en": "Liverpool FC", "away_en": "Arsenal FC"},  # 已有，跳过
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    rc = backfill_main()
    assert rc == 0

    out = capsys.readouterr().out
    assert "填充 home_en：1" in out
    assert "填充 away_en：1" in out
    assert "已完整无需改：1" in out  # 第二条已经含 home_en/away_en

    # 文件被修改
    data = json.loads(target.read_text(encoding="utf-8"))
    by_id = {m["id"]: m for m in data["matches"]}
    assert by_id["m1"]["home_en"] == "Man United"
    assert by_id["m1"]["away_en"] == "Chelsea"
    assert by_id["m2"]["home_en"] == "Liverpool FC"  # 未动
    assert by_id["m2"]["away_en"] == "Arsenal FC"

    # 备份目录存在
    backups = list(tmp_path.glob(".bak-*"))
    assert len(backups) == 1
    assert (backups[0] / "result_2026-09-27.json").exists()


def test_backfill_script_idempotent(monkeypatch, tmp_path: Path, capsys):
    """第二次跑 backfill 应无变化（字段已存在）。"""
    import scripts.backfill_result_home_en as bk
    monkeypatch.setattr(bk, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(bk, "PROJECT_ROOT", tmp_path.parent)

    target = tmp_path / "result_2026-09-27.json"
    target.write_text(json.dumps({
        "date": "2026-09-27",
        "matches": [{"id": "m1", "home": "A", "away": "B", "home_en": "A", "away_en": "B"}],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 第一次
    backfill_main()
    # 第二次（幂等）
    backfill_main()

    out = capsys.readouterr().out
    # 两次都应报"已完整"
    assert "已完整无需改：1" in out
