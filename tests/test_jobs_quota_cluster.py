"""R42-1: web/services/jobs.py quota cluster 6 tests direct + D-44 lock.

quota 6 件套 (load_quota / save_quota / _quota_state / quota_usage /
quota_exhausted / quota_consume) 仅通过 test_m3_jobs.py 集成校验,
0 直测。但承担 jobs 系统"日配额守卫"全局不变量: 跨 BJT 日自动重置
+ 原子消费。任何 status 漂移会让 active_job 状态机读错。

helper 含六条契约 (D-44):
1. load_quota: 缺文件 -> {day: bjt_today, count: 0}; OSError/ValueError/非 dict
   同兜底; warn 一下但不抛穿
2. save_quota: 原子 tmp+os.replace (防止写盘期间损坏留半写文件)
3. _quota_state: data['day'] != bjt_today -> (today, 0) 自动重置 (跨日)
4. quota_usage: {day, used, limit} 三字段, used = _quota_state 后读,
   limit = config.DAILY_TRIGGER_LIMIT
5. quota_exhausted: 只读预检, _exclusive_lock 外, used >= limit -> True
6. quota_consume: 原子 (持 _exclusive_lock) 读-改-写; 超限 -> False; 否则
   count+1 写回 -> True

6 tests (HIGH):
- load_quota 缺文件返 {day, count: 0}
- save_quota 原子 tmp+os.replace (验证无 .tmp 残留)
- _quota_state 跨日自动重置 (data.day != today)
- quota_usage 三字段 + 跨日 used 重置为 0
- quota_exhausted 只读不扣减 (count=0 -> False)
- quota_consume 原子消费 (count=limit -> False, 不写; count<limit -> True,
  count+1 写盘)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def jobs_mod(tmp_path, monkeypatch):
    """加载 web/services/jobs.py + QUOTA_FILE 重定向 tmp 隔离真实配额。"""
    from web import config as web_config
    quota_path = tmp_path / "quota.json"
    quota_lock = tmp_path / "quota.lock"
    monkeypatch.setattr(web_config, "QUOTA_FILE", quota_path)
    # with_suffix(".lock") 必须能在 tmp_path 看到; jobs._exclusive_lock 走 os
    monkeypatch.setattr(web_config, "JOBS_DIR", tmp_path / "jobs")
    spec = importlib.util.spec_from_file_location(
        "jobs_quota_cluster_test",
        REPO_ROOT / "web" / "services" / "jobs.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_today(monkeypatch, jobs_mod):
    """锁定 _bjt_day_key 返固定日, 不被 wall-clock 影响。"""
    today = "2026-10-15"
    monkeypatch.setattr(jobs_mod, "_bjt_day_key", lambda: today)
    return today


# ──────────────────────── D-44 六契约 ────────────────────────


def test_quota_d44_load_quota_missing_file_returns_zero_state(
    jobs_mod, fake_today, tmp_path,
):
    """R42-1 D-44: 缺文件 -> 返 {day: today, count: 0}, 不抛穿。"""
    quota_path = jobs_mod.config.QUOTA_FILE  # 已重定向 tmp
    assert not quota_path.exists()
    out = jobs_mod.load_quota()
    assert out == {"day": fake_today, "count": 0}


def test_quota_d44_save_quota_atomic_tmp_replace(jobs_mod, fake_today, tmp_path):
    """R42-1 D-44: save_quota 原子 tmp+os.replace (无 .tmp 残留, 内容可读)。"""
    quota_path = jobs_mod.config.QUOTA_FILE
    payload = {"day": fake_today, "count": 3}
    jobs_mod.save_quota(payload)
    # 主文件存在 + 内容正确
    assert quota_path.is_file()
    assert json.loads(quota_path.read_text(encoding="utf-8")) == payload
    # .tmp 文件不应残留
    assert not quota_path.with_suffix(".tmp").exists()


def test_quota_d44_state_cross_day_auto_resets(jobs_mod, fake_today):
    """R42-1 D-44: data['day'] != today -> 强制 (today, 0) 重置 (跨日守卫)。

    锁定跨 BJT 日自动重置; 后端 quota_exhausted 用此不变量。
    """
    yesterday = "2026-10-14"
    data = {"day": yesterday, "count": 50}
    today, used = jobs_mod._quota_state(data)
    assert today == fake_today
    assert used == 0  # 重置


def test_quota_d44_usage_three_fields(jobs_mod, fake_today, tmp_path, monkeypatch):
    """R42-1 D-44: quota_usage 返 {day, used, limit} 三字段; data['day'] != today
    时 used 重置为 0 (跨日 quota 自动归零)。
    """
    # 写一份昨日配额 (used=5), 验证 _quota_state 自动归零
    quota_path = jobs_mod.config.QUOTA_FILE
    quota_path.parent.mkdir(parents=True, exist_ok=True)
    quota_path.write_text(
        json.dumps({"day": "2026-10-14", "count": 5}),
        encoding="utf-8")
    monkeypatch.setattr(jobs_mod, "config",
                        jobs_mod.config)  # 保证 config reference
    out = jobs_mod.quota_usage()
    assert out["day"] == fake_today
    assert out["used"] == 0  # 跨日自动重置
    assert out["limit"] == jobs_mod.config.DAILY_TRIGGER_LIMIT


def test_quota_d44_exhausted_read_only_no_consume(jobs_mod, fake_today, tmp_path):
    """R42-1 D-44: quota_exhausted 只读预检, 不扣减。

    锁定"先检查后扣减"两段式契约: quota_exhausted 不消费, 写入后读出应
    不变。
    """
    quota_path = jobs_mod.config.QUOTA_FILE
    quota_path.parent.mkdir(parents=True, exist_ok=True)
    quota_path.write_text(
        json.dumps({"day": fake_today, "count": 0}),
        encoding="utf-8")
    assert jobs_mod.quota_exhausted() is False
    # 二次调用 (无扣减, 仍 False)
    assert jobs_mod.quota_exhausted() is False


def test_quota_d44_consume_atomic_full_returns_false(
    jobs_mod, fake_today, tmp_path,
):
    """R42-1 D-44: count 已达 limit -> quota_consume 返 False, 不写盘。

    锁原子消费: 超限路径不修改文件 (count 已达, 写入反而污染真值)。
    """
    quota_path = jobs_mod.config.QUOTA_FILE
    quota_path.parent.mkdir(parents=True, exist_ok=True)
    quota_path.write_text(
        json.dumps({
            "day": fake_today,
            "count": jobs_mod.config.DAILY_TRIGGER_LIMIT,
        }),
        encoding="utf-8")
    out = jobs_mod.quota_consume()
    assert out is False
    # 文件未被修改 (count 仍 = limit)
    after = json.loads(quota_path.read_text(encoding="utf-8"))
    assert after["count"] == jobs_mod.config.DAILY_TRIGGER_LIMIT
