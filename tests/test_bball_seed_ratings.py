"""R37-5: scripts/bball/elo_bball.py::_seed_ratings 4 测直测 + D-25 兜底锁（Top 5）。

现状：篮球 ELO 初始化函数 0 直测；被 load_ratings 调用（已测 happy path），
但种子解析 + 兜底 fallback 链路从未被直测。

D-25 bug-shaped 锁定：TEAMS_SEED_FILE.read_text() 失败（JSONDecodeError /
OSError）→ fallback `list(TEAM_CN)`。当前实现静默回退无 warning，
若种子文件被外部破坏时拿不到任何信号。

本轮补 4 测：
- 正常 JSON dict 种子 → 按 keys 初始化 INITIAL
- 正常 JSON list 种子 → 直接迭代（list 形态也支持）
- JSONDecodeError → fallback list(TEAM_CN)
- OSError（文件缺/不可读）→ fallback list(TEAM_CN)
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ── 预先注册 bball 包 + config_bball 子模块，让 elo_bball 相对导入工作 ──
_bball_pkg = types.ModuleType("bball")
_bball_pkg.__path__ = [str(REPO_ROOT / "scripts" / "bball")]
sys.modules.setdefault("bball", _bball_pkg)
_config_bball = types.ModuleType("bball.config_bball")
_config_bball.ELO = {
    "INITIAL": 1500.0, "SCALE": 400, "K": 20, "HOME_ADV": 100,
}
_config_bball.TEAM_CN = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS",
}
_config_bball.ELO_FILE = Path("unused")
_config_bball.TEAMS_SEED_FILE = Path("unused")
sys.modules.setdefault("bball.config_bball", _config_bball)


class _FakeSeedFile:
    """Mock TEAMS_SEED_FILE.read_text() — 返指定 data 或抛指定异常。"""

    def __init__(self, data: str | None = None, raises: Exception | None = None):
        self.data = data
        self.raises = raises

    def read_text(self, encoding: str | None = None) -> str:
        if self.raises:
            raise self.raises
        return self.data or ""


@pytest.fixture
def bball_elo_mod():
    """加载 bball.elo_bball 模块（依赖已注册的 bball.config_bball 子模块）。

    用 `spec_from_file_location('bball.elo_bball', ...)` 让 bball 包成为父包，
    相对导入 `from .config_bball import ...` 才能找到。
    """
    spec = importlib.util.spec_from_file_location(
        "bball.elo_bball",
        REPO_ROOT / "scripts" / "bball" / "elo_bball.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _seed_ratings ────────────────────────


def test_seed_ratings_dict_seed_initializes_keys(bball_elo_mod, monkeypatch):
    """R37-5：dict 形态种子 → 按 keys 初始化 INITIAL (1500)。"""
    monkeypatch.setattr(bball_elo_mod, "TEAMS_SEED_FILE",
                        _FakeSeedFile(json.dumps({"Lakers": 1, "Celtics": 1})))
    out = bball_elo_mod._seed_ratings()
    assert out == {"Lakers": 1500.0, "Celtics": 1500.0}


def test_seed_ratings_list_seed_initializes_elements(bball_elo_mod, monkeypatch):
    """R37-5：list 形态种子 → 直接迭代各元素初始化 INITIAL。"""
    monkeypatch.setattr(bball_elo_mod, "TEAMS_SEED_FILE",
                        _FakeSeedFile(json.dumps(["Lakers", "Celtics"])))
    out = bball_elo_mod._seed_ratings()
    assert out == {"Lakers": 1500.0, "Celtics": 1500.0}


def test_seed_ratings_d25_json_decode_error_falls_back_to_team_cn(bball_elo_mod, monkeypatch):
    """R37-5 D-25 锁定：JSONDecodeError → fallback list(TEAM_CN) keys。

    静默回退（无 warning）；若种子文件被外部破坏时拿不到任何信号。
    锁定现状防 silent 切换为抛 ValueError 时回归。
    """
    monkeypatch.setattr(bball_elo_mod, "TEAMS_SEED_FILE",
                        _FakeSeedFile(data="not_valid_json"))
    out = bball_elo_mod._seed_ratings()
    # fallback list(TEAM_CN).keys() → 用 mock 的 TEAM_CN (2 队)
    assert "Atlanta Hawks" in out
    assert "Boston Celtics" in out


def test_seed_ratings_d25_os_error_falls_back_to_team_cn(bball_elo_mod, monkeypatch):
    """R37-5 D-25 锁定：OSError（文件缺/不可读）→ fallback list(TEAM_CN) keys。"""
    monkeypatch.setattr(bball_elo_mod, "TEAMS_SEED_FILE",
                        _FakeSeedFile(raises=OSError("file missing")))
    out = bball_elo_mod._seed_ratings()
    # fallback TEAM_CN
    assert "Atlanta Hawks" in out
    assert "Boston Celtics" in out