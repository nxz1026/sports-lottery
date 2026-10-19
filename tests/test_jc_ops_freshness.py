"""R50-2: web/routers/jc_ops.py::freshness 4 cases direct + D-93 lock.

freshness (L51-L72, 22 行) topic 新鲜度视图:
1) th 限制在 [0.5, 168.0] (L61 max/min 闭区间)
2) th 解析失败 -> th=24.0 (L62-63)
3) jc_ops_view.ops_snapshot() 异常 -> topics=[] (L64-69)
4) evaluate_topics(topics, threshold_hours=th) -> result
5) result["alerts"] = format_alert_lines(result)
6) 返 result

R44-5/R46-5/R50-1 D-60/D-72/D-92 已锁 accuracy / health / compute_haf,
但 freshness 路由 helper 自身核心契约 (th 限位 + 解析兜底 + snapshot
异常降级 + alerts 字段) 0 直测。

D-93 锁定 4 契约:
- D-93-1: 主流程 happy - 返 evaluate_topics 结果 + alerts 字段
- D-93-2: th 范围限位 (0.5 ~ 168.0, 含边界 clamp)
- D-93-3: th 解析失败 (字符串/None) -> th=24.0
- D-93-4: ops_snapshot 异常 -> topics=[] + 仍返 result (降级)

依赖 stub:
- jc_ops_view.ops_snapshot (monkeypatch 返受控 dict)
- web.services.freshness.evaluate_topics + format_alert_lines (monkeypatch)
- require_auth 路由装饰器 - 跳过 FastAPI (直接调函数)
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def jc_ops_mod(monkeypatch):
    """Spec-load jc_ops.py + stub 下游依赖。"""
    spec = importlib.util.spec_from_file_location(
        "jc_ops_freshness_test",
        REPO_ROOT / "web" / "routers" / "jc_ops.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 默认 stub
    def fake_snapshot():
        return {"topics": [{"topic": "x", "since_latest_hours": 5.0}]}

    # stub store.jc_ops_view: 必须在 store package 的 namespace 上 setattr stub
    # (实测 sys.modules 子模块替换对 `from store import jc_ops_view` 无效——
    #  Python in `from X import Y` 优先查 X.__dict__, 不 fallback 到子模块 import)
    jc_view_stub = types.ModuleType("store.jc_ops_view")
    jc_view_stub.ops_snapshot = fake_snapshot
    import store  # noqa: PLC0415 - ensure `store` package is loaded
    # store.__init__.py 是空文件, jc_ops_view 不在 namespace 上, 用 raising=False
    # 让 monkeypatch 在属性不存在时也能 setattr (退出时 delattr, 不 fail)
    monkeypatch.setattr(store, "jc_ops_view", jc_view_stub, raising=False)
    # 同步替换 sys.modules 子模块 (D-93-4 用 `import store.jc_ops_view as ...` 时也生效)
    monkeypatch.setitem(sys.modules, "store.jc_ops_view", jc_view_stub)

    # stub web.services.freshness (用字符串路径避免 stale module reference)
    monkeypatch.setattr("web.services.freshness.evaluate_topics",
                        lambda topics, threshold_hours: {"ok": True,
                                                            "threshold_hours": threshold_hours,
                                                            "stale": [],
                                                            "all": len(topics)})
    monkeypatch.setattr("web.services.freshness.format_alert_lines",
                        lambda r: ["alert-line"])
    return mod


# ──────────────────────── D-93 四契约 ────────────────────────


def test_freshness_d93_1_happy_returns_evaluate_with_alerts(jc_ops_mod):
    """R50-2 D-93-1: 主流程 happy - 返 evaluate_topics 结果 + alerts 字段。

    snapshot 返 1 topic -> evaluate_topics 返 {"ok": True, "all": 1, ...} ->
    result["alerts"] = ["alert-line"] (format_alert_lines stub)。
    """
    out = jc_ops_mod.freshness(threshold_hours=24.0)
    # 评估结果透传
    assert out["ok"] is True
    assert out["threshold_hours"] == 24.0
    assert out["all"] == 1
    # alerts 字段 (L71)
    assert out["alerts"] == ["alert-line"]


def test_freshness_d93_2_threshold_clamp_to_range(jc_ops_mod):
    """R50-2 D-93-2: th 范围限位 (0.5 ~ 168.0, 含边界 clamp)。

    L61 `th = max(0.5, min(float(threshold_hours), 168.0))`:
    - 0.1 -> clamp 0.5
    - 200 -> clamp 168.0
    - 24 -> 原值
    - 0.5 (边界) -> 0.5
    - 168.0 (边界) -> 168.0
    """
    # 0.1 -> 0.5
    out_low = jc_ops_mod.freshness(threshold_hours=0.1)
    assert out_low["threshold_hours"] == 0.5
    # 200 -> 168.0
    out_high = jc_ops_mod.freshness(threshold_hours=200)
    assert out_high["threshold_hours"] == 168.0
    # 24 -> 24
    out_mid = jc_ops_mod.freshness(threshold_hours=24.0)
    assert out_mid["threshold_hours"] == 24.0
    # 边界 0.5 -> 0.5
    out_lo_edge = jc_ops_mod.freshness(threshold_hours=0.5)
    assert out_lo_edge["threshold_hours"] == 0.5
    # 边界 168 -> 168
    out_hi_edge = jc_ops_mod.freshness(threshold_hours=168.0)
    assert out_hi_edge["threshold_hours"] == 168.0


def test_freshness_d93_3_invalid_threshold_falls_back_to_24(jc_ops_mod):
    """R50-2 D-93-3: th 解析失败 (字符串/None) -> th=24.0。

    L62-63 `except (TypeError, ValueError): th = 24.0`。
    """
    # 字符串非数字
    out_str = jc_ops_mod.freshness(threshold_hours="abc")
    assert out_str["threshold_hours"] == 24.0
    # None 触发 TypeError
    out_none = jc_ops_mod.freshness(threshold_hours=None)
    assert out_none["threshold_hours"] == 24.0
    # 列表触发 TypeError
    out_list = jc_ops_mod.freshness(threshold_hours=[1, 2])
    assert out_list["threshold_hours"] == 24.0


def test_freshness_d93_4_snapshot_raises_degrades_to_empty_topics(jc_ops_mod, monkeypatch):
    """R50-2 D-93-4: ops_snapshot 异常 -> topics=[] + 仍返 result (降级)。

    L67-69 `except Exception: topics = []` 不影响后续 evaluate_topics。
    返 {"all": 0, ...} 仍带 alerts。
    """
    # 改 stub 让 snapshot 抛错
    import store.jc_ops_view as ops_view_mod
    def boom():
        raise RuntimeError("db connection lost")
    monkeypatch.setattr(ops_view_mod, "ops_snapshot", boom)

    out = jc_ops_mod.freshness(threshold_hours=24.0)
    # topics=[] -> evaluate_topics 返 all=0
    assert out["all"] == 0
    # 仍返完整 result + alerts (不 5xx)
    assert out["threshold_hours"] == 24.0
    assert out["alerts"] == ["alert-line"]
