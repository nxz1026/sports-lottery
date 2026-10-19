"""R41-4: web/services/jobs.py::read_job_log 3 cases direct + D-41 silent-fallback lock.

读 jobs/<jid>.log 最后 tail 行 helper 0 direct tests; R39-5 _job_view_detail
间接覆盖 log_tail 字段, 但 read_job_log 自身契约 (tail 截断 / 异常兜底 /
非法 UTF-8) 未锁。

helper 含三条契约 (D-41):
- 缺文件: logs 路径不存在 -> 返 "" (绝不抛穿, scheduler race 安全)
- tail 截断: 500 行 + tail=10 -> 返最后 10 行 (lines not bytes)
- 非法 UTF-8: b"\xff\xfe invalid bytes" -> errors="replace" 后 \ufffd 替代
  不抛 UnicodeDecodeError (与 R32 _failure_hint 同模式)

3 tests (MEDIUM):
- 缺文件 -> ""
- tail 截断生效 (count == 10)
- 非法 UTF-8 字节不抛 (用 b"\xff" 触发 errors=replace, 验证包含 \ufffd)
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
def jobs_mod(tmp_path, monkeypatch):
    """加载 web/services/jobs.py + 把 JOBS_DIR 重定向到 tmp 隔离真实目录。"""
    from web import config as web_config
    monkeypatch.setattr(web_config, "JOBS_DIR", tmp_path / "jobs")
    spec = importlib.util.spec_from_file_location(
        "jobs_read_job_log_test",
        REPO_ROOT / "web" / "services" / "jobs.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # 确保 LOADER cache 已被重新装载 (因 spec name 不同但 module body 可能复用)
    return mod


# ──────────────────────── D-41 三契约 ────────────────────────


def test_read_job_log_d41_missing_file_returns_empty_string(jobs_mod):
    """R41-4 D-41: 缺 log 文件 -> 返 "" (不抛, scheduler race 安全)。

    锁定 silent fallback: 任务刚 submit 但 log 文件未创建, schedule
    50ms 后调 read_job_log 不应抛 FileNotFoundError。
    """
    jid = "j_nonexistent"
    out = jobs_mod.read_job_log(jid)
    assert out == ""
    assert isinstance(out, str)


def test_read_job_log_d41_tail_truncates_lines(jobs_mod):
    """R41-4 D-41: tail 截断是 line-count (非 bytes), 500 行 + tail=10 -> 最后 10 行。

    锁定 `splitlines()[-tail:]` 语义, 不要被后人改成 char-truncate 漂移。
    """
    jid = "j_tail_test"
    log_path = jobs_mod._log_file(jid)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 写 500 行简单内容
    log_path.write_bytes("\n".join(f"line {i:03d}" for i in range(500)).encode("utf-8"))

    out = jobs_mod.read_job_log(jid, tail=10)
    lines = out.split("\n")
    assert len(lines) == 10
    # 最后一行是 line 499
    assert lines[-1] == "line 499"


def test_read_job_log_d41_invalid_utf8_uses_replace(jobs_mod):
    """R41-4 D-41: log 文件含非法 UTF-8 字节 -> errors=\"replace\" 替代, 不抛 UnicodeDecodeError。

    与 R32 _failure_hint 同模式: decode bytes with errors="replace" 输出 \ufffd,
    防止客户上传/第三方写入的非法字节让整 log 不可读。
    """
    jid = "j_bad_utf8"
    log_path = jobs_mod._log_file(jid)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # b"\xff\xfe invalid bytes" 是无效 UTF-8 序列
    log_path.write_bytes(b"\xff\xfe invalid bytes\n")

    # 必须不抛 UnicodeDecodeError
    out = jobs_mod.read_job_log(jid)

    # 含有 \ufffd (Unicode 替换字符)
    U_REPLACEMENT = "\ufffd"
    assert U_REPLACEMENT in out
    # 同时也保留可读部分
    assert "invalid bytes" in out
