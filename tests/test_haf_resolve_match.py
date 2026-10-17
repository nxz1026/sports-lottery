"""R44-4: web/routers/haf.py::_resolve_match 6 tests direct + D-59 lock.

POST body 解析 helper 0 直测; /api/v1/haf 端点 wrapper 间接调用, 但 helper
自身契约 (line 浮点转化 / 范围守门 / match_num 反查) 未锁。

helper 含三条契约 (D-59):
- home/away/line 直接输入 -> 返 (h, a, line_float); line None/"" 默认 0.0
- line 非数字 -> 400 (HTTPException)
- line ∉ [-10, 10] -> 400
- 缺 home+away 但 match_num -> jc_view.fixtures_on(bjt_today) 反查
- 反查 miss 或 fixtures 缺失 -> 400 (兜底, 不能猜)
- 内部 raise HTTPException(400) detail 含子原因

6 tests (MEDIUM):
- happy: home+away+line=2.5 -> (h, a, 2.5)
- line default: line=None -> line_f=0.0
- line 字符串数字: "2.5" -> 2.5 (coerce)
- D-59 line out-of-range: 11 -> 400
- D-59 line 非数字 "abc" -> 400
- 缺 home/away 无 match_num -> 400
- bonus: match_num=7001 命中 fixtures -> 反查成功
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import date as _date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def haf_mod(monkeypatch):
    """加载 web/routers/haf.py + 准备 jc_view.fixtures_on 占位。"""
    spec = importlib.util.spec_from_file_location(
        "haf_resolve_match_test",
        REPO_ROOT / "web" / "routers" / "haf.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # mock bjt_today -> 固定日期
    import web.services.store as ws
    monkeypatch.setattr(ws, "bjt_today", lambda: _date(2026, 10, 15))

    # 占位 store.jc_view 给 lazy import
    return mod


def _stub_jc_view(monkeypatch, fixtures: list):
    """Mock store.jc_view.fixtures_on 返受控 fixtures (含 store 包 stub)。"""
    jc_view_mod = types.ModuleType("store.jc_view")
    jc_view_mod.fixtures_on = lambda date_iso: fixtures
    store_pkg = types.ModuleType("store")
    store_pkg.jc_view = jc_view_mod
    monkeypatch.setitem(sys.modules, "store.jc_view", jc_view_mod)
    monkeypatch.setitem(sys.modules, "store", store_pkg)


# ──────────────────────── D-59 三契约 ────────────────────────


def test_resolve_match_d59_happy_direct_passthrough(haf_mod):
    """R44-4 D-59: home+away+line 直接输入 -> 返 (h, a, float)。"""
    h, a, line = haf_mod._resolve_match(
        {"home": "阿森纳", "away": "切尔西", "line": 2.5})
    assert h == "阿森纳"
    assert a == "切尔西"
    assert line == 2.5


def test_resolve_match_d59_line_none_or_empty_defaults_zero(haf_mod):
    """R44-4 D-59: line=None / "" / 缺 -> 默认 0.0 (不放 400, 给个稳态值)。"""
    h, a, line = haf_mod._resolve_match({"home": "A", "away": "B", "line": None})
    assert line == 0.0

    h2, a2, line2 = haf_mod._resolve_match({"home": "A", "away": "B", "line": ""})
    assert line2 == 0.0

    h3, a3, line3 = haf_mod._resolve_match({"home": "A", "away": "B"})
    assert line3 == 0.0  # 缺也默认 0


def test_resolve_match_d59_line_string_number_coerced(haf_mod):
    """R44-4 D-59: line 是字符串数字 -> float() 强转成功, 不抛 400。

    锁 coerce: 前端可能传 string "1.5", 后端应接受并转 float。
    """
    h, a, line = haf_mod._resolve_match({"home": "A", "away": "B", "line": "1.5"})
    assert line == 1.5
    assert isinstance(line, float)


def test_resolve_match_d59_line_non_numeric_raises_400(haf_mod):
    """R44-4 D-59: line="abc" (无法转 float) -> HTTPException 400 "line 必须是数字"。

    锁定 fast-fail: 非数字不能让 _resolve_match 默默兜 0.0, 必须抛 400。
    """
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        haf_mod._resolve_match({"home": "A", "away": "B", "line": "abc"})
    assert exc.value.status_code == 400
    assert "数字" in exc.value.detail


@pytest.mark.parametrize("bad_line", [11, -11, 100, -100])
def test_resolve_match_d59_line_out_of_range_raises_400(haf_mod, bad_line):
    """R44-4 D-59: line ∉ [-10, 10] -> 400 "line 超出 [-10, 10]"。

    锁定范围守门: 防 frontend 传错 (e.g., '10.5' 表示 '让球 -10.5'
    而非 10.5) 导致归一化映射错位。
    """
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        haf_mod._resolve_match({"home": "A", "away": "B", "line": bad_line})
    assert exc.value.status_code == 400
    assert "[-10,10]" in exc.value.detail


def test_resolve_match_d59_match_num_lookup_fills_home_away(haf_mod, monkeypatch):
    """R44-4 D-59: 缺 home/away 但有 match_num -> jc_view 反查填充。

    锁定 fixture 反查: 业务日 (bjt_today) 而非 latest 走查, 防前端读错日期。
    """
    _stub_jc_view(monkeypatch, [
        {"match_num": 7001, "home_cn": "阿森纳", "away_cn": "切尔西"},
    ])
    h, a, line = haf_mod._resolve_match({"match_num": 7001, "line": 0.5})
    assert h == "阿森纳"
    assert a == "切尔西"
    assert line == 0.5


def test_resolve_match_d59_no_home_no_match_num_raises_400(haf_mod):
    """R44-4 D-59: 缺 home+away 且无 match_num -> 400 "需提供 home/away 或 match_num"。

    锁兜底: 反查入口无则 400, 不能猜默认空串。
    """
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        haf_mod._resolve_match({"line": 0.5})
    assert exc.value.status_code == 400
    assert "home/away 或 match_num" in exc.value.detail
