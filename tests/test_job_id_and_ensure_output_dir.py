"""R41-1: 2 EASY 合并 — D-38 (ai_analyze._ensure_output_dir) + D-39 (jobs.job_id).

两个 helper 都极简 (一行 + 1-2 行), 0 直测但被频繁使用:
- _ensure_output_dir: ai_analyze CLI 入口的目录自举, 反复调用要幂等
- job_id: 12-char hex 任务 ID, 用作 jobs/<id>.{json,log} 文件名主键

D-38 (ensure_output_dir):
- 幂等: 二次调用不抛 FileExistsError (exist_ok=True)
- 路径恒等: 返回值 == OUTPUT_DIR (调用方无需自行 concat)

D-39 (job_id):
- 长度/字符集: 12 chars hex (regex ^[0-9a-f]{12}$)
- 唯一性: 两次连续调用不等
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.services import ai_analyze as ai_mod
from web.services import jobs as jobs_mod

# ──────────────────────── D-38 _ensure_output_dir ────────────────────────


def test_ensure_output_dir_d38_idempotent(tmp_path, monkeypatch):
    """R41-1 D-38: 二次调用幂等 (exist_ok=True 不抛 FileExistsError)。"""
    target_dir = tmp_path / "ai_analysis"
    monkeypatch.setattr(ai_mod, "OUTPUT_DIR", target_dir)
    # 第一次: 创目录
    out1 = ai_mod._ensure_output_dir()
    assert target_dir.is_dir()
    # 第二次: 同 process 反复调用应不抛
    out2 = ai_mod._ensure_output_dir()
    assert out1 == out2 == target_dir
    # 目录仍存在 (没被二次 mkdir 改状态)
    assert target_dir.is_dir()


def test_ensure_output_dir_d38_returns_output_dir_constant(tmp_path, monkeypatch):
    """R41-1 D-38: 返回值恒等于 OUTPUT_DIR 模块常量 (调用方无需再拼)。"""
    target_dir = tmp_path / "nested" / "ai_analysis"
    monkeypatch.setattr(ai_mod, "OUTPUT_DIR", target_dir)
    out = ai_mod._ensure_output_dir()
    # 严格恒等 (返回 OUTPUT_DIR 引用)
    assert out == ai_mod.OUTPUT_DIR
    assert out == target_dir


# ──────────────────────── D-39 job_id ────────────────────────


def test_job_id_d39_12_char_hex():
    """R41-1 D-39: job_id 严格 12-char hex (regex ^[0-9a-f]{12}$)。"""
    jid = jobs_mod.job_id()
    assert len(jid) == 12
    assert re.match(r"^[0-9a-f]{12}$", jid), f"got {jid!r}"


def test_job_id_d39_uniqueness_two_calls_differ():
    """R41-1 D-39: 连续两次调用不等 (12 字符熵足够碰撞规避)。

    uuid4().hex[:12] 在同进程产生 2 个独立 ID, 期望不等;
    即使有重复 (e.g., 1/16^12 概率) 此测 fail, 提示维护者检查 PRNG。
    """
    a, b = jobs_mod.job_id(), jobs_mod.job_id()
    assert a != b
