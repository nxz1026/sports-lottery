"""R50-1: web/services/haf.py::compute_haf + _pois 4 cases direct + D-92 lock.

web/services/haf.py 实际是 P2-12/P2-25 迁移后的薄包装:
- _pois (L30-L37): NotImplementedError 兜底 (兼容旧 import 路径)
- compute_haf (L40-L46): 委托 derive.haf.probs (同签名)

R44-5/R47-7 D-60/D-80 已锁 accuracy._collect_league_data /
poisson_hhad, 但 compute_haf + _pois 自身核心契约 (raise 兜底 + 委托 + 透传)
0 直测。

D-92 锁定 4 契约:
- D-92-1: _pois 永远 raise NotImplementedError (L35-37 兜底)
- D-92-2: _pois raise 消息含 "已迁移到 scripts/derive/haf" (L36 提示)
- D-92-3: compute_haf 委托 derive.haf.probs (spy 验证调用)
- D-92-4: compute_haf 同名同参透传 (lambda_home/lambda_away/line/max_goals)

依赖 stub:
- derive.haf.probs (monkeypatch 返受控 dict)
- _pois spy NotImplementedError 触发
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def haf_mod(monkeypatch):
    """Spec-load web/services/haf.py + stub derive.haf.probs。"""
    spec = importlib.util.spec_from_file_location(
        "haf_compute_haf_test",
        REPO_ROOT / "web" / "services" / "haf.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # derive.haf 真模块已通过 sys.path 解析, stub probs
    from derive import haf as derive_haf
    monkeypatch.setattr(derive_haf, "probs",
                        lambda lh, la, line=0, max_goals=10: {
                            "HT_RATIO": 0.3, "lambda_h": lh, "lambda_a": la,
                            "line": line, "max_goals": max_goals,
                            "result": "stub"
                        })
    return mod


# ──────────────────────── D-92 四契约 ────────────────────────


def test_pois_d92_1_always_raises_not_implemented(haf_mod):
    """R50-1 D-92-1: _pois 永远 raise NotImplementedError (L35-37 兜底)。

    LOCK: _pois 不会真跑 (L31-34 注释明示), 仅 raise。
    """
    with pytest.raises(NotImplementedError):
        haf_mod._pois(1.5, 10)


def test_pois_d92_2_error_message_indicates_migration(haf_mod):
    """R50-1 D-92-2: _pois raise 消息含 "已迁移到 scripts/derive/haf" (L36 提示)。

    LOCK: 错误消息指引调用方改 import 路径。
    """
    with pytest.raises(NotImplementedError) as exc_info:
        haf_mod._pois(1.5, 10)
    assert "已迁移到 scripts/derive/haf" in str(exc_info.value)


def test_compute_haf_d92_3_delegates_to_derive_haf_probs(haf_mod, monkeypatch):
    """R50-1 D-92-3: compute_haf 委托 derive.haf.probs (spy 验证调用)。

    L45-46 `from derive.haf import probs as _probs_impl; return _probs_impl(...)`
    """
    from derive import haf as derive_haf
    call_count = {"i": 0}

    def spy_probs(lh, la, line=0, max_goals=10):
        call_count["i"] += 1
        return {"spy": True, "lh": lh, "la": la}

    monkeypatch.setattr(derive_haf, "probs", spy_probs)
    out = haf_mod.compute_haf(1.5, 1.0, line=-0.5, max_goals=8)
    assert call_count["i"] == 1
    assert out == {"spy": True, "lh": 1.5, "la": 1.0}


def test_compute_haf_d92_4_passes_all_args_through(haf_mod, monkeypatch):
    """R50-1 D-92-4: compute_haf 同名同参透传 (lambda_home/lambda_away/line/max_goals)。

    默认 line=0, max_goals=10; 传 line=2.5, max_goals=12 应都透传。
    """
    from derive import haf as derive_haf
    captured = {}

    def fake_probs(lh, la, line=0, max_goals=10):
        captured["lh"] = lh
        captured["la"] = la
        captured["line"] = line
        captured["max_goals"] = max_goals
        return {"ok": True}

    monkeypatch.setattr(derive_haf, "probs", fake_probs)
    out = haf_mod.compute_haf(2.0, 0.8, line=2.5, max_goals=12)
    assert captured == {"lh": 2.0, "la": 0.8, "line": 2.5, "max_goals": 12}
    assert out == {"ok": True}
    # 默认值 (不传 line/max_goals) - L40 default
    out_default = haf_mod.compute_haf(1.0, 1.0)
    assert captured["line"] == 0  # default
    assert captured["max_goals"] == 10  # default