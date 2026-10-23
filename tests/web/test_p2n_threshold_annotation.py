"""P2-N（2026-10-23）：荐单的**门槛口径标注** —— 页面不许把旧批次的荐单
当成现行门槛下的荐单展示。

⚠️ **背景（真缺口，不是假想）**：

推荐端阈值 2026-10-23 起是 **EV ≥ 12.9%**（用户批准；此前是 EV ≥ 5%）。
但生成端 ``scripts/decision/recommend.py::write_reports`` **只做 upsert**
（``ON CONFLICT ... DO UPDATE``）、**从不清理**同一比赛日早先批次留下的行
⇒ 库里会同时存在「旧批次行」与「现行批次行」
⇒ 页面若照旧把它们都当「荐单」展示，就与面板上写的
「推荐端阈值 EV ≥ 12.9%」**自相矛盾**（页面在说假话）。

⇒ 实测（2026-10-23 生产）：当日早先批次产出的 3 行里曾有 2 行 EV < 12.9%
  ⇒ 采集机复跑后这 3 行被新一轮 upsert 覆盖成达标值，**碰巧**没暴露出矛盾
  ⇒ 但机制仍在（下一轮若不再入选该场次，旧行就会留在表里）。

⇒ 修法（**只标注、不删历史行** —— 历史记录不可篡改）：
  ① **真源**（``core/betting_policy.py``）：补旧门槛常量 ``EV_PREV_MIN_EV``；
  ② **web 读层**：每条 pick 带 ``meets_threshold`` / ``threshold_label``；
     战绩带 ``n_below_threshold`` / ``threshold_note``（标明「混合口径」）；
  ③ **前端**：不达标的**逐条标注**，面板级 + 战绩口径说明都印出来；
  ④ **生成端**：跑完立刻自检当日残留行 ⇒ **显式告警**（静默失败比报错更危险）。

本文件是**静态层 + 运行时层 + 反向对照**的三层锁：
* 运行时层：``tools/dashboard_smoke.js`` 的两条新断言
  （``p2n_below_threshold_pick_is_labeled`` / ``p2n_threshold_notes_are_rendered``）；
* 反向对照：把每条修复**回退** ⇒ 冒烟**必须报 FAIL** ⇒ 证明断言不是空断言。

零真库、零真网络、零真锁：只读静态文件 + 假连接 + 起 node 子进程。
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import betting_policy as bp  # noqa: E402
from decision import recommend as rec  # noqa: E402

import web.services.recommend as wsr  # noqa: E402

DASHBOARD = REPO_ROOT / "static" / "dashboard.html"
SMOKE = REPO_ROOT / "tools" / "dashboard_smoke.js"
WEB_RECOMMEND = REPO_ROOT / "web" / "services" / "recommend.py"


# ══════════════════════════════════════════════════════════════════════
# 假连接（照抄 tests/web/test_degraded_reads.py 的既有范式）
# ══════════════════════════════════════════════════════════════════════


def _pick_row(report_id: int, ev, *, p_model=0.4, option_code="a"):
    """一行 ``analysis.ev_report``（列序与 ``_PICKS_SQL`` 一致）。"""
    return (report_id, "2026-10-23 13:49:17+00", 2042111 + report_id, option_code,
            3.1, p_model, ev, 0.12, [], "note", "2026-10-24", "epl")


class _FakeConn:
    """按 SQL 文本分派的假连接。"""

    def __init__(self, *, picks=(), record_row=(0, 0, 0.0, 0.0), by_day=(),
                 detail=(), n_below=0):
        self._picks = list(picks)
        self._record_row = record_row
        self._by_day = list(by_day)
        self._detail = list(detail)
        self._n_below = n_below
        self.calls: list[tuple[str, tuple]] = []
        self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(str(sql).split())
        self.calls.append((self._last, params if params is not None else ()))
        return self

    def fetchone(self):
        if "ev_report" in self._last:
            return (self._n_below,)
        return self._record_row

    def fetchall(self):
        if "ev_report" in self._last:
            return self._picks
        if "rec_settle" in self._last and "GROUP BY" in self._last:
            return self._by_day
        if "rec_settle" in self._last:
            return self._detail
        return []


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *a):
        return False


def _patch_conn(monkeypatch, conn):
    monkeypatch.setattr("store.pg.read_conn", lambda *a, **k: _Ctx(conn))


# ══════════════════════════════════════════════════════════════════════
# ① 真源：现行门槛 + 旧门槛（历史口径）
# ══════════════════════════════════════════════════════════════════════


def test_p2n_true_source_keeps_current_and_previous_thresholds():
    """真源必须同时记着「现行门槛」与「旧门槛（仅用于标注）」。"""
    assert bp.MIN_EV == pytest.approx(0.129, abs=1e-9)
    assert bp.EV_PREV_MIN_EV == pytest.approx(0.05, abs=1e-9)
    assert bp.EV_THRESHOLD_TEXT == "EV ≥ 12.9%"
    assert bp.EV_PREV_THRESHOLD_TEXT == "EV ≥ 5%"
    assert bp.EV_POLICY_EFFECTIVE_DATE == bp.EV_APPROVAL_DATE == "2026-10-23"


def test_p2n_previous_threshold_is_not_used_for_filtering():
    """旧门槛**只用于标注** ⇒ 不许出现在任何筛选路径上。

    ⚠️ 现行门槛下生成端不可能产出 EV 低于门槛的行 ⇒ 库里不达标的行
    **必然**是旧批次残留 ⇒ 这条纪律是「标注即可」的前提。
    """
    from decision import recommend as r

    src = Path(r.__file__).read_text(encoding="utf-8")
    assert "EV_PREV_MIN_EV" not in src, \
        "旧门槛不许出现在生成端（会被误当成筛选门槛）"
    assert "EV_PREV_THRESHOLD_TEXT" in src, \
        "生成端的告警日志里要说明「当时的门槛更低」"


# ══════════════════════════════════════════════════════════════════════
# ② web 读层：逐条标注
# ══════════════════════════════════════════════════════════════════════


def test_p2n_below_threshold_pick_is_labeled(monkeypatch):
    """EV 低于现行门槛 ⇒ 必须打「不满足现行门槛」的标注。"""
    conn = _FakeConn(picks=[_pick_row(1, 0.09)])
    _patch_conn(monkeypatch, conn)
    picks = wsr.today_picks()
    assert len(picks) == 1
    p = picks[0]
    assert p["meets_threshold"] is False
    assert p["threshold_label"], "不达标的荐单必须有用户可见标注"
    assert "不满足现行门槛" in p["threshold_label"]
    # 标注里的门槛数字必须来自真源（不许手写）
    assert bp.EV_THRESHOLD_TEXT in p["threshold_label"]


def test_p2n_meeting_threshold_pick_has_no_label(monkeypatch):
    """达标的荐单**不许**带标注（否则标注失去意义）。"""
    conn = _FakeConn(picks=[_pick_row(2, 0.30)])
    _patch_conn(monkeypatch, conn)
    p = wsr.today_picks()[0]
    assert p["meets_threshold"] is True
    assert p["threshold_label"] == ""


def test_p2n_missing_ev_is_not_treated_as_meeting(monkeypatch):
    """EV 缺失 ⇒ **无法判定**（不许当成达标，那是在编数）。"""
    conn = _FakeConn(picks=[_pick_row(3, None)])
    _patch_conn(monkeypatch, conn)
    p = wsr.today_picks()[0]
    assert p["meets_threshold"] is None
    assert p["threshold_label"] == ""


def test_p2n_threshold_label_mentions_the_cause_not_a_number():
    """标注要说清「来自更早批次」，但**不写旧门槛的数字**。

    ⚠️ 用户可见文案里摆两个 EV 数字（旧 5% / 现行 12.9%）极容易被读成
    「现行门槛 = 那个数」⇒ 旧门槛只留在代码/文档层。
    """
    label = wsr._below_threshold_label()
    assert "更早的荐单批次" in label
    assert bp.EV_PREV_THRESHOLD_TEXT not in label, \
        "用户可见标注里不许出现旧门槛的数字（会被读成现行门槛）"
    assert bp.EV_THRESHOLD_TEXT in label


# ══════════════════════════════════════════════════════════════════════
# ③ 战绩：混合口径必须写出来
# ══════════════════════════════════════════════════════════════════════


def test_p2n_record_exposes_below_threshold_count_and_note(monkeypatch):
    """战绩必须给出「有多少条不满足现行门槛」+ 一句混合口径说明。"""
    conn = _FakeConn(record_row=(13, 2, 26.0, 20.0), n_below=2)
    _patch_conn(monkeypatch, conn)
    out = wsr.record()
    assert out["degraded"] is False
    assert out["n_below_threshold"] == 2
    assert "混合口径" in out["threshold_note"]
    assert bp.EV_THRESHOLD_TEXT in out["threshold_note"]


def test_p2n_record_note_when_everything_meets_threshold(monkeypatch):
    """全部达标 ⇒ 也要说明「全部满足现行门槛」（不许什么都不说）。"""
    conn = _FakeConn(record_row=(5, 3, 10.0, 12.0), n_below=0)
    _patch_conn(monkeypatch, conn)
    out = wsr.record()
    assert out["n_below_threshold"] == 0
    assert "全部满足现行门槛" in out["threshold_note"]


def test_p2n_record_degraded_does_not_invent_a_count(monkeypatch):
    """读失败 ⇒ 不达标条数**未知**（不许报 0，那是在编数）。"""
    monkeypatch.setattr("store.pg.read_conn",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    out = wsr.record()
    assert out["degraded"] is True
    assert out["n_below_threshold"] is None


# ══════════════════════════════════════════════════════════════════════
# ④ snapshot：面板级字段
# ══════════════════════════════════════════════════════════════════════


def test_p2n_snapshot_exposes_threshold_fields(monkeypatch):
    """snapshot 必须给出面板级的门槛口径（前端不许自己写死阈值）。"""
    conn = _FakeConn(picks=[_pick_row(1, 0.09), _pick_row(2, 0.30)])
    _patch_conn(monkeypatch, conn)
    out = wsr.snapshot()
    assert out["ev_threshold_text"] == bp.EV_THRESHOLD_TEXT
    assert out["picks_n_below_threshold"] == 1
    assert "不满足现行门槛" in out["picks_threshold_note"]


def test_p2n_snapshot_threshold_note_empty_when_all_meet(monkeypatch):
    """全部达标 ⇒ 面板级说明为空串（前端据此不渲染警告行）。"""
    conn = _FakeConn(picks=[_pick_row(2, 0.30)])
    _patch_conn(monkeypatch, conn)
    out = wsr.snapshot()
    assert out["picks_n_below_threshold"] == 0
    assert out["picks_threshold_note"] == ""


# ══════════════════════════════════════════════════════════════════════
# ⑤ 生成端：当日残留行自检（显式告警，不静默）
# ══════════════════════════════════════════════════════════════════════


def test_p2n_generation_side_counts_residual_rows():
    """自检必须按 **BJT 日 + EV < 现行门槛** 数，且参数带上真源阈值。"""
    captured: dict = {}

    class _C:
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(str(sql).split())
            captured["params"] = params
            return self

        def fetchone(self):
            return (3,)

    out = rec.count_below_threshold(_C(), "2026-10-23")
    assert out == 3
    assert "(made_at AT TIME ZONE 'Asia/Shanghai')::date" in captured["sql"]
    assert "ev < %s" in captured["sql"]
    assert captured["params"] == ("2026-10-23", bp.MIN_EV)


def test_p2n_generation_side_warns_about_residual_rows():
    """跑批后必须**显式告警**（静默失败比报错更危险）。"""
    src = Path(rec.__file__).read_text(encoding="utf-8")
    assert "count_below_threshold(conn)" in src, "主流程必须调用当日残留行自检"
    assert "logger.warning" in src, "有残留行时必须显式告警"


# ══════════════════════════════════════════════════════════════════════
# ⑥ 前端：必须真的把标注印出来
# ══════════════════════════════════════════════════════════════════════


def test_p2n_frontend_renders_per_pick_label():
    """前端必须按后端给的标志渲染逐条标注（不许自己算阈值）。"""
    src = DASHBOARD.read_text(encoding="utf-8")
    assert "p.meets_threshold===false" in src, \
        "前端必须按后端给的 meets_threshold 标志标注（自己算阈值 = 会漂移）"
    assert "threshold_label" in src, "前端必须渲染后端给的 threshold_label"
    assert "picks_threshold_note" in src, "前端必须渲染面板级门槛说明"
    assert "rec.threshold_note" in src, "前端必须渲染战绩的门槛口径说明"


# ══════════════════════════════════════════════════════════════════════
# ⑦ 运行时层 + 反向对照
# ══════════════════════════════════════════════════════════════════════


def _run_smoke(html_path: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [shutil.which("node"), str(SMOKE)]
    if html_path is not None:
        cmd += ["--html", str(html_path)]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="当前环境未安装 node ⇒ 本项未检查（非失败）")
def test_p2n_runtime_smoke_assertions_pass():
    """运行时：两条新断言必须都过（页面级 bug 必须有可自动跑的验证）。"""
    r = _run_smoke()
    out = r.stdout + r.stderr
    for key in ("p2n_below_threshold_pick_is_labeled",
                "p2n_threshold_notes_are_rendered"):
        assert f"OK  : [{key}]" in out, f"{key} 没被报告为 OK：\n{out}"
        assert f"FAIL: [{key}]" not in out, f"{key} 报了 FAIL：\n{out}"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="当前环境未安装 node ⇒ 本项未检查（非失败）")
def test_p2n_reverse_control_frontend_without_label_fails(tmp_path):
    """回退前端标注（不渲染 threshold_label）⇒ 冒烟必须 FAIL。"""
    src = DASHBOARD.read_text(encoding="utf-8")
    patched = src.replace(
        'const bad=p.meets_threshold===false;',
        'const bad=false;')
    assert patched != src, "反向补丁没命中（锚点变了？）"
    p = tmp_path / "dashboard.html"
    p.write_text(patched, encoding="utf-8")
    r = _run_smoke(p)
    out = r.stdout + r.stderr
    assert r.returncode != 0, "回退前端标注后冒烟仍然通过 ⇒ 断言是空断言"
    assert "FAIL: [p2n_below_threshold_pick_is_labeled]" in out, out[-2000:]


def test_p2n_reverse_control_web_without_meets_threshold_fails(tmp_path, monkeypatch):
    """回退 web 读层标注（不再算 meets_threshold）⇒ 标注必须消失。

    ⇒ 证明「标注确实由 ``meets_threshold`` 驱动」，而不是后端碰巧给了个字符串。
    """
    src = WEB_RECOMMEND.read_text(encoding="utf-8")
    patched = src.replace('"meets_threshold": meets,', '"meets_threshold": True,')
    patched = patched.replace(
        '"threshold_label": "" if meets is not False else _below_threshold_label(),',
        '"threshold_label": "",')
    assert patched != src, "反向补丁没命中（锚点变了？）"
    mod_path = tmp_path / "recommend_patched.py"
    mod_path.write_text(patched, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("wsr_p2n_patched", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    conn = _FakeConn(picks=[_pick_row(1, 0.09)])
    _patch_conn(monkeypatch, conn)
    p = mod.today_picks()[0]
    assert p["meets_threshold"] is True
    assert p["threshold_label"] == "", "回退后不该再有标注"
