"""R32 #1: scripts/core/predictor.py::_resolve_direction_winner 5 测直测。

现状：函数本体 0 直测；仅被 _reconcile_score_with_direction 间接调用，
但 reconcile 自身也无直测，所以整个"方向解析 → 矛盾消解"链路
完全是黑盒。

本轮补 5 测，覆盖：
- "平局"（无"胜"字）→ None
- home_name 前缀 → "home"
- away_name 前缀 → "away"
- 都不匹配 → None + logger.warning（caplog 验证）
- D-13 bug-shaped 锁定：home=""（match_ctx 缺 home 字段）→ 任何
  dir_str 都返 "home"（Python `str.startswith("")` 永远 True 的副作用）
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
def predictor_mod():
    """加载 predictor.py 为唯一 module name（predictor 顶层 import 链触发 core.config
    + core.log + core.rankings + core.model.* 等；测试只读 _resolve_direction_winner）。"""
    spec = importlib.util.spec_from_file_location(
        "resolve_dir_winner_test", REPO_ROOT / "scripts" / "core" / "predictor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _resolve_direction_winner ────────────────────────


def test_resolve_direction_winner_draw_returns_none(predictor_mod):
    """R32 #1："平局" 不含"胜"字 → None（不做 prefix-match 解 draw）。"""
    out = predictor_mod._resolve_direction_winner(
        "平局", {"home": "Arsenal", "away": "Chelsea"})
    assert out is None


def test_resolve_direction_winner_home_prefix(predictor_mod):
    """R32 #1：dir_str 以 home_name 起 → "home"。"""
    out = predictor_mod._resolve_direction_winner(
        "Arsenal 胜", {"home": "Arsenal", "away": "Chelsea"})
    assert out == "home"


def test_resolve_direction_winner_away_prefix(predictor_mod):
    """R32 #1：dir_str 以 away_name 起 → "away"。"""
    out = predictor_mod._resolve_direction_winner(
        "Chelsea 胜", {"home": "Arsenal", "away": "Chelsea"})
    assert out == "away"


def test_resolve_direction_winner_no_match_returns_none_with_warning(predictor_mod, caplog):
    """R32 #1：home/away 前缀都不匹配 → None + logger.warning（队名互为前缀边界）。"""
    with caplog.at_level("WARNING"):
        out = predictor_mod._resolve_direction_winner(
            "Barcelona 胜", {"home": "Arsenal", "away": "Chelsea"})
    assert out is None
    # 警告日志含原 dir_str + home + away，便于排查
    assert any(
        "_resolve_direction_winner" in rec.message and "Barcelona" in rec.message
        for rec in caplog.records
    ), f"missing warning, got: {[r.message for r in caplog.records]}"


def test_resolve_direction_winner_empty_home_always_returns_home(predictor_mod):
    """R32 #1 D-13 bug-shaped 锁定：home_name=""（match_ctx 缺 home 字段）→ 任何
    dir_str 都返 "home"，因为 Python `str.startswith("")` 永远 True。

    这是 _resolve_direction_winner 的隐性副作用：上游 match_ctx 必须保证 home/away
    非空，否则方向解析退化为常量 "home"。当前 _save_output 主流程上游（predict.py
    对接 api-football 时）不会缺 home/away，但 AI 分析路径可能传不完整 ctx → 静默
    全判 "home"。本测锁定现状防 silent 修复时回归。
    """
    # home="" + dir 含"胜" → 返 "home"（空字符串前缀匹配）
    out = predictor_mod._resolve_direction_winner(
        "胜", {"home": "", "away": "Chelsea"})
    assert out == "home"
    # 同理 away="" → 返 "away"（注意 home 在 elif 前先返，空字符串触发不到 away 分支）
    out2 = predictor_mod._resolve_direction_winner(
        "胜", {"home": "Arsenal", "away": ""})
    assert out2 == "away"
    # match_ctx 完全空（home 和 away 都默认 ""）→ 走 home 分支
    out3 = predictor_mod._resolve_direction_winner(
        "胜", {})
    assert out3 == "home"
