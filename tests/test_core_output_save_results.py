"""R23 #4: core/output.save_results 端到端边角测试。

现状覆盖：test_core_output_home_en.py (3) + test_predict_log_error_wrappers.py (2)，
但未触达：
  · scored 为空时早 return（不写文件）
  · 已有 corrupt JSON 时静默覆盖（不死循环）
  · 同日多次调用合并 dedup（用 home_away key）
  · RESULTS_DIR 缺失时 mkdir 兜底
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _load_output_module():
    spec = importlib.util.spec_from_file_location(
        "output_mod_for_test", REPO_ROOT / "scripts" / "core" / "output.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


save_results = _load_output_module().save_results


def _set_results_dir(monkeypatch, tmp_path):
    """``save_results`` 内部 ``from core.config import RESULTS_DIR`` 是 from-import 别名
    在 ``output_mod_for_test`` 独立 module name 加载；monkeypatch 这个 module
    局部属性才能影响 from-import 后的引用。"""
    import sys
    mod = sys.modules["output_mod_for_test"]
    monkeypatch.setattr(mod, "RESULTS_DIR", tmp_path)


def test_no_scored_matches_returns_early_no_file(tmp_path, monkeypatch):
    """所有 match 缺 score / score 无 "-" → save_results 早 return，**不写文件**。"""
    _set_results_dir(monkeypatch, tmp_path)

    save_results([{"home": "A", "away": "B"},          # 缺 score
                   {"home": "C", "away": "D", "score": ""},  # 空 score
                   {"home": "E", "away": "F", "score": "1"}])  # 无 "-"
    # 不应写出 result_*.json
    assert list(tmp_path.glob("result_*.json")) == []


def test_corrupt_existing_json_is_silently_overwritten(tmp_path, monkeypatch):
    """已有 corrupt JSON（坏文件） → 静默覆盖，不抛错也不死循环。"""
    _set_results_dir(monkeypatch, tmp_path)

    # 写坏文件
    bad = tmp_path / "result_2026-10-15.json"
    bad.write_text("{not valid json", encoding="utf-8")

    # 跑 save_results → 应写合法 JSON 覆盖坏文件
    save_results([{"name": "m1", "home": "A", "away": "B",
                   "score": "2-1", "kickoff_utc": "2026-10-15T12:00:00Z"}])
    data = json.loads(bad.read_text(encoding="utf-8"))
    assert data["date"] == "2026-10-15"
    assert len(data["matches"]) == 1
    assert data["matches"][0]["home"] == "A"


def test_same_day_merge_dedupes_by_home_away_key(tmp_path, monkeypatch):
    """同日两次 save_results：相同 (home, away) 覆盖，添加新对阵不丢旧数据。"""
    _set_results_dir(monkeypatch, tmp_path)

    # 第一次：m1 + m2
    save_results([
        {"name": "m1", "home": "A", "away": "B", "score": "2-1",
         "kickoff_utc": "2026-10-15T12:00:00Z"},
        {"name": "m2", "home": "C", "away": "D", "score": "0-0",
         "kickoff_utc": "2026-10-15T14:00:00Z"},
    ])
    # 第二次：m1 比分更新（覆盖）+ 新 m3
    save_results([
        {"name": "m1", "home": "A", "away": "B", "score": "3-1",
         "kickoff_utc": "2026-10-15T12:00:00Z"},
        {"name": "m3", "home": "E", "away": "F", "score": "1-0",
         "kickoff_utc": "2026-10-15T16:00:00Z"},
    ])

    data = json.loads((tmp_path / "result_2026-10-15.json").read_text(encoding="utf-8"))
    matches = {(m["home"], m["away"]): m for m in data["matches"]}

    assert len(matches) == 3, f"应 3 个 unique key；实际 {len(matches)}: {list(matches)}"
    # m1 比分被覆盖 2-1 → 3-1
    assert matches[("A", "B")]["home_score"] == 3
    assert matches[("A", "B")]["away_score"] == 1
    # m2 保留旧值
    assert matches[("C", "D")]["home_score"] == 0
    # m3 新增
    assert matches[("E", "F")]["home_score"] == 1


def test_results_dir_created_if_missing(tmp_path, monkeypatch):
    """RESULTS_DIR 父目录不存在 → save_results 自动 mkdir。"""
    nested = tmp_path / "deeply" / "nested" / "results"
    _set_results_dir(monkeypatch, nested)
    assert not nested.exists()

    save_results([{"name": "m1", "home": "A", "away": "B",
                   "score": "1-0", "kickoff_utc": "2026-10-15T12:00:00Z"}])
    assert nested.is_dir(), "RESULTS_DIR 应被自动创建"
    assert (nested / "result_2026-10-15.json").exists()
