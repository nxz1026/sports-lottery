"""R39-4: scripts/bball/elo_ball.py::save_ratings 4 tests direct + D-31 type-crash lock.

篮球 ELO 持久化 0 direct tests; load_ratings 已测 (fallback 路径), 但 save
自身行为未锁。save 是无原子写 (write_text 直接覆盖), 半写期间并发读拿
JSONDecodeError 被静默回退, 丢运维线索。

helper 含两条隐契约:
- 父目录创建: ELO_FILE.parent.mkdir(parents=True, exist_ok=True) — 即便
  ELO_FILE 父目录初次生成也 work; 重复运行不抛 FileExistsError
- JSON 兼容值: json.dumps 默认 encoder 不支持 complex / numpy / torch
  标量; 若上游 update_ratings 误把 numpy.float32 塞进 dict, save 抛 TypeError
  (而非返回部分写入的垃圾文件)
- indent=2 锁: 文件含换行 + 缩进, 不是 compact JSON

D-31 (type-crash lock):
- 非 JSON 兼容值 (如 complex) 必须抛 TypeError; silent 退化为半写文件
  会让 load_ratings 永久回退种子, 丢失 ELO 序列。fast-fail 才能暴露 bug。

4 tests:
- happy path roundtrip: ratings -> save -> read -> equal (indent=2 锁)
- parent dir 自动创建: 深嵌套未存父目录 -> save 后 exists
- parent dir 已存在: 不抛 FileExistsError, 重复写 OK
- D-31 非 JSON 兼容值 (complex) -> TypeError 快失败
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ⚠️ **P1（2026-10-23）**：占位路径必须是**绝对**路径。
#    旧写法 `Path("unused")` 是**相对路径** ⇒ 一旦有代码真去写它，
#    落点就是 **CWD = 仓库根** ⇒ 仓库根多出一个 `unused` 文件。
#    ⚠️ 本文件尤其危险：`sys.modules.setdefault("bball.config_bball", ...)`
#    把这个假模块**装进全局 sys.modules**（整个 pytest session 都生效）
#    ⇒ 漏掉重定向的属性会被**后续别的测试**（如 test_bball_run.py）写到仓库根。
#    实测：全量 pytest 时 `test_run_marks_no_future_matches_when_predictions_empty`
#    的哨兵就抓到 `sports-lottery/unused` 被改（实测 108 字节）。
_UNUSED = Path(tempfile.gettempdir()) / "sports-lottery-test-unused"


# ── 预先注册 bball 包 + config_bball 子模块 ──
_bball_pkg = types.ModuleType("bball")
_bball_pkg.__path__ = [str(REPO_ROOT / "scripts" / "bball")]
sys.modules.setdefault("bball", _bball_pkg)
_config_bball = types.ModuleType("bball.config_bball")
_config_bball.ELO = {
    "INITIAL": 1500.0, "SCALE": 400, "K": 20, "HOME_ADV": 100,
}
_config_bball.TEAM_CN = {"Atlanta Hawks": "ATL", "Boston Celtics": "BOS"}
_config_bball.ELO_FILE = _UNUSED  # 占位; 每个 fixture 重定向到 tmp
_config_bball.TEAMS_SEED_FILE = _UNUSED
# 占位: R39-4 只用 ELO/ELO_FILE; 其他属性让 from .config_bball import 不爆
_config_bball.LEAGUE = {"odds_sport": "basketball_nba", "name": "NBA"}
_config_bball.PREDICT = {"LEAGUE_PACE": 98.5, "AVG_PPG": 112.0, "HOME_SCORE_ADV": 2.5, "ELO_TO_POINTS": 0.06, "SPREAD_CONFIDENCE": 0.65, "TOTAL_CONFIDENCE": 0.6}
_config_bball.DATA_DIR = _UNUSED
_config_bball.TEAM_CN_ALIASES = {}
sys.modules.setdefault("bball.config_bball", _config_bball)


@pytest.fixture
def bball_elo_mod(tmp_path, monkeypatch):
    """加载 elo_bball + 把 ELO_FILE / TEAMS_SEED_FILE / DATA_DIR 重定向到 tmp 隔离真实目录。

    ⚠️ **DATA_DIR 也必须重定向**：本文件用 ``sys.modules.setdefault`` 把假模块
    装进了**全局** sys.modules ⇒ 漏掉任何一个属性，**后续别的测试**就会把它
    写到仓库根（实测：`unused` 文件被改 ⇒ 哨兵 FAIL）。
    """
    elo_file = tmp_path / "nba_elo.json"
    seed_file = tmp_path / "teams_seed.json"
    monkeypatch.setattr(_config_bball, "ELO_FILE", elo_file)
    monkeypatch.setattr(_config_bball, "TEAMS_SEED_FILE", seed_file)
    monkeypatch.setattr(_config_bball, "DATA_DIR", tmp_path / "data")
    spec = importlib.util.spec_from_file_location(
        "bball.elo_bball",
        REPO_ROOT / "scripts" / "bball" / "elo_bball.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── happy path ────────────────────────


def test_save_ratings_roundtrip(bball_elo_mod):
    """R39-4 happy path: ratings dict -> save -> read -> equal (indent=2 锁)。"""
    ratings = {"Lakers": 1510.0, "Celtics": 1490.0, "Heat": 1500.0}
    bball_elo_mod.save_ratings(ratings)

    # 直接读文件 (绕开 load_ratings 的 ELO 兼容转换)
    raw = bball_elo_mod.ELO_FILE.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert parsed == ratings
    # indent=2 锁: 文件含换行 (compact JSON 单行)
    assert "\n" in raw


# ──────────────────────── parent dir behavior ────────────────────────


def test_save_ratings_creates_parent_dir(bball_elo_mod, tmp_path, monkeypatch):
    """R39-4: ELO_FILE 父目录初次生成 -> mkdir(parents=True, exist_ok=True) 自动建。"""
    deep_path = tmp_path / "deep" / "nested" / "file.json"
    assert not deep_path.parent.exists()  # 父目录确认不存在
    # ELO_FILE 是模块本地 binding (from .config_bball import ELO_FILE)
    # 必须 monkeypatch 模块本体, 不是 config_bball
    monkeypatch.setattr(bball_elo_mod, "ELO_FILE", deep_path)

    bball_elo_mod.save_ratings({"Lakers": 1500.0})

    # save 后: 父目录被自动创建, 文件存在
    assert deep_path.parent.is_dir()
    assert deep_path.is_file()
    parsed = json.loads(deep_path.read_text(encoding="utf-8"))
    assert parsed == {"Lakers": 1500.0}


def test_save_ratings_existing_parent_dir_ok(bball_elo_mod):
    """R39-4: 父目录已存在 -> save 不抛 FileExistsError (exist_ok=True)。

    多次连续调用 (定期写盘) 不应中断; 上次写入胜出。
    """
    # 默认 fixture tmp_path 父目录已存在
    assert bball_elo_mod.ELO_FILE.parent.is_dir()

    bball_elo_mod.save_ratings({"Lakers": 1500.0})
    # 连续第二次调用不应抛错
    bball_elo_mod.save_ratings({"Lakers": 1505.0})

    parsed = json.loads(bball_elo_mod.ELO_FILE.read_text(encoding="utf-8"))
    # 最近一次写入胜出
    assert parsed == {"Lakers": 1505.0}


# ──────────────────────── D-31 type-crash lock ────────────────────────


def test_save_ratings_d31_non_json_value_raises(bball_elo_mod):
    """R39-4 D-31: 非 JSON 兼容值 (complex) -> save 抛 TypeError。

    锁定当前 fast-fail 契约: 静默退化为半写文件会让 load_ratings 永久回退
    种子, 丢失 ELO 序列。fast-fail 才能在 CI / GHA 立即暴露 bug。

    JSON 默认 encoder 不支持: complex, bytes, set, numpy.float32。
    """
    ratings = {"Lakers": complex(1500, 0.5)}  # complex 不可序列化
    with pytest.raises(TypeError):
        bball_elo_mod.save_ratings(ratings)
