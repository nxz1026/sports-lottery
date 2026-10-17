"""R42-3: web/routers/combo.py::combo_eval 5 tests direct + D-46 lock.

combo_eval 路由 handler 仅 test_combo_router.py 经 TestClient 集成覆盖,
0 直测。本轮补 5 直测锁定 5 条降级契约。

helper 含五条契约 (D-46):
1. payload 缺 legs + selections -> 400 "legs 或 selections 必须是数组"
2. selections > 32 -> 400 "selections 最多 32 条"
3. legs > 32 -> 400 "legs 最多 32 条"
4. joint_prob 越界 (<0 | >1) -> 400; 非数字 -> 400
5. stake 默认 2.0; stake 异常 / <= 0 -> 回退 2.0
6. 正常 path -> 返回 combo_mod.evaluate_combo() 透传 (mocked)

5 tests (MEDIUM): 直接 import + 调 combo_eval 函数 (传 None 作 auth dep)
+ monkeypatch combo_mod.evaluate_combo 返回受控 dict。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.routers import combo as combo_mod
from web.routers.combo import combo_eval


def _request():
    """最小 starlette Request (combo_eval 接收但未用其内容)。"""
    from starlette.requests import Request
    return Request(scope={"type": "http", "method": "POST",
                          "headers": [(b"content-type", b"application/json")]})


# ──────────────────────── D-46 五契约 ────────────────────────


def test_combo_eval_d46_missing_legs_selections_raises_400(monkeypatch):
    """R42-3 D-46: payload 既无 legs 也无 selections -> HTTPException 400。

    锁定"两段式互斥"契约; 同时缺应明确报 400 而不是默默猜 default。
    """
    monkeypatch.setattr(combo_mod.combo_mod, "evaluate_combo",
                        lambda legs, joint_prob=None, stake=2.0: {"ok": True})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        combo_eval(_request(), payload={}, _=None)
    assert exc.value.status_code == 400
    assert "legs 或 selections" in exc.value.detail


def test_combo_eval_d46_selections_over_32_raises_400(monkeypatch):
    """R42-3 D-46: selections > 32 -> 400 (防组合爆炸)。"""
    monkeypatch.setattr(combo_mod.combo_mod, "evaluate_combo",
                        lambda legs, joint_prob=None, stake=2.0: {})
    # _assemble_legs 也 stub 防 network 调 model
    monkeypatch.setattr(combo_mod, "_assemble_legs",
                        lambda s: [{"side": "主胜", "odds": 1.5, "p_model": 0.5}] * len(s))
    from fastapi import HTTPException
    sels = [{"match_num": i, "side": "主胜"} for i in range(33)]
    with pytest.raises(HTTPException) as exc:
        combo_eval(_request(), payload={"selections": sels}, _=None)
    assert exc.value.status_code == 400
    assert "selections 最多 32" in exc.value.detail


def test_combo_eval_d46_legs_over_32_raises_400(monkeypatch):
    """R42-3 D-46: legs > 32 -> 400 (防组合爆炸)。"""
    monkeypatch.setattr(combo_mod.combo_mod, "evaluate_combo",
                        lambda legs, joint_prob=None, stake=2.0: {})
    from fastapi import HTTPException
    legs = [{"side": "主胜", "odds": 1.5, "p_model": 0.5}] * 33
    with pytest.raises(HTTPException) as exc:
        combo_eval(_request(), payload={"legs": legs}, _=None)
    assert exc.value.status_code == 400
    assert "legs 最多 32" in exc.value.detail


@pytest.mark.parametrize("bad_jp", [1.5, -0.1, "not_a_number"])
def test_combo_eval_d46_joint_prob_invalid_raises_400(monkeypatch, bad_jp):
    """R42-3 D-46: joint_prob 越界 / 非数字 -> 400 (锁定 [0, 1] 范围守卫)。

    三种 invalid 形态: >1, <0, 非数字。
    """
    monkeypatch.setattr(combo_mod.combo_mod, "evaluate_combo",
                        lambda legs, joint_prob=None, stake=2.0: {})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        combo_eval(_request(), payload={
            "legs": [{"side": "主胜", "odds": 1.8, "p_model": 0.5}],
            "joint_prob": bad_jp,
        }, _=None)
    assert exc.value.status_code == 400


def test_combo_eval_d46_happy_path_passes_through_to_evaluate_combo(monkeypatch):
    """R42-3 D-46: 合法 legs + joint_prob + stake -> 透传到 evaluate_combo。

    锁定 evaluate_combo 返回值契约: 函数最后一行 `return combo_mod.evaluate_combo(...)`,
    mocked 返回可观察 (n_legs 是 evaluate_combo 的真实字段, 我们改 evaluate_combo
    返 {'n_legs': len(legs)} 验证 legs 透传)。
    """
    captured = {}

    def fake_evaluate(legs, joint_prob=None, stake=2.0):
        captured["legs"] = legs
        captured["joint_prob"] = joint_prob
        captured["stake"] = stake
        return {"n_legs": len(legs), "stake": stake, "joint_prob": joint_prob}

    monkeypatch.setattr(combo_mod.combo_mod, "evaluate_combo", fake_evaluate)

    legs = [{"side": "主胜", "odds": 1.8, "p_model": 0.55}]
    out = combo_eval(_request(),
                      payload={"legs": legs, "joint_prob": 0.4, "stake": 5.0},
                      _=None)
    assert out == {"n_legs": 1, "stake": 5.0, "joint_prob": 0.4}
    assert captured["legs"] == legs
    assert captured["joint_prob"] == 0.4
    assert captured["stake"] == 5.0
