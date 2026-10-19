"""R38-1: scripts/core/data/parse.py::_warm_team_names 5 测直测（Top 7）。

现状：i18n LLM 翻译缓存预热函数 0 直测；只通过 `_warm_team_names`
间接调用。异常吞掉 + logger.warning 兜底；i18n 故障不污染主流程。

5 分支：
- 空 events → warm_translations([])
- name 含 " at " → 拆 home/away
- name 不含 " at " → 整串加入
- competitions.competitors[].team.displayName → 收集
- warm_translations 抛异常 → 吞掉 + logger.warning

本轮补 5 测。
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
def parse_mod(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "parse_warm_team_names_test",
        REPO_ROOT / "scripts" / "core" / "data" / "parse.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def captured_names(monkeypatch):
    """mock warm_translations 捕获 names 参数。"""
    captured = []
    import core.i18n as i18n_mod
    monkeypatch.setattr(i18n_mod, "warm_translations",
                        lambda names: captured.append(list(names)))
    return captured


# ──────────────────────── _warm_team_names ────────────────────────


def test_warm_team_names_empty_events(parse_mod, captured_names):
    """R38-1：空 events → warm_translations([])。"""
    parse_mod._warm_team_names([])
    assert captured_names == [[]]


def test_warm_team_names_extracts_home_away_from_at(parse_mod, captured_names):
    """R38-1：name 含 " at " → 拆出 home（后半）+ away（前半）。

    `en.split(" at ", 1)` → [_a, _h]，_h 在后为 home，_a 在前为 away。
    """
    parse_mod._warm_team_names([
        {"name": "Manchester United at Liverpool"},
    ])
    assert captured_names == [["Liverpool", "Manchester United"]]


def test_warm_team_names_no_at_appends_whole_name(parse_mod, captured_names):
    """R38-1：name 不含 " at " → 整串加入。"""
    parse_mod._warm_team_names([{"name": "Single Team Name"}])
    assert captured_names == [["Single Team Name"]]


def test_warm_team_names_collects_displayName(parse_mod, captured_names):
    """R38-1：competitions[].competitors[].team.displayName → 收集。

    嵌套 dict 安全访问：`(ev.get("competitions") or [])` + `(_c.get("team") or {})`。
    实际行为（bug-shaped）：ev 缺 `name` 字段 → en="" → 无 "at" → else 分支
    `_names.append("")` 加入空串（不是跳过）。锁定现状。
    """
    events = [
        {"competitions": [{"competitors": [
            {"team": {"displayName": "Arsenal"}},
            {"team": {"displayName": "Chelsea"}},
        ]}]},
    ]
    parse_mod._warm_team_names(events)
    # 锁实际行为：缺 name → append ""；然后 collect 两条 displayName
    assert captured_names == [["", "Arsenal", "Chelsea"]]


def test_warm_team_names_swallows_warm_translations_exception(parse_mod, monkeypatch, caplog):
    """R38-1：warm_translations 抛异常 → 吞掉 + logger.warning（不向上抛）。"""
    import core.i18n as i18n_mod

    def boom(names):
        raise RuntimeError("llm network down")
    monkeypatch.setattr(i18n_mod, "warm_translations", boom)
    with caplog.at_level("WARNING"):
        # 不抛异常
        parse_mod._warm_team_names([{"name": "Arsenal at Chelsea"}])
    # warning 含 "Team-name translation warm-up failed"
    assert any("Team-name translation warm-up failed" in r.message
               for r in caplog.records)
