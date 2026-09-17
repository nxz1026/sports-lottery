"""M6 测试：AI 摘要云端化（ai-enrich job）+ LLM 输出中文化（WO-M6）。

测试安全网：子进程一律假 Popen（记录 cmd，绝不真实执行引擎/jobs 目录不落盘真实
数据）；中文指令行断言直接读 ai 包源文件（无 LLM 调用）。
"""
from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

AI_ENRICH_SCRIPT = "web.enrich"


def _fake_sp(proc_cls) -> types.SimpleNamespace:
    """假 subprocess 模块：Popen 记录、STDOUT 常量、TimeoutExpired 用真类。"""
    import subprocess as _real_subprocess
    return types.SimpleNamespace(
        Popen=proc_cls, STDOUT=0, TimeoutExpired=_real_subprocess.TimeoutExpired,
    )


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """临时 env + 临时 jobs/quota 数据目录 + 应用实例（不 patch _build_cmd）。"""
    import importlib
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")
    monkeypatch.setenv("PREDICT_DAILY_LIMIT", "80")
    monkeypatch.setenv("AUTO_REFRESH_DAILY", "1")

    import web.config as config
    import web.session_store as session_store
    config = importlib.reload(config)
    session_store = importlib.reload(session_store)
    import web.auth as auth_mod
    importlib.reload(auth_mod)

    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "QUOTA_FILE", tmp_path / "quota.json")
    monkeypatch.setattr(config, "JOBS_LOCK_FILE", tmp_path / "jobs.lock")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")

    import web.services.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "_build_env", lambda: {})

    from web.api import create_app
    return create_app()


@pytest.fixture()
def client(app):
    return TestClient(app)


def _login(client):
    res = client.post("/api/v1/login", json={
        "username": "unit-test-user", "password": "unit-test-pass"})
    assert res.status_code == 200, res.text


class _OkProc:
    """正常完成的假子进程：wait 返回 0。"""

    def __init__(self, cmd, **kw):
        self._killed = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._killed = True

    def poll(self):
        return 0 if not self._killed else None


class _BlockProc:
    """阻塞门控假子进程：wait 挂起直到 gate 释放（制造 running 窗口）。"""

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.gate = threading.Event()

    def wait(self, timeout=None):
        self.gate.wait(5)
        return 0

    def kill(self):
        self.gate.set()

    def poll(self):
        return None


# --- 验收：401 未鉴权拒绝 ---------------------------------------------------

def test_ai_enrich_require_auth(client):
    assert client.post("/api/v1/jobs/ai-enrich").status_code == 401


# --- 验收：202 + job.script=ai_enrich（执行 web.enrich）、status=queued ------

def test_ai_enrich_202_queued_with_script(client, monkeypatch):
    import web.services.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "subprocess", _fake_sp(_OkProc))
    _login(client)
    r = client.post("/api/v1/jobs/ai-enrich")
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    assert job["script"] == "ai_enrich"
    assert job["status"] == "queued"
    assert job["trigger"] == "manual"
    # 持久化后 script 字段仍可读；轮询等待后台线程池写终态
    deadline = time.time() + 10
    got = None
    while time.time() < deadline:
        got = client.get(f"/api/v1/jobs/{job['id']}").json()["job"]
        if got["status"] in ("done", "failed", "timeout"):
            break
        time.sleep(0.1)
    assert got["script"] == "ai_enrich"
    assert got["status"] == "done"


def test_ai_enrich_spawns_correct_script(client, monkeypatch):
    """子进程 argv 为 `python -m web.enrich`（web 原生执行体，非 GHA 脚本）。"""
    import web.services.jobs as jobs_mod
    recorded = []
    gate = threading.Event()

    class GateProc(_BlockProc):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            recorded.append(cmd)

    monkeypatch.setattr(jobs_mod, "subprocess", _fake_sp(GateProc))
    _login(client)
    try:
        r = client.post("/api/v1/jobs/ai-enrich")
        assert r.status_code == 202
        # 轮询终态确保 run_job 消费了记录
        jid = r.json()["job"]["id"]
        for _ in range(200):
            body = client.get(f"/api/v1/jobs/{jid}").json()["job"]
            if body["status"] in ("done", "failed", "timeout"):
                break
            time.sleep(0.05)
        assert len(recorded) == 1
        cmd = recorded[0]
        assert "-m" in cmd
        assert cmd[-1] == AI_ENRICH_SCRIPT
        assert "ai_enrich_gha.py" not in cmd
        assert "predict.py" not in cmd
    finally:
        gate.set()


# --- 验收：409 已有 job 运行时并发拒绝 --------------------------------------

def test_ai_enrich_concurrent_409(client, monkeypatch):
    import time
    import web.services.jobs as jobs_mod
    recorded = []
    gate = threading.Event()

    class GateProc(_BlockProc):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            recorded.append(cmd)

    monkeypatch.setattr(jobs_mod, "subprocess", _fake_sp(GateProc))
    _login(client)
    try:
        r1 = client.post("/api/v1/jobs/ai-enrich")
        assert r1.status_code == 202
        # 等待第一个子进程真正起（run_job 在共享线程池排队，避免竞态）
        for _ in range(200):
            if recorded:
                break
            time.sleep(0.05)
        assert len(recorded) == 1
        r2 = client.post("/api/v1/jobs/ai-enrich")
        assert r2.status_code == 409
        body = r2.json()
        assert body["code"] == "already_running"
        assert body["job"]["script"] == "ai_enrich"
        assert body["job"]["status"] in ("queued", "running")
        assert len(recorded) == 1
    finally:
        gate.set()


# --- 验收：配额计数共享消耗（predict 与 ai_enrich 同一计数器） --------------

def test_quota_shared_between_predict_and_ai_enrich(client, monkeypatch):
    import web.config as config
    monkeypatch.setattr(config, "DAILY_TRIGGER_LIMIT", 1)
    _login(client)
    r1 = client.post("/api/v1/jobs/ai-enrich")
    assert r1.status_code == 202
    r2 = client.post("/api/v1/jobs/predict", json={})
    assert r2.status_code == 429
    assert r2.json()["code"] == "quota_exhausted"
    usage = client.get("/api/v1/sources/status").json()["jobs"]["quota"]
    assert usage["used"] == 1


# --- 验收：中文指令行存在于 _build_prompt 输出（读 ai 包断言） ---------------

def test_build_prompt_has_chinese_directive():
    lines = (REPO_ROOT / "ai" / "batch_pipeline.py").read_text(encoding="utf-8").splitlines()
    def line_no(sub: str) -> int:
        for i, ln in enumerate(lines):
            if sub in ln:
                return i
        raise AssertionError(f"未找到 {sub!r}")
    ln_return = line_no("Return:")
    ln_chinese = line_no("简体中文")
    ln_rubric = line_no("{scoring_rubric}")
    assert "JSON 键名保持英文" in lines[ln_chinese]
    # 指令行位于 Return 之后、scoring_rubric 占位之前（f-string 字面顺序）
    assert ln_return < ln_chinese < ln_rubric


def test_prompt_build_output_contains_directive(monkeypatch):
    """直接执行 _build_prompt 断言输出含「简体中文」指令（注入假 LLM 模块，零调用）。"""
    import types
    fake_llm = types.ModuleType("ai.llm_client")
    fake_llm.generate = lambda *a, **k: {"analyses": []}
    monkeypatch.setitem(sys.modules, "ai.llm_client", fake_llm)
    from ai.batch_pipeline import _build_prompt
    prompt = _build_prompt(
        [{"match": "A vs B", "home": "A", "away": "B", "_skip": 1}],
        context="", preference_prompt="", config={},
    )
    assert "简体中文" in prompt
    assert "Return:" in prompt


# --- M6R：web 原生执行体离线用例（严禁 live LLM/网络） ---------------------

def test_enrich_collect_items_maps_fields(monkeypatch):
    """collect_items：两联赛 doc → 每联赛 [:40] 全量、字段映射正确。"""
    import web.enrich as enrich_mod
    docs = {
        "EPL": {"data": {"predictions": [
            {"match": f"EPL match {i}", "stars": f"{i}-star",
             "confidence_score": 0.6 + i / 10,
             "direction": "曼城 胜" if i % 2 == 0 else "利物浦 胜"}
            for i in range(7)  # 7 条 → 全量窗口 [:40]
        ]}},
        "LALIGA": {"data": {"predictions": [
            {"match": "LALIGA match 0", "stars": "1-star", "confidence_score": 0.61,
             "direction": "皇马 胜"}
        ]}},
    }
    monkeypatch.setattr(enrich_mod.store, "latest_by_league", lambda leagues=None: docs)
    items = enrich_mod.collect_items()
    assert len(items) == 8  # 7 + 1（M8t2b: [:5]→[:40] 全量窗口，两联赛共 8 条）
    epl = [it for it in items if it["league"] == "EPL"]
    assert len(epl) == 7  # M8t2b: [:5]→[:40] 全量窗口
    assert epl[0]["name"] == "EPL match 0"
    assert epl[0]["date_found"] == ""
    assert epl[0]["stars"] == "0-star"
    assert epl[0]["confidence"] == 0.6
    assert epl[0]["direction"] == "曼城 胜"
    assert epl[1]["direction"] == "利物浦 胜"
    laliga = [it for it in items if it["league"] == "LALIGA"][0]
    assert laliga["direction"] == "皇马 胜"  # 方向原样透传，不再归约


def test_enrich_main_writes_back_and_returns_zero(monkeypatch, capsys):
    """main：假 analyse_batch（带中文 summary）+ 假 save_ai_scores → 返回 0、写回被调用。"""
    import web.enrich as enrich_mod
    saved = {}

    def fake_collect():
        return [{"name": "A vs B", "league": "EPL", "date_found": "",
                 "direction": "home", "stars": 1, "confidence": "c"}]

    def fake_analyse(items, **kw):
        assert items == fake_collect()
        assert kw["context"] == ""
        assert kw["preference_prompt"] == ""
        assert kw["config"]["ai"]["model"]
        return [{"name": "A vs B", "ai_score": 0.9, "ai_summary": "主队胜算高",
                 "ai_notes": "", "source": "test"}]

    def fake_save(enriched, league_key=""):
        saved["enriched"] = enriched
        saved["league_key"] = league_key

    monkeypatch.setattr(enrich_mod, "collect_items", fake_collect)
    monkeypatch.setattr("ai.batch_pipeline.analyse_batch", fake_analyse, raising=False)
    monkeypatch.setattr("ai.feedback_loop.save_ai_scores", fake_save, raising=False)
    assert enrich_mod.main() == 0
    assert saved["league_key"] == ""
    assert len(saved["enriched"]) == 1
    assert "主队胜算高" in saved["enriched"][0]["ai_summary"]
    out = capsys.readouterr().out
    assert "processed 1 items, wrote back 1" in out


# --- M6S：按名配对（修 analyses 位置错配，线上实证 2026-09-11） -------------

def _load_batch_pipeline(monkeypatch):
    """注入假 ai.llm_client 后加载 ai.batch_pipeline（零 requests/LLM 依赖）。"""
    import types
    fake_llm = types.ModuleType("ai.llm_client")
    fake_llm.generate = lambda *a, **k: {"analyses": []}
    monkeypatch.setitem(sys.modules, "ai.llm_client", fake_llm)
    monkeypatch.delitem(sys.modules, "ai.batch_pipeline", raising=False)
    import ai.batch_pipeline as bp
    return bp


def test_analyse_batch_pairs_by_name(monkeypatch):
    """LLM 返回乱序带 match 键的 analyses → 按名配对而非按位置。"""
    bp = _load_batch_pipeline(monkeypatch)
    monkeypatch.setattr(bp, "generate", lambda prompt, **k: {"analyses": [
        {"match": "B vs C", "score": 90, "summary": "乙队胜", "notes": ""},
        {"match": "A vs B", "score": 70, "summary": "甲队胜", "notes": ""},
    ]})
    out = bp.analyse_batch(
        [{"name": "A vs B", "league": "EPL"}, {"name": "B vs C", "league": "EPL"}],
        config={},
    )
    assert [o["name"] for o in out] == ["A vs B", "B vs C"]
    assert [o["ai_summary"] for o in out] == ["甲队胜", "乙队胜"]
    assert [o["ai_score"] for o in out] == [70, 90]


def test_analyse_batch_missing_analysis_keeps_item_plain(monkeypatch):
    """analyses 缺一条 → 缺失项原样保留、不串位（无 ai_ 字段）。"""
    bp = _load_batch_pipeline(monkeypatch)
    monkeypatch.setattr(bp, "generate", lambda prompt, **k: {"analyses": [
        {"match": "A vs B", "score": 80, "summary": "甲队胜", "notes": ""},
    ]})
    out = bp.analyse_batch(
        [{"name": "A vs B", "league": "EPL"}, {"name": "B vs C", "league": "EPL"}],
        config={},
    )
    assert out[0]["ai_summary"] == "甲队胜"
    assert out[1]["name"] == "B vs C"
    assert "ai_score" not in out[1]
    assert "ai_summary" not in out[1]
    assert "ai_notes" not in out[1]


def test_analyse_batch_falls_back_to_position_without_match(monkeypatch):
    """所有 analyses 无 match 键且数量相等 → 兼容回退按位置配对（旧行为）。"""
    bp = _load_batch_pipeline(monkeypatch)
    monkeypatch.setattr(bp, "generate", lambda prompt, **k: {"analyses": [
        {"score": 60, "summary": "丙队胜", "notes": ""},
        {"score": 65, "summary": "丁队胜", "notes": ""},
    ]})
    out = bp.analyse_batch(
        [{"name": "C vs D", "league": "EPL"}, {"name": "D vs E", "league": "EPL"}],
        config={},
    )
    assert [o["ai_summary"] for o in out] == ["丙队胜", "丁队胜"]


def test_analyse_batch_skips_non_dict_position_fallback(monkeypatch):
    """位置回退含字符串和空值时，仅好分析写入 AI 字段。"""
    bp = _load_batch_pipeline(monkeypatch)
    monkeypatch.setattr(bp, "generate", lambda prompt, **k: {"analyses": [
        {"score": 88, "summary": "好队胜", "notes": ""},
        "坏分析",
        None,
    ]})
    items = [
        {"name": "A vs B", "league": "EPL"},
        {"name": "B vs C", "league": "EPL"},
        {"name": "C vs D", "league": "EPL"},
        {"name": "D vs E", "league": "EPL"},
    ]

    out = bp.analyse_batch(items, config={})

    assert out[0]["ai_score"] == 88
    assert "ai_score" not in out[1]
    assert "ai_score" not in out[2]

# --- 验收修复（2026-09-17）：失败不得伪装成成功、league 不得丢失 ---------------

def _load_feedback_loop(tmp_path, monkeypatch):
    """把 ai_scores.json 指到 tmp，返回 feedback_loop 模块。"""
    import importlib
    import ai.feedback_loop as fl
    importlib.reload(fl)
    monkeypatch.setattr(fl, "AI_SCORES_FILE", tmp_path / "ai_scores.json")
    return fl


def test_save_ai_scores_skips_unscored_items(tmp_path, monkeypatch):
    """LLM 失败时 analyse_batch 原样返回未评分条目 → 绝不能落盘成 ai_score=50。

    旧行为：item.get("ai_score", 50) 兜底成 50，把"完全没分析"伪造成"中性 50 分"；
    而 adjust_prediction 的 0.7+0.3*50/100=0.85 会静默削掉 15% 信心。
    """
    fl = _load_feedback_loop(tmp_path, monkeypatch)
    fl.save_ai_scores([
        {"name": "A vs B", "league": "epl"},                                   # 未评分
        {"name": "C vs D", "league": "epl", "ai_score": 80, "ai_summary": "主队占优"},
    ], league_key="")

    import json
    data = json.loads(fl.AI_SCORES_FILE.read_text(encoding="utf-8"))
    assert "A vs B" not in data, "未评分条目不得落盘（否则等于编造 50 分）"
    assert data["C vs D"]["ai_score"] == 80


def test_save_ai_scores_keeps_item_league(tmp_path, monkeypatch):
    """league_key 为空时必须回退到条目自带 league，否则 ai_daily 命中数结构性永久为 0。

    旧行为：league_key or item.get("source","") → "" or "" = ""，
    而 ai_daily 要求 scores[match]["league"] == prediction["league"]（预测侧 league 恒非空）。
    """
    fl = _load_feedback_loop(tmp_path, monkeypatch)
    fl.save_ai_scores([
        {"name": "布伦特福德 vs 切尔西", "league": "epl", "ai_score": 70, "ai_summary": "接近"},
    ], league_key="")

    import json
    data = json.loads(fl.AI_SCORES_FILE.read_text(encoding="utf-8"))
    assert data["布伦特福德 vs 切尔西"]["league"] == "epl"


def test_enrich_main_returns_nonzero_when_nothing_scored(monkeypatch, capsys):
    """全部条目都没被评分 → main() 必须返回非 0，不能报 done。

    旧行为：恒 return 0 → 任务显示 done、看板"AI 状态：可用"，用户完全无法察觉 LLM 全挂。
    """
    import web.enrich as enrich_mod

    monkeypatch.setattr(enrich_mod, "collect_items",
                        lambda: [{"name": "A vs B", "league": "epl", "date_found": "",
                                  "direction": "home", "stars": 1, "confidence": "c"}])
    monkeypatch.setattr("ai.batch_pipeline.analyse_batch",
                        lambda items, **kw: [dict(items[0])], raising=False)   # 未评分
    monkeypatch.setattr("ai.feedback_loop.save_ai_scores",
                        lambda enriched, league_key="": None, raising=False)

    assert enrich_mod.main() == 1
    out = capsys.readouterr().out
    assert "processed 1 items, wrote back 0" in out
