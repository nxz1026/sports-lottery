from __future__ import annotations

import os
import time

import scripts.predict as P


def _stale_model(base, league: str):
    """在 base 下造一个过期模型文件。"""
    p = base / f"ml_model_{league}.json"
    p.write_text("{}", encoding="utf-8")
    # 把 mtime 拨到 10 天前 → 必过期
    old = time.time() - 10 * 86400
    os.utime(p, (old, old))
    return p


def test_refresh_ml_retrains_stale_model(monkeypatch, tmp_path):
    _stale_model(tmp_path, "epl")
    called = []

    def fake_train(league_key, base_dir=None, days=365, elo_ratings=None):
        called.append(league_key)
        d = type("M", (), {})()
        return d

    monkeypatch.setattr("core.model.ml_model.train_league_model", fake_train)
    out = P._refresh_ml_models("epl", max_age_days=1.0, base_dir=tmp_path)
    assert called == ["epl"]
    assert out == {"epl": True}


def test_refresh_ml_skips_fresh_model(monkeypatch, tmp_path):
    # 刚写的模型：不算过期 → 不重训
    (tmp_path / "ml_model_epl.json").write_text("{}", encoding="utf-8")
    called = []
    monkeypatch.setattr("core.model.ml_model.train_league_model",
                        lambda *a, **k: called.append("x"))
    out = P._refresh_ml_models("epl", max_age_days=1.0, base_dir=tmp_path)
    assert called == []          # 未触发训练
    assert out == {}


def test_refresh_ml_missing_model_retrains(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr("core.model.ml_model.train_league_model",
                        lambda *a, **k: called.append("epl") or object())
    out = P._refresh_ml_models("epl", max_age_days=1.0, base_dir=tmp_path)
    assert called == ["epl"]
    assert out == {"epl": True}


def test_refresh_ml_survives_train_failure(monkeypatch, tmp_path):
    _stale_model(tmp_path, "epl")

    def boom(*a, **k):
        raise RuntimeError("train failed")

    monkeypatch.setattr("core.model.ml_model.train_league_model", boom)
    out = P._refresh_ml_models("epl", max_age_days=1.0, base_dir=tmp_path)
    assert out == {"epl": None}   # 异常被捕获，不抛穿


def _mk_intermediate(tmp_path, files):
    """files: {relpath: age_days} 在 FOOTBALL_DIR 结构下造文件。"""
    for rel, age in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
        if age:
            old = time.time() - age * 86400
            os.utime(p, (old, old))
    monkeypatch_need = None
    return tmp_path


def test_cleanup_intermediate_removes_old_preserves_training(tmp_path, monkeypatch):
    files = {
        "predictions/pred_old.json": 20,   # 旧中间件
        "predictions/pred_new.json": 1,    # 新鲜
        "results/result_old.json": 30,     # 旧
        "references/historical_past_matches.json": 999,  # 训练必需
        "references/ml_model_epl.json": 999,             # 训练必需
        "references/team_translations.json": 999,        # 参照必需
        "references/.calibration_state.json": 50,        # 旧中间件
    }
    for rel, age in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
        if age:
            old = time.time() - age * 86400
            os.utime(p, (old, old))
    monkeypatch.setattr("core.config.FOOTBALL_DIR", str(tmp_path))
    out = P._cleanup_intermediate(days=7)
    assert out["predictions"] == 1
    assert out["results"] == 1
    assert (tmp_path / "predictions" / "pred_old.json").exists() is False
    assert (tmp_path / "predictions" / "pred_new.json").exists() is True
    assert (tmp_path / "results" / "result_old.json").exists() is False
    # 训练/参照必需：无论多旧都保留
    assert (tmp_path / "references" / "historical_past_matches.json").exists()
    assert (tmp_path / "references" / "ml_model_epl.json").exists()
    assert (tmp_path / "references" / "team_translations.json").exists()
    # 旧校准状态被清
    assert (tmp_path / "references" / ".calibration_state.json").exists() is False