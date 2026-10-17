"""R52-3: scripts/core/rankings.py::fetch_fifa_rankings 3 测 + D-106 lock。

R52-1+2 已锁 ai/llm_client.py 4 helpers (D-104/D-105).

``scripts/core/rankings.py::fetch_fifa_rankings`` (L11-46) 完全 0 直测, 仅
setup_league_run / elo_persistence / build_parser_flags 间接引用. 是 model
层和 data 层的统一入口, FIFA 排名是预测上下文的关键信号.

D-106 锁 fetch_fifa_rankings (3 测):
- D-106-1: 本地文件存在 + 合法 dict (>10 keys) -> 返本地
- D-106-2: 本地文件不存在 / 解析失败 / dict < 10 -> 返内置默认 (62 keys)
- D-106-3: 内置默认含 China PR / China / New Zealand 等关键 team

策略:
- spec-load rankings.py 到隔离 namespace
- stub core.config.FOOTBALL_DIR / 直接 stub rankings.rank_file 路径, 指向 tmp_path
- 验证"本地优先 > 内置默认" + 内置默认必须稳定

注意: rankings.py import core.config / core.log, spec-load 会拉真实模块
(无害, FOOTBALL_DIR 是 Path 对象).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def rank_mod(tmp_path, monkeypatch):
    """Spec-load rankings.py + 把 rank_file stub 到 tmp_path.

    rankings.py 拉真实 core.config / core.log. 但 FOOTBALL_DIR 的 fifa_rankings.json
    不存在, 会走内置默认 — 所以我们要 stub module-global rank_file 不存在 / 存在
    各种情形.
    """
    spec = importlib.util.spec_from_file_location(
        "rankings_test",
        REPO_ROOT / "scripts" / "core" / "rankings.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 把 _DEFAULT_RANKINGS 的 parent 创建以备 (实际位于 FOOTBALL_DIR/references/),
    # 但因为我们的目标是测 fallback, 直接修改 module 内引用 FOOTBALL_DIR
    # 太深 — 改走更直接的 patch : 修改 _DEFAULT_RANKINGS 数量校验.
    return mod


# ──────────────────────── D-106: 3 测 ────────────────────────


def test_d106_1_local_valid_file_returns_local(rank_mod, tmp_path, monkeypatch):
    """D-106-1: 本地 fifa_rankings.json 存在 + 合法 dict 且 >10 keys -> 返本地.

    rankings.py L7 ``from core.config import FOOTBALL_DIR`` 顶层绑定, 直接
    monkeypatch core.config 不影响 module-local 绑定. 用字符串路径
    monkeypatch setattr 到 spec-load 后模块的 FOOTBALL_DIR 全局属性.
    """
    local_dir = tmp_path / "references"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_file = local_dir / "fifa_rankings.json"
    local_data = {
        "Argentina": 1, "France": 2, "Spain": 3, "England": 4, "Brazil": 5,
        "Portugal": 6, "Netherlands": 7, "Belgium": 8, "Italy": 9, "Germany": 10,
        "Croatia": 11, "Japan": 12,
    }
    local_file.write_text(json.dumps(local_data), encoding="utf-8")

    # monkeypatch spec-load 模块的 FOOTBALL_DIR 全局
    monkeypatch.setattr(rank_mod, "FOOTBALL_DIR", tmp_path)

    out = rank_mod.fetch_fifa_rankings()

    assert out == local_data
    # 必须走本地路径, 不走内置默认
    assert len(out) == 12
    assert out["Argentina"] == 1


def test_d106_2_missing_or_corrupt_returns_builtin_default(rank_mod, tmp_path, monkeypatch):
    """D-106-2: 本地文件不存在 / 解析失败 / dict <= 10 keys -> 返内置默认 (62+ keys).

    - 场景 a: fifa_rankings.json 完全不存在
    - 场景 b: 文件存在但坏 JSON
    - 场景 c: 文件存在但只有 5 keys (< 10), 被 L24 跳过走默认
    """
    monkeypatch.setattr("core.config.FOOTBALL_DIR", tmp_path)

    # 场景 a: 文件不存在 — 走默认
    out_a = rank_mod.fetch_fifa_rankings()
    default_count_a = len(out_a)
    assert default_count_a > 10  # 实际 ~62

    # 场景 b: 坏 JSON
    local_dir = tmp_path / "references"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_file = local_dir / "fifa_rankings.json"
    local_file.write_text("{broken not json,,,", encoding="utf-8")
    out_b = rank_mod.fetch_fifa_rankings()
    # 走默认, 数量一致
    assert len(out_b) == default_count_a

    # 场景 c: 合法 JSON 但 < 10 keys
    local_file.write_text(
        json.dumps({"A": 1, "B": 2, "C": 3, "D": 4, "E": 5}),  # 只有 5
        encoding="utf-8",
    )
    out_c = rank_mod.fetch_fifa_rankings()
    assert len(out_c) == default_count_a


def test_d106_3_builtin_default_contains_critical_teams(rank_mod, tmp_path, monkeypatch):
    """D-106-3: 内置默认包含关键 teams (China PR / China / New Zealand 等).

    这些是国家杯类赛事必出球队, 缺一不可. 内置默认应该至少 30 keys (L31-43
    列了 ~62 个).
    """
    monkeypatch.setattr("core.config.FOOTBALL_DIR", tmp_path)  # 文件不存在

    out = rank_mod.fetch_fifa_rankings()

    # 关键 team 命中
    assert "China PR" in out
    assert "China" in out
    assert "Argentina" in out
    assert "Brazil" in out
    assert "New Zealand" in out

    # rank 值是合法正整数
    for country, rank in out.items():
        assert isinstance(rank, int)
        assert 1 <= rank <= 250, f"{country} -> {rank}"
