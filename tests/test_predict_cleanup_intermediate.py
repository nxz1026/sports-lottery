"""scripts/predict.py::_cleanup_intermediate 专项测试（R22 #3）。

被测函数：脚本预测完成后清掉运行期中间件（>days 天的 json），保留训练必需 3 文件。
保护清单（_prot_references）：
  - references/historical_past_matches.json     — calibration 历史样本累积
  - references/ml_model_<league>.json          — production 模型权重
  - references/team_translations.json          — i18n 翻译缓存

覆盖：
  · 过期 predictions/results json → 删除
  · 过期 references 下非保护 json → 删除
  · 过期但被保护的 json → 保留（即便超过 days 阈值）
  · 未过期 json（fresh）→ 全部保留
  · 异常 OSError（个别文件 unlink 失败）→ 不抛穿，继续
  · 返回 dict 三档计数正确
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
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


def _set_football_dir(monkeypatch, tmp_path):
    """``_cleanup_intermediate`` 内部 ``from core.config import FOOTBALL_DIR``，
    需要同时 monkeypatch core.config + core.constants（observability 也引）。"""
    import core.config
    import core.constants
    monkeypatch.setattr(core.config, "FOOTBALL_DIR", tmp_path)
    monkeypatch.setattr(core.constants, "FOOTBALL_DIR", tmp_path)


def _touch(p: Path, mtime_offset_seconds: int = 0) -> None:
    """写文件 + 设 mtime（正数 = 改成更早；负数 = 改成更晚）。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    new_mtime = time.time() - mtime_offset_seconds
    os.utime(p, (new_mtime, new_mtime))


# ────────────────────────── happy path：三保护文件保留 ──────────────────────────


def test_cleanup_preserves_training_critical_files(tmp_path, monkeypatch):
    """R22 #3：三保护文件（historical_past_matches / ml_model_* / team_translations）
    即便 mtime 已远超 cutoff，也必须保留（防止 retrain 重训链路被误删）。
    """
    mod = _load_predict_module()
    _set_football_dir(monkeypatch, tmp_path)
    # 让 cutoff = 1 hour，所有文件 mtime 都比 cutoff 旧
    _touch(tmp_path / "predictions" / "prediction_2026-09-01_10.json", mtime_offset_seconds=86400 * 30)
    _touch(tmp_path / "results" / "result_2026-09-01.json", mtime_offset_seconds=86400 * 30)
    _touch(tmp_path / "references" / "historical_past_matches.json", mtime_offset_seconds=86400 * 30)
    _touch(tmp_path / "references" / "ml_model_epl.json", mtime_offset_seconds=86400 * 30)
    _touch(tmp_path / "references" / "team_translations.json", mtime_offset_seconds=86400 * 30)
    # 标记一个非保护 json（应当被删）
    _touch(tmp_path / "references" / "old_calibration_state_epl.json", mtime_offset_seconds=86400 * 30)

    out = mod._cleanup_intermediate(days=7)

    assert not (tmp_path / "predictions" / "prediction_2026-09-01_10.json").exists()
    assert not (tmp_path / "results" / "result_2026-09-01.json").exists()
    assert not (tmp_path / "references" / "old_calibration_state_epl.json").exists()
    # 三保护文件全部保留
    assert (tmp_path / "references" / "historical_past_matches.json").exists()
    assert (tmp_path / "references" / "ml_model_epl.json").exists()
    assert (tmp_path / "references" / "team_translations.json").exists()
    # 返回 dict 计数
    assert out == {"predictions": 1, "results": 1, "references": 1}


def test_cleanup_keeps_fresh_files(tmp_path, monkeypatch):
    """fresh json（mtime > cutoff）→ 全部保留（包括非保护）。"""
    mod = _load_predict_module()
    _set_football_dir(monkeypatch, tmp_path)
    # mtime 写成当前（fresh）→ cutoff 应剔除
    _touch(tmp_path / "predictions" / "prediction_2026-10-14_10.json", mtime_offset_seconds=0)
    _touch(tmp_path / "results" / "result_2026-10-14.json", mtime_offset_seconds=0)
    _touch(tmp_path / "references" / "tmp_state.json", mtime_offset_seconds=0)
    out = mod._cleanup_intermediate(days=7)
    assert out == {"predictions": 0, "results": 0, "references": 0}
    assert (tmp_path / "predictions" / "prediction_2026-10-14_10.json").exists()
    assert (tmp_path / "references" / "tmp_state.json").exists()


# ────────────────────────── 三保护文件名匹配精确性 ──────────────────────────


def test_prot_references_ml_model_prefix_match(tmp_path, monkeypatch):
    """``ml_model_<league>.json``（任何联赛后缀）都被保护；``ml_model`` 无后缀不保护。"""
    mod = _load_predict_module()
    _set_football_dir(monkeypatch, tmp_path)
    # 各联赛 ml_model_* 都保护
    for lg in ("epl", "laliga", "bundesliga", "seriea", "ligue1"):
        _touch(tmp_path / "references" / f"ml_model_{lg}.json",
               mtime_offset_seconds=86400 * 30)
    # ml_model_epl_from_events.json 也被保护（R12 隔离路径，与主模型同保护清单）
    _touch(tmp_path / "references" / "ml_model_epl_from_events.json",
           mtime_offset_seconds=86400 * 30)
    # ml_models_epl.json 不同名字不被保护（保护前缀是 "ml_model_"）
    _touch(tmp_path / "references" / "ml_models_epl.json",
           mtime_offset_seconds=86400 * 30)

    mod._cleanup_intermediate(days=7)

    # 五个 league 主模型 + from_events 隔离路径全部保留
    for lg in ("epl", "laliga", "bundesliga", "seriea", "ligue1"):
        assert (tmp_path / "references" / f"ml_model_{lg}.json").exists()
    assert (tmp_path / "references" / "ml_model_epl_from_events.json").exists()
    # ml_models_epl.json（注意末尾 s）应被删
    assert not (tmp_path / "references" / "ml_models_epl.json").exists()


# ────────────────────────── 异常路径 ──────────────────────────


def test_cleanup_survives_oserror_on_unlink(tmp_path, monkeypatch):
    """个别文件 unlink 抛 OSError → 不让 _cleanup_intermediate 抛穿（main() 次要步骤不计入退出码）。
    """
    mod = _load_predict_module()
    _set_football_dir(monkeypatch, tmp_path)
    _touch(tmp_path / "predictions" / "expired_1.json", mtime_offset_seconds=86400 * 30)
    _touch(tmp_path / "predictions" / "expired_2.json", mtime_offset_seconds=86400 * 30)
    _touch(tmp_path / "results" / "expired_r.json", mtime_offset_seconds=86400 * 30)

    real_unlink = Path.unlink
    call_count = {"n": 0}

    def patched_unlink(self, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 第一次 unlink 失败
            raise OSError("simulated unlink failure")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", patched_unlink)

    # 必须不抛错
    out = mod._cleanup_intermediate(days=7)
    # 第二/第三次 unlink 成功 → 2 个文件实际被删
    # 但 _cleanup_intermediate 不计 OSError 失败的 → removed 计 2
    assert out["predictions"] + out["results"] == 2


def test_cleanup_survives_missing_directories(tmp_path, monkeypatch):
    """缺失 predictions/results/references 目录 → 不抛，按空目录处理。"""
    mod = _load_predict_module()
    _set_football_dir(monkeypatch, tmp_path)
    # 不创建任何子目录
    out = mod._cleanup_intermediate(days=7)
    assert out == {"predictions": 0, "results": 0, "references": 0}


def test_cleanup_days_zero_removes_everything_nonprotected(tmp_path, monkeypatch):
    """days=0 → cutoff=now → 任何已存在 json 都视为过期（非保护路径）。
    但历史过 last few seconds 写完的也立刻过期，验证 cutoff 边界。
    """
    mod = _load_predict_module()
    _set_football_dir(monkeypatch, tmp_path)
    _touch(tmp_path / "predictions" / "fresh.json", mtime_offset_seconds=0)  # 写完立刻过期
    _touch(tmp_path / "references" / "historical_past_matches.json", mtime_offset_seconds=0)
    _touch(tmp_path / "references" / "ml_model_epl.json", mtime_offset_seconds=0)
    _touch(tmp_path / "references" / "team_translations.json", mtime_offset_seconds=0)
    _touch(tmp_path / "references" / "tmp.json", mtime_offset_seconds=0)

    out = mod._cleanup_intermediate(days=0)
    # fresh.json 和 tmp.json 应被清掉；三保护保留
    assert not (tmp_path / "predictions" / "fresh.json").exists()
    assert not (tmp_path / "references" / "tmp.json").exists()
    for protect in ("historical_past_matches.json", "ml_model_epl.json", "team_translations.json"):
        assert (tmp_path / "references" / protect).exists()
    assert out["predictions"] == 1
    assert out["references"] == 1

