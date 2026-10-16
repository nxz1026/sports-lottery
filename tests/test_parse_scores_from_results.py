"""R37-8: scripts/core/data/parse.py::_scores_from_results 4 测直测（Top 8）。

现状：form_score / form_str / record_summary / record_score 四件套
推导函数 0 直测；足球状态评分核心。空 list 兜底 (0.5, "", "", 0.5)
是契约。

公式：
- score = sum(1.0 for W + 0.5 for D + 0.0 for L) / len
- form_str = "".join(results)
- record_summary = "W-D-L"
- rec_score = w / (w + d + l)（无 w/d/l 时 0.5）

本轮补 4 测：
- 全胜 "WWWWW" → (1.0, "WWWWW", "5-0-0", 1.0)
- 全负 "LLLLL" → (0.0, "LLLLL", "0-0-5", 0.0)
- 混合 "WDLWW" → (0.7, "WDLWW", "3-1-1", 0.6)
- 空 list → (0.5, "", "", 0.5) 兜底
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def parse_mod():
    spec = importlib.util.spec_from_file_location(
        "parse_scores_from_results_test",
        REPO_ROOT / "scripts" / "core" / "data" / "parse.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _scores_from_results ────────────────────────


def test_scores_from_results_all_wins(parse_mod):
    """R37-8：全胜 "WWWWW" → (1.0, "WWWWW", "5-0-0", 1.0)。"""
    out = parse_mod._scores_from_results(["W"] * 5)
    assert out == (1.0, "WWWWW", "5-0-0", 1.0)


def test_scores_from_results_all_losses(parse_mod):
    """R37-8：全负 "LLLLL" → (0.0, "LLLLL", "0-0-5", 0.0)。"""
    out = parse_mod._scores_from_results(["L"] * 5)
    assert out == (0.0, "LLLLL", "0-0-5", 0.0)


def test_scores_from_results_mixed(parse_mod):
    """R37-8：混合 "WDLWW" → (0.7, "WDLWW", "3-1-1", 0.6)。

    score = (1.0 + 0.5 + 0.0 + 1.0 + 1.0) / 5 = 3.5 / 5 = 0.7
    record_summary = "3-1-1"（3W, 1D, 1L）
    rec_score = 3 / (3 + 1 + 1) = 0.6（D 不计入 rec_score）
    """
    out = parse_mod._scores_from_results(["W", "D", "L", "W", "W"])
    assert out == (0.7, "WDLWW", "3-1-1", 0.6)


def test_scores_from_results_empty_returns_neutral_fallback(parse_mod):
    """R37-8：空 list → (0.5, "", "", 0.5) 兜底（中性开局）。

    新球队无历史 → 视为中性 form_score=0.5、rec_score=0.5、空字符串。
    """
    assert parse_mod._scores_from_results([]) == (0.5, "", "", 0.5)