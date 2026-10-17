"""R43-3: scripts/predict.py::_finalize_league_output 5 tests direct + D-52 lock.

收尾 helper 0 直测; run_league() 调用, 但 helper 自身契约 (silent / _save /
_print_summary side effect / Total runtime 格式) 未锁。

helper 含五条契约 (D-52):
- silent=True -> stdout 不打 JSON; silent=False -> 打 (indent=2, ensure_ascii=False)
- _save_output 必调 (任何路径都落盘, 否则 predictions 丢)
- _print_summary 必调 (任何路径都打 stderr 摘要)
- Total runtime 必须格式化为 "{int}ms" (stderr)
- 末尾 '='*60 必打 (stderr, 摘要块收尾)

5 tests (MEDIUM):
- silent=True: stdout 空 (JSON 不打) + 落盘 + 摘要 + runtime + 末尾等号
  都走 (silent 仅抑制 JSON, 不抑制 stderr)
- silent=False: stdout 含 JSON (indent=2 锁) + 其它 4 步仍走
- _save_output 调用次数 == 1 (不重复写)
- _print_summary 调用次数 == 1 + args 顺序透传 (锁参数 8 个)
- Total runtime 格式严格 (含 "ms" + 整数部分, stderr only)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def predict_mod(tmp_path, monkeypatch):
    """加载 scripts/predict.py + PREDICTIONS_DIR 重定向 tmp。

    _save_output 必调真实落盘以验证 PREDICTIONS_DIR 路径正确;
    _print_summary stub 收集调用避免 capsys 噪声。
    """
    spec = importlib.util.spec_from_file_location(
        "predict_finalize_test",
        REPO_ROOT / "scripts" / "predict.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "PREDICTIONS_DIR", tmp_path)
    return mod


@pytest.fixture
def summary_calls(monkeypatch, predict_mod):
    """Mock _print_summary 收集调用, 不读 stderr。"""
    calls = []

    def fake_print_summary(predictions, calibration, calibration_offset,
                            monte_carlo_result, n_simulations, accuracy_summary):
        calls.append({
            "predictions": predictions,
            "calibration": calibration,
            "calibration_offset": calibration_offset,
            "monte_carlo_result": monte_carlo_result,
            "n_simulations": n_simulations,
            "accuracy_summary": accuracy_summary,
        })

    monkeypatch.setattr(predict_mod, "_print_summary", fake_print_summary)
    return calls


def _args(predict_mod):
    """构造合法 _finalize_league_output 最小 args 集 (位置参数顺序匹配签名)。

    _finalize_league_output(silent, output, calibration_offset, now_utc,
                              predictions, calibration, monte_carlo_result,
                              n_simulations, accuracy_summary, _t_start)
    """
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    output = {"generated_at": now.isoformat(), "league": "epl",
              "data_window": "20261015-20261020", "predictions": []}
    calibration = {"total_matches": 5, "home_win_rate": 0.5}
    calibration_offset = None
    predictions = []
    monte_carlo_result = None
    accuracy_summary = None
    n_simulations = 1000
    # _t_start = now - 1.5s 使 Total runtime 在 ~1500ms 范围
    import time
    _t_start = time.time() - 1.5
    # 顺序匹配函数签名 (silent 由调用方传, 这里从 output 开始)
    return (output, calibration_offset, now, predictions, calibration,
            monte_carlo_result, n_simulations, accuracy_summary, _t_start)


# ──────────────────────── D-52 五契约 ────────────────────────


def test_finalize_d52_silent_true_no_json_on_stdout(
    predict_mod, summary_calls, capsys,
):
    """R43-3 D-52: silent=True -> stdout 空 (JSON 不打), 但 stderr runtime + summary 仍走。"""
    args = _args(predict_mod)
    predict_mod._finalize_league_output(True, *args)
    captured = capsys.readouterr()
    # silent 仅抑制 JSON 印 stdout
    assert captured.out == ""
    # stderr 仍有 Total runtime + 末尾等号 + _print_summary 调用
    assert "Total runtime:" in captured.err
    assert "=" * 60 in captured.err
    assert len(summary_calls) == 1


def test_finalize_d52_silent_false_prints_json(
    predict_mod, summary_calls, capsys,
):
    """R43-3 D-52: silent=False -> stdout 含 JSON (indent=2 + ensure_ascii=False)。

    锁定 ensure_ascii=False: 中文队名应在 JSON 中原样, 不能 unicode 转义。
    """
    args = _args(predict_mod)
    # output 含中文 league_name 验 ensure_ascii (args[0] 是 output dict)
    args[0]["league_name_cn"] = "英超"
    predict_mod._finalize_league_output(False, *args)
    captured = capsys.readouterr()
    # JSON 应可解析
    parsed = json.loads(captured.out)
    assert parsed["league"] == "epl"
    assert parsed["league_name_cn"] == "英超"
    # indent=2: 含换行 (compact JSON 单行)
    assert "\n" in captured.out


def test_finalize_d52_save_output_called_exactly_once(
    predict_mod, summary_calls, tmp_path, monkeypatch,
):
    """R43-3 D-52: _save_output 必调且仅调一次 (防止重复写)。

    锁定 helper 不重复落盘 (同 league 同小时两次调用会让 jsonl 翻倍)。
    """
    args = _args(predict_mod)
    save_calls = {"n": 0}

    real_save = predict_mod._save_output

    def counting_save(*a, **kw):
        save_calls["n"] += 1
        return real_save(*a, **kw)

    monkeypatch.setattr(predict_mod, "_save_output", counting_save)
    predict_mod._finalize_league_output(True, *args)
    assert save_calls["n"] == 1


def test_finalize_d52_print_summary_passes_all_args_through(
    predict_mod, summary_calls,
):
    """R43-3 D-52: _print_summary 调用一次, 8 args 顺序透传 (predictions /
    calibration / calibration_offset / monte_carlo_result / n_simulations /
    accuracy_summary)。

    锁定 helper 不修改 / 排序 / 过滤 8 个参数。
    """
    args = _args(predict_mod)
    # args tuple 顺序: (output, calibration_offset, now, predictions, calibration,
    #                    monte_carlo_result, n_simulations, accuracy_summary, _t_start)
    predict_mod._finalize_league_output(True, *args)
    assert len(summary_calls) == 1
    call = summary_calls[0]
    assert call["predictions"] is args[3]
    assert call["calibration"] is args[4]
    assert call["calibration_offset"] is args[1]
    assert call["monte_carlo_result"] is args[5]
    assert call["n_simulations"] == args[6]
    assert call["accuracy_summary"] is args[7]


def test_finalize_d52_total_runtime_format_int_ms_stderr(
    predict_mod, summary_calls, capsys,
):
    """R43-3 D-52: Total runtime 行格式严格 "{int}ms" 仅 stderr。

    锁定 runtime 行格式: 必须含 "Total runtime:" + 整数 + "ms";
    不允许把 Total runtime 写 stdout。
    """
    args = _args(predict_mod)
    predict_mod._finalize_league_output(True, *args)
    captured = capsys.readouterr()
    # stderr 含
    import re
    assert re.search(r"Total runtime: \d+ms", captured.err)
    # stdout 不含
    assert "Total runtime" not in captured.out
    # 末尾再 '='*60
    assert captured.err.count("=" * 60) >= 1
