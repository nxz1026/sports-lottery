"""--all 模式落盘文件名回归（2026-09-18 验收修复）。

背景：``_save_output`` 的文件名只有小时精度（``prediction_%Y-%m-%d_%H.json``）且
**不含联赛名**。而 ``--all`` 会在同一次运行里依次跑完全部联赛，于是 6 个联赛写同一个
文件、互相覆盖 —— 实测 5 个足球联赛全部预测成功（各 5-6 场），落盘文件却只剩最后
一个联赛（ligue1）的 6 场，其余全被覆盖。

归并侧 ``store.latest_by_league()`` 读的是 JSON 里的 ``league`` 字段、不解析文件名，
所以给文件名加联赛后缀不影响归并，但能杜绝覆盖。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _load_predict_module():
    spec = importlib.util.spec_from_file_location(
        "predict_mod_for_test", REPO_ROOT / "scripts" / "predict.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_all_mode_writes_one_file_per_league(tmp_path, monkeypatch):
    """同一小时内多个联赛必须各写各的文件，不得互相覆盖。"""
    mod = _load_predict_module()
    monkeypatch.setattr(mod, "PREDICTIONS_DIR", tmp_path)

    now = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)
    leagues = ["epl", "laliga", "bundesliga", "seriea", "ligue1"]
    for lg in leagues:
        mod._save_output({"league": lg, "generated_at": "x", "predictions": [{"match": lg}]},
                         None, now)

    files = sorted(p.name for p in tmp_path.glob("prediction_*.json"))
    assert len(files) == len(leagues), f"应各写一个文件，实际只有 {files}"
    for lg in leagues:
        assert any(lg in name for name in files), f"缺少 {lg} 的文件：{files}"

    # 每个文件的 league 字段必须与文件名后缀一致（无覆盖/错位）
    for p in tmp_path.glob("prediction_*.json"):
        assert json.loads(p.read_text(encoding="utf-8"))["league"] in p.name


def test_missing_league_still_writes_a_file(tmp_path, monkeypatch):
    """league 缺失时不得崩，退回不带后缀的文件名（保持向后兼容）。"""
    mod = _load_predict_module()
    monkeypatch.setattr(mod, "PREDICTIONS_DIR", tmp_path)

    now = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)
    out = mod._save_output({"generated_at": "x", "predictions": []}, None, now)
    assert out.exists()
    assert out.name == "prediction_2026-09-18_06.json"


def test_bball_and_football_do_not_clobber_each_other(tmp_path, monkeypatch):
    """篮球与足球写同一个 PREDICTIONS_DIR：同小时内必须互不覆盖。

    scripts/bball/run.py 的 _save 原本也是 prediction_%Y-%m-%d_%H.json，与足球
    同小时先后跑会互相覆盖（一方数据静默丢失）。两处都已加联赛后缀。
    """
    import importlib.util

    football = _load_predict_module()
    spec = importlib.util.spec_from_file_location(
        "bball_run_for_test", REPO_ROOT / "scripts" / "bball" / "run.py")
    bball = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bball
    spec.loader.exec_module(bball)

    monkeypatch.setattr(football, "PREDICTIONS_DIR", tmp_path)
    monkeypatch.setattr(bball, "PREDICTIONS_DIR", tmp_path)

    now = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)
    football._save_output({"league": "epl", "predictions": [{"match": "a"}]}, None, now)
    bball._save({"league": "nba", "sport": "basketball", "predictions": [{"match": "b"}]}, now)

    files = sorted(p.name for p in tmp_path.glob("prediction_*.json"))
    assert files == ["prediction_2026-09-18_06_epl.json",
                     "prediction_2026-09-18_06_nba.json"], files
    for p in tmp_path.glob("prediction_*.json"):
        assert json.loads(p.read_text(encoding="utf-8"))["league"] in p.name


# ────────────────────────── R22 #1：R13 #3 OSError 兜底边角测 ──────────────────────────


def test_save_output_swallows_oserror_on_predictions_write(tmp_path, monkeypatch):
    """R13 #3（兜底验收）：预测文件写失败 OSError 不让 _save_output 抛穿主流程。

    期望：
      · 不抛错（调用者照常继续）
      · log_error 落 events.jsonl 路径
      · 仍执行 _emit_ml_predictions（即使文件没写）
    """
    mod = _load_predict_module()
    monkeypatch.setattr(mod, "PREDICTIONS_DIR", tmp_path)

    # mock 内置 open 让 json.dump 抛 OSError（prediction file 那次）
    real_open = open

    def patched_open(path, *a, **kw):
        if "prediction_" in str(path):
            raise OSError("simulated write failure on prediction file")
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", patched_open)

    # mock log_error 验证被调
    log_error_calls = []
    from core import observability
    monkeypatch.setattr(observability, "log_error",
                        lambda exc, **kw: log_error_calls.append((exc, kw)))

    # mock _emit_ml_predictions 验证不漏
    emit_calls = []
    monkeypatch.setattr(mod, "_emit_ml_predictions",
                        lambda output, league=None: emit_calls.append(league))

    now = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)
    out_path = mod._save_output({"league": "epl", "predictions": [{"match": "a"}]},
                                 None, now, emit_ml_events=True)
    # 返回路径仍应给出（即使文件没写出），调用方拿到路径以便后续读
    assert "prediction_" in str(out_path)
    # OSError 已 log_error 上报
    assert len(log_error_calls) == 1, f"预测 OSError 必须被 log_error 捕获；实际 {log_error_calls}"
    assert "_save_output.write_predictions" in log_error_calls[0][1].get("context", "")
    # _emit_ml_predictions 仍被调（数据已落 events.jsonl，预测文件失败不影响 ml feedback）
    assert emit_calls == ["epl"], f"OSError 后 _emit_ml_predictions 必须照走；实际 {emit_calls}"


def test_save_output_swallows_oserror_on_calibration_write(tmp_path, monkeypatch):
    """R13 #3（兜底验收）：校准文件写失败 OSError 同样不抛穿。

    验证：log_error 拿到的 context 区分预测 vs 校准 两个写入路径。
    """
    mod = _load_predict_module()
    monkeypatch.setattr(mod, "PREDICTIONS_DIR", tmp_path)

    real_open = open

    def patched_open(path, *a, **kw):
        s = str(path)
        if "pred_calibration" in s:
            raise OSError("simulated write failure on calibration file")
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", patched_open)

    log_error_calls = []
    from core import observability
    monkeypatch.setattr(observability, "log_error",
                        lambda exc, **kw: log_error_calls.append((exc, kw)))
    monkeypatch.setattr(mod, "_emit_ml_predictions", lambda output, league=None: None)

    now = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)
    # calibration_offset 给非 None 触发校准写路径
    cal = {"h": 0.02, "d": -0.01, "a": -0.01}
    out_path = mod._save_output({"league": "epl", "predictions": [{"match": "a"}]},
                                 cal, now, emit_ml_events=True)
    # 预测文件应写出（only calibration 模拟失败）
    assert out_path.exists(), "预测文件不被 calibration OSError 影响"
    # calibration OSError 落 log_error
    cal_log = [c for c in log_error_calls if "calibration" in c[1].get("context", "")]
    assert len(cal_log) == 1, f"校准 OSError 必须被 log_error 捕获；实际 {log_error_calls}"
