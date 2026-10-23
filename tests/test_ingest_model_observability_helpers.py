"""R61: 剩余 5 个 0 直测 helper 7 测 + D-124 lock。

R60 末已锁 D-121~D-123, 仓库剩余 0 直测 helper 收敛到 5 个:

- ingest/jc_read._stamp                       (L12-15): 文件名 → datetime via regex
- ingest/jc_manifest._count_lines / _check_entry (L8-50): 行数 + manifest entry 校验
- model/fit._terms                            (L54-73): 一趟出 GF/GA + γ, 零进球拒收
- core/observability._warn_unwritable_events_once (L258-267): global 单次哨兵

D-124 锁 5 helper (7 测):
- D-124-1: _stamp 合法文件名 → datetime
- D-124-2: _stamp 非法文件名 (含 regex fail) → AttributeError (因 m.group 调不到)
- D-124-3: _count_lines 空行 / 多行 / PermissionError → 0
- D-124-4: _check_entry 文件不存在 → (path, "rejected")
- D-124-5: _terms 正常 teams 全 0+ 进球 → 返回 (gf, ga, ...)
- D-124-6: _terms 某球队 0 进球 → ValueError
- D-124-7: _warn_unwritable_events_once 两次调用只 log 1 次 (global flag)

策略:
- spec-load 4 个模块到隔离 namespace (用 scripts 包 parent 让 .x 相对 import 解析)
- observability._UNWRITABLE_EVENTS_WARNED 是 module-global, fixture 退出时 reset
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


# ───────── helpers ─────────


def _load_pkg(name: str):
    spec = importlib.util.spec_from_file_location(name, str(REPO_ROOT / "scripts" / "store" / "__init__.py"))
    return spec  # placeholder


@pytest.fixture(scope="module")
def root_pkg():
    """Pre-load scripts package + 子包 scripts.ingest."""
    # scripts package init
    scripts_init = REPO_ROOT / "scripts" / "__init__.py"
    if scripts_init.exists():
        spec = importlib.util.spec_from_file_location("r61_scripts_pkg", scripts_init)
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        try:
            spec.loader.exec_module(m)
        except Exception:
            pass

    # ingest subpackage init
    ingest_init = REPO_ROOT / "scripts" / "ingest" / "__init__.py"
    if ingest_init.exists():
        spec = importlib.util.spec_from_file_location("r61_scripts_pkg.ingest", ingest_init)
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        try:
            spec.loader.exec_module(m)
        except Exception:
            pass

    return "r61_scripts_pkg"


def _spec_load(name: str, path: Path, parent_pkg: str):
    spec = importlib.util.spec_from_file_location(f"{parent_pkg}.{name}", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _spec_load_simple(name: str, path: Path):
    """独立 spec-load (适合无相对 import 的 module)."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-124: 7 测 ────────────────────────


@pytest.fixture
def jc_read_mod(root_pkg):
    """Spec-load ingest.jc_read (含 _stamp)."""
    # 先注入 ingest.jc_manifest 让 _check_entry import 解析
    _spec_load("jc_manifest", REPO_ROOT / "scripts" / "ingest" / "jc_manifest.py", root_pkg)
    return _spec_load("jc_read", REPO_ROOT / "scripts" / "ingest" / "jc_read.py", root_pkg)


@pytest.fixture
def jc_manifest_mod(root_pkg):
    return _spec_load("jc_manifest", REPO_ROOT / "scripts" / "ingest" / "jc_manifest.py", root_pkg)


@pytest.fixture
def fit_mod(root_pkg):
    """Spec-load scripts.model.fit (含 _terms)."""
    return _spec_load("fit", REPO_ROOT / "scripts" / "model" / "fit.py", root_pkg)


@pytest.fixture
def observability_mod():
    """Spec-load scripts.core.observability."""
    return _spec_load_simple("r61_core_observability",
                            REPO_ROOT / "scripts" / "core" / "observability.py")


# D-124-1+2: _stamp
def test_d124_1_stamp_extracts_datetime(jc_read_mod):
    """D-124-1: jc_read._stamp (L12-15) 文件名 → datetime, 完整 / 不带秒两种."""
    mod = jc_read_mod

    # 完整 6 字段
    dt1 = mod._stamp("2025-10-17T14-30-25.json")
    assert dt1.year == 2025 and dt1.month == 10 and dt1.day == 17
    assert dt1.hour == 14 and dt1.minute == 30 and dt1.second == 25

    # 不带秒 (last group optional L13 ``(?:-(\d\d))?``)
    dt2 = mod._stamp("2025-10-17T14-30.blah")
    assert dt2.hour == 14 and dt2.minute == 30 and dt2.second == 0


def test_d124_2_stamp_invalid_name_raises_attribute_error(jc_read_mod):
    """D-124-2: _stamp 文件名无时间戳 → 显式抛错, **不静默返 None**。

    ⚠️ **P2-C（2026-10-23）改**：异常类型从 ``AttributeError``（``m.group()``
    在 ``m is None`` 上的意外产物）改成**可区分**的 ``InvalidFileNameError``
    （``jc_read`` 里与 ``UnreadableFileError`` 同级的「文件级可隔离错误」）。

    为什么必须改：旧行为是**掀翻整批**（``_window`` 里一个异形文件名 ⇒
    AttributeError ⇒ ``load_batch`` 整批 rollback ⇒ marker 永久 ok=false
    ⇒ 每 10 分钟重装整批）。审计 P2「_stamp 遇异形文件名掀翻整批」。
    ⇒ 现在抛的类型让调用方能**按文件隔离**（记失败 + 原因，其余文件继续）。
    本测试的**契约意图不变**：显式抛错，不返 None。
    """
    mod = jc_read_mod

    with pytest.raises(mod.InvalidFileNameError):
        mod._stamp("not_a_timestamp.txt")
    with pytest.raises(mod.InvalidFileNameError):
        mod._stamp("20251017.json")  # 缺少 - 分隔符
    # 非法时间戳（如 13 月 45 日）也走同一条可区分的路，而不是 datetime 的裸 ValueError
    with pytest.raises(mod.InvalidFileNameError):
        mod._stamp("2026-13-45T99-99__001.jsonl")


# D-124-3: _count_lines
def test_d124_3_count_lines_filters_blank(jc_manifest_mod, tmp_path):
    """D-124-3: jc_manifest._count_lines (L8-14) 数非空行 (L11 ``s.strip()`` 过滤空行)."""
    mod = jc_manifest_mod

    # 多行 + 空行 + whitespace 单一
    f = tmp_path / "test.jsonl"
    f.write_text("\n".join([
        "line1",      # 真行
        "",            # 空行 (过滤)
        "  ",          # 仅空白 (过滤)
        "line3",       # 真行
        "line4",       # 真行
        "",            # 空行
    ]), encoding="utf-8")

    assert mod._count_lines(f) == 3  # 实际 3 个非空行


# D-124-4: _check_entry
def test_d124_4_check_entry_missing_file_returns_rejected(jc_manifest_mod, tmp_path, caplog):
    """D-124-4: _check_entry 文件不存在 → (path, "rejected") + logger.warning."""
    mod = jc_manifest_mod

    missing = tmp_path / "nonexistent.jsonl"
    with caplog.at_level(logging.WARNING):
        out = mod._check_entry("test_topic", missing, decl=10)
    assert out == (missing, "rejected")
    assert "manifest-file-missing" in caplog.text


# D-124-5+6: _terms
def test_d124_5_terms_returns_gf_ga_and_gamma(fit_mod):
    """D-124-5: fit._terms 正常行 → 返回 (gf, ga, atk_terms, def_terms, gamma)."""
    # Mock Match: row = (home, away, gh, ga_) 4 元组
    class MockMatch:
        def __init__(self, h, a, gh, ga):
            self._data = (h, a, gh, ga)
        def __getitem__(self, idx):
            return self._data[idx]

    rows = [
        MockMatch("Home", "Away", 2, 1),  # Home beats Away 2:1
        MockMatch("Away", "Home", 0, 3),  # Return: Away loses 0:3 (so Home's away 3 is ga)
    ]
    names = ["Home", "Away"]
    gf, ga, atk_terms, def_terms, gamma = fit_mod._terms(rows, names)

    # Home: 进球 2 + 3 = 5gf, 失球 1 + 0 = 1ga
    # Away: 进球 1 + 0 = 1gf, 失球 2 + 3 = 5ga
    assert gf["Home"] == 5.0
    assert ga["Home"] == 1.0
    assert gf["Away"] == 1.0
    assert ga["Away"] == 5.0
    # γ = sum(home_goals) / sum(away_goals) = (2+0)/(1+3) = 0.5
    assert gamma == 0.5
    # terms 包含每队
    assert "Home" in atk_terms and "Away" in atk_terms


def test_d124_6_terms_zero_goals_raises_value_error(fit_mod):
    """D-124-6: _terms (L71-72) 某球队 GF/GA 含 0 → ValueError 不许默认 1.0."""
    mod = fit_mod

    class MockMatch:
        def __init__(self, h, a, gh, ga):
            self._data = (h, a, gh, ga)
        def __getitem__(self, idx):
            return self._data[idx]

    # 单场: Home beats Away 1:0 -> Away 的 gf=0 (客胜 0)
    rows = [MockMatch("Home", "Away", 1, 0)]
    names = ["Home", "Away"]

    with pytest.raises(ValueError, match="含 0"):
        mod._terms(rows, names)


# D-124-7: _warn_unwritable_events_once
def test_d124_7_warn_unwritable_events_once_is_idempotent(observability_mod, caplog):
    """D-124-7: _warn_unwritable_events_once (L258-267) 第二次调用不再 log (global flag)."""
    mod = observability_mod

    # 重置 global
    if hasattr(mod, "_UNWRITABLE_EVENTS_WARNED"):
        mod._UNWRITABLE_EVENTS_WARNED = False
    elif hasattr(mod, "_warned"):
        mod._warned = False

    with caplog.at_level(logging.WARNING):
        mod._warn_unwritable_events_once("first reason")
        first_count = sum(1 for r in caplog.records if "events.jsonl unavailable" in r.message)

        mod._warn_unwritable_events_once("second reason")  # 不应再 log
        second_count = sum(1 for r in caplog.records if "events.jsonl unavailable" in r.message)

    # 只应该 log 一次
    assert first_count == 1, f"first call should emit 1 warning, got {first_count}"
    assert second_count == 1, f"second call should NOT emit new warning, got {second_count} total"
