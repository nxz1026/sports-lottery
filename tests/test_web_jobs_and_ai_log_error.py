"""Round 10 C.1 + C.2 守门 —— web jobs spin_state + ai_analyze LLM。

C.1 + C.2 涉及 web.services.jobs 与 web.services.ai_analyze，后者各有懒加载
external 依赖（subprocess.Popen / ai.llm_client.generate），完整集成测试链
太脆易碎。本文件用**静态校验**：确认生产代码在正确的 except 分支上包了
``observability.log_error`` 调用 + context 字段名与设计草案一致。

完整 e2e 验证留给后续 prod 端真实流量；CI 这层只锁"代码变动有合理 log_error
调用位"，可在 review 时一眼看出漏布防的位置。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_source(rel_path: str) -> str:
    p = REPO_ROOT / rel_path
    return p.read_text(encoding="utf-8")


# ────────────────────── C.1: jobs.run_job ──────────────────────────


class TestJobsSpinStateLogErrorWiring:
    """C.1: ``run_job`` 三处 ``_spin_state`` 调用各自有 try/except + log_error。"""

    def test_spawn_failed_has_spawn_failed_context(self):
        """spawn OSError 分支包了 ``jobs.spin_state_after_spawn_failed`` context。"""
        src = _read_source("web/services/jobs.py")
        # 找 OSError 紧跟 except 后到 _spin_state + try/except 块
        assert "jobs.spin_state_after_spawn_failed" in src, (
            "C.1 设计要求：spawn 失败后 _spin_state 自身失败也走 log_error "
            "context=jobs.spin_state_after_spawn_failed"
        )
        # 必须有 try/except 守护 _spin_state 调用
        # 找 'spin_state_after_spawn_failed' 上下文
        ctx_idx = src.index("jobs.spin_state_after_spawn_failed")
        snippet = src[max(0, ctx_idx - 400):ctx_idx + 200]
        assert "try:" in snippet and "except" in snippet, (
            "log_error 必须在 try/except 内，外部 except 又要 catch observability 自身失败"
        )
        assert "observability.log_error" in snippet
        assert "jid" in snippet
        assert "original" in snippet  # 截 200 字符的原 spawn OSError

    def test_timeout_has_timeout_context(self):
        """timeout 分支包了 ``jobs.spin_state_after_timeout``。"""
        src = _read_source("web/services/jobs.py")
        assert "jobs.spin_state_after_timeout" in src
        ctx_idx = src.index("jobs.spin_state_after_timeout")
        snippet = src[max(0, ctx_idx - 300):ctx_idx + 100]
        assert "try:" in snippet and "except" in snippet
        assert "observability.log_error" in snippet
        assert "jid" in snippet

    def test_nonzero_exit_has_nonzero_exit_context(self):
        """非零退出分支包了 ``jobs.spin_state_after_nonzero_exit``。"""
        src = _read_source("web/services/jobs.py")
        assert "jobs.spin_state_after_nonzero_exit" in src
        ctx_idx = src.index("jobs.spin_state_after_nonzero_exit")
        snippet = src[max(0, ctx_idx - 400):ctx_idx + 200]
        assert "try:" in snippet and "except" in snippet
        assert "observability.log_error" in snippet
        assert "exit_code" in snippet
        assert "jid" in snippet


# ────────────────────── C.2: ai_analyze ──────────────────────────


class TestAiAnalyzeLlmLogErrorWiring:
    """C.2: ai_analyze 三类 LLM + 主入口三类 catch 各自包 log_error。"""

    def test_per_match_llm_wired(self):
        """``_class_per_match`` 的 LLM 调用有 log_error 兜底。"""
        src = _read_source("web/services/ai_analyze.py")
        assert "ai_analyze.per_match_llm" in src
        idx = src.index("ai_analyze.per_match_llm")
        snippet = src[max(0, idx - 300):idx + 100]
        assert "_ai_obs.log_error" in snippet or "observability.log_error" in snippet
        assert "league" in snippet
        assert "match" in snippet

    def test_banker_llm_wired(self):
        """``_class_banker`` 的 LLM 调用有 log_error 兜底。"""
        src = _read_source("web/services/ai_analyze.py")
        assert "ai_analyze.banker_llm" in src
        idx = src.index("ai_analyze.banker_llm")
        snippet = src[max(0, idx - 200):idx + 200]
        assert "_ai_obs.log_error" in snippet or "observability.log_error" in snippet
        assert "candidates_count" in snippet

    def test_lottery_llm_wired(self):
        """``_class_lottery`` 的 LLM 调用有 log_error 兜底。"""
        src = _read_source("web/services/ai_analyze.py")
        assert "ai_analyze.lottery_llm" in src
        idx = src.index("ai_analyze.lottery_llm")
        snippet = src[max(0, idx - 300):idx + 200]
        assert "_ai_obs.log_error" in snippet or "observability.log_error" in snippet
        assert "game_num" in snippet

    def test_analyze_main_catches_all_three(self):
        """主入口 analyze() 三类 catch 都包 log_error。"""
        src = _read_source("web/services/ai_analyze.py")
        for ctx in ("ai_analyze.analyze.per_match",
                    "ai_analyze.analyze.banker",
                    "ai_analyze.analyze.lottery"):
            assert ctx in src, f"主入口必须有 {ctx} log_error 调用"

    def test_top_level_observability_import_at_analyze(self):
        """``analyze()`` 顶部 should import observability once（避免重复 3 次）。"""
        src = _read_source("web/services/ai_analyze.py")
        # analyze() 函数定义后到 return 之前应该有 _ai_obs_top 引用
        idx = src.index("def analyze(")
        snippet = src[idx:src.index("return payload", idx)]
        assert "_ai_obs_top" in snippet, "analyze() 顶部懒加载 observability 实例"

    def test_per_class_helper_log_error_wrapped_in_try(self):
        """每个 helper 里 log_error 都在 try/except 内（observability 自身失败不阻塞）。"""
        src = _read_source("web/services/ai_analyze.py")
        # 三处带 log_error 的 except 块：每个前面 80 字符内应出现 try:
        for ctx in ("ai_analyze.per_match_llm", "ai_analyze.banker_llm",
                    "ai_analyze.lottery_llm",
                    "ai_analyze.analyze.per_match",
                    "ai_analyze.analyze.banker",
                    "ai_analyze.analyze.lottery"):
            idx = src.index(ctx)
            snippet = src[max(0, idx - 150):idx + 50]
            assert "try:" in snippet or "try " in snippet, (
                f"{ctx} 的 log_error 应在 try/except 内（兜住 observability 自身失败）"
            )


# ────────────────────── 设计完整性 ──────────────────────────


class TestCwiringSanity:
    """设计完整性：observability 装挂在 web/api 已确保 + ai_analyze import 时装挂。"""

    def test_web_api_installs_observability_with_web(self):
        """web/api.py 模块级 setup(process_name="web") 已锁（Round 9 H3）。"""
        src = _read_source("web/api.py")
        assert "process_name=\"web\"" in src

    def test_ai_analyze_installs_observability_with_ai_suffix(self):
        """ai_analyze.py 模块级 setup(process_name="ai_analyze") 已锁（Round 10 E.3）。"""
        src = _read_source("web/services/ai_analyze.py")
        assert "process_name=\"ai_analyze\"" in src

    def test_no_legacy_logger_xxx_in_jobs_serving_path(self):
        """jobs.run_job 关键分支仍写 logger.* 而不是被 log_error 吞掉 — 两条并存。"""
        src = _read_source("web/services/jobs.py")
        # run_job 仍应保留 stderr logger 同步
        # 不强制检查具体行数，只是验证 log_error 包装和 logger.* 不冲突
        assert "logger.info(\"job" in src or "logger.info('job" in src
