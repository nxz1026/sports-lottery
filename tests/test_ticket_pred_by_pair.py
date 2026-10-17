"""R42-5: web/routers/ticket.py::_pred_by_pair 3 tests + D-48 silent-fallback lock.

ticket 端反查字典 helper 0 直测; ticket_sfc 端点间接消费, 但 helper 自身
"异常 -> {}" 降级契约未锁。与 haf/combo 同源 fallback 协议需对齐 (同一异常
行为)。

helper 含三条契约 (D-48):
- pred_index_by_pair 返 dict -> 原样透传
- pred_index_by_pair 抛异常 -> 返 {} (silent fallback), logger.warning 写
  日志 (降级语义)
- happy 路径返 dict 与 `_match_probs` (R40-5 已锁) 消费契约对齐

3 tests (MEDIUM):
- happy path: pred_index_by_pair stub -> 透传
- 异常路径: pred_index_by_pair 抛 RuntimeError -> 返 {} + warning 记录
- D-48 异常契约与 haf / combo 对齐: 同 capture log 调用, 锁定返 {}
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def ticket_mod(monkeypatch):
    """加载 web/routers/ticket.py (避免触发 _match_probs 间接路径)。

    ticket.py 模块已 import 链长 (web.services.store.pred_index_by_pair 等),
    直接 import 会加载完整模块。这是 R40-5 路径, spec_from_file_location 隔离
    sys.modules 名。
    """
    spec = importlib.util.spec_from_file_location(
        "ticket_pred_by_pair_test",
        REPO_ROOT / "web" / "routers" / "ticket.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-48 三契约 ────────────────────────


def test_pred_by_pair_d48_happy_path_passes_through(ticket_mod, monkeypatch):
    """R42-5 D-48: pred_index_by_pair 返受控 dict -> _pred_by_pair 透传。

    锁定 happy 不修改; 任何 silent filtering (例如丢空 dict) 都是契约漂移。
    """
    fake_pred = {("Liverpool", "ManU"): {"home": "Liverpool", "away": "ManU",
                                        "reasoning_factors": {}}}

    def fake_pred_index_by_pair():
        return fake_pred

    # ticket._pred_by_pair 内部 lazy import; 我们 monkeypatch web.services.store
    import web.services.store as web_store
    monkeypatch.setattr(web_store, "pred_index_by_pair", fake_pred_index_by_pair)

    out = ticket_mod._pred_by_pair()
    assert out is fake_pred


def test_pred_by_pair_d48_exception_returns_empty_dict(ticket_mod, monkeypatch, caplog):
    """R42-5 D-48: pred_index_by_pair 抛任何异常 -> _pred_by_pair 返 {}。

    锁 silent fallback: 上游 LRU cache 失效 / store 半损坏 / import 失败等
    都不能让 ticket 端点 500。
    """
    def fake_pred_index_by_pair():
        raise RuntimeError("simulated store failure")

    import web.services.store as web_store
    monkeypatch.setattr(web_store, "pred_index_by_pair", fake_pred_index_by_pair)

    with caplog.at_level(logging.WARNING, logger="web.ticket"):
        out = ticket_mod._pred_by_pair()
    assert out == {}
    assert isinstance(out, dict)
    # warning 日志记录失败原因 (silent 但 not silent-complete)
    assert any("pred_index_by_pair failed" in r.message for r in caplog.records)


def test_pred_by_pair_d48_consistent_with_haf_combo_fallback(ticket_mod, monkeypatch):
    """R42-5 D-48: 与 haf / combo 同源契约 — 异常返 {} 锁定。

    haf._find_prediction / combo._find_fix 也走 pred_index_by_pair 包装;
    锁定 ticket 端口与它们一致同 fallback, 避免不同端口契约漂移。
    """
    def fake_pred_index_by_pair():
        raise ValueError("bad cache state")

    import web.services.store as web_store
    monkeypatch.setattr(web_store, "pred_index_by_pair", fake_pred_index_by_pair)

    # ticket 端返 {} (而非抛穿 / 改别的形式)
    assert ticket_mod._pred_by_pair() == {}
    # 二次调用: 同样 fallback, 幂等
    assert ticket_mod._pred_by_pair() == {}
