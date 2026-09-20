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