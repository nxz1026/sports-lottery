"""R47-5: web/services/store.py::calibration_states 4 cases direct + D-78 lock.

calibration_states (L297-L309, 13 行) 读 OUTPUT_DIR/references/
.calibration_state_{league}.json (契约 §2.4), 坏文件跳过。web 路由层
/status 等依赖, R45-4 已测 _json_load / _file_dt, 但 calibration_states
自身核心契约 (glob pattern + league 提取 + 坏文件跳过 + 目录缺失) 0 直测。

D-78 锁定 4 契约:
- D-78-1: 主流程 happy (2 league 各 1 file) -> 2 entries
- D-78-2: 目录不存在 -> 返 {} (logger.warning)
- D-78-3: 坏文件 (JSON 解析失败) -> 跳过该 file
- D-78-4: 非 .calibration_state_* 前缀文件不被识别 (LOCK 命名约定)

依赖 stub:
- OUTPUT_DIR (monkeypatch 指向 tmp_path)
- 写 .calibration_state_*.json fixture 模拟真实状态文件
- _json_load 真跑 (L27 已测试, 真读)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def store_setup(tmp_path, monkeypatch):
    """Spec-load store.py, 重定向 OUTPUT_DIR + 清模块级缓存。"""
    spec = importlib.util.spec_from_file_location(
        "store_calibration_states_test",
        REPO_ROOT / "web" / "services" / "store.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # 重定向 OUTPUT_DIR 到 tmp_path
    monkeypatch.setattr(mod, "OUTPUT_DIR", tmp_path)
    # 清缓存
    if hasattr(mod, "_load_prediction_docs_cached"):
        mod._load_prediction_docs_cached.clear()
    if hasattr(mod, "_pred_index_by_pair_cached"):
        mod._pred_index_by_pair_cached.clear()
    return mod, tmp_path


# ──────────────────────── D-78 四契约 ────────────────────────


def test_calibration_states_d78_1_happy_two_leagues(store_setup):
    """R47-5 D-78-1: 主流程 happy - 2 league 各 1 file -> 2 entries。

    epl/.calibration_state_epl.json + laliga/.calibration_state_laliga.json
    -> {epl: {calibrated: True}, laliga: {calibrated: False}}。
    """
    mod, tmp = store_setup
    refs = tmp / "references"
    refs.mkdir()
    (refs / ".calibration_state_epl.json").write_text(
        json.dumps({"calibrated": True, "last_run": "2026-07-15"}),
        encoding="utf-8")
    (refs / ".calibration_state_laliga.json").write_text(
        json.dumps({"calibrated": False}),
        encoding="utf-8")

    out = mod.calibration_states()
    assert out == {
        "epl": {"calibrated": True, "last_run": "2026-07-15"},
        "laliga": {"calibrated": False},
    }


def test_calibration_states_d78_2_missing_dir_returns_empty(store_setup, caplog):
    """R47-5 D-78-2: 目录不存在 -> 返 {} + logger.warning。

    L301-L303: not refs.is_dir() -> logger.warning + 返 {}。
    """
    mod, tmp = store_setup
    # 不创建 references/ 子目录
    import logging
    with caplog.at_level(logging.WARNING, logger="web.services.store"):
        out = mod.calibration_states()
    assert out == {}
    assert any("校准状态目录缺失" in r.message for r in caplog.records)


def test_calibration_states_d78_3_corrupt_file_skipped(store_setup):
    """R47-5 D-78-3: 坏文件 (JSON 解析失败) -> 跳过该 file, 其它正常 file 仍加载。

    L306-L308: data = _json_load(f); if data is not None: states[league] = data
    _json_load 对坏 JSON 返 None (D-31 锁定), 跳过。
    """
    mod, tmp = store_setup
    refs = tmp / "references"
    refs.mkdir()
    # 1 个正常 + 1 个坏
    (refs / ".calibration_state_epl.json").write_text(
        json.dumps({"calibrated": True}), encoding="utf-8")
    (refs / ".calibration_state_seriea.json").write_text(
        "this is not valid json {{", encoding="utf-8")

    out = mod.calibration_states()
    # seriea 坏文件被跳过, epl 仍加载
    assert "epl" in out
    assert "seriea" not in out
    assert out["epl"] == {"calibrated": True}


def test_calibration_states_d78_4_non_matching_files_ignored(store_setup):
    """R47-5 D-78-4: 非 .calibration_state_*.json 前缀文件不被识别。

    LOCK: glob pattern `'.calibration_state_*.json'` 严格匹配, 其它文件
    (e.g. `.calibration.json` / `calibration_state_epl.json` 无前导点) 不命中。
    """
    mod, tmp = store_setup
    refs = tmp / "references"
    refs.mkdir()
    # 1 个正确 + 1 个无前导点 + 1 个其它名字
    (refs / ".calibration_state_epl.json").write_text(
        json.dumps({"calibrated": True}), encoding="utf-8")
    (refs / "calibration_state_laliga.json").write_text(  # 无前导点
        json.dumps({"calibrated": False}), encoding="utf-8")
    (refs / ".other_file.json").write_text(
        json.dumps({"foo": "bar"}), encoding="utf-8")

    out = mod.calibration_states()
    # 只 epl 命中
    assert list(out.keys()) == ["epl"]
    assert out["epl"] == {"calibrated": True}
