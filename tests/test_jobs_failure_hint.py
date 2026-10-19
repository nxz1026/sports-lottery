"""R32 #5: web/services/jobs.py::_failure_hint 5 测直测。

现状：失败任务诊断摘要 0 直测；仅在 jobs API/看板集成中间接跑。
D-9（jobs.py:413）：_failure_hint 用 `read_bytes().decode("utf-8", errors="replace")`
保护 Windows BOM / 非法字节（不是 text mode）。

本轮补 5 测：
- 日志文件不存在 → OSError → text="" → "exit {code}"（无 tail）
- 空日志文件 → splitlines() 空 → "exit {code}"
- 多行日志 → 取最后非空 stripped 行（不是首行）
- 行长度超 limit=300 → 截断
- 非法 UTF-8 字节 → errors="replace" 仍可解析
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def jobs_mod():
    """加载 jobs.py 为唯一 module name（web/services/jobs.py 顶层 import 链较重）。"""
    spec = importlib.util.spec_from_file_location(
        "failure_hint_test", REPO_ROOT / "web" / "services" / "jobs.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _failure_hint ────────────────────────


def test_failure_hint_log_missing_returns_plain_exit(jobs_mod, tmp_path):
    """R32 #5：日志文件不存在 → OSError 兜底 → text="" → "exit {code}"（无 tail）。"""
    log = tmp_path / "missing.log"
    assert not log.exists()
    out = jobs_mod._failure_hint(log, code=2)
    assert out == "exit 2"


def test_failure_hint_log_empty_returns_plain_exit(jobs_mod, tmp_path):
    """R32 #5：日志文件存在但为空 → splitlines() 空 → tail="" → "exit {code}"。"""
    log = tmp_path / "empty.log"
    log.write_bytes(b"")
    out = jobs_mod._failure_hint(log, code=1)
    assert out == "exit 1"


def test_failure_hint_multiline_picks_last_nonempty(jobs_mod, tmp_path):
    """R32 #5：多行日志 → 取最后非空 stripped 行（不是首行）。

    含末尾空行尾随：splitlines() 仍能正确处理；reversed + skip-stripped 找到
    "ConnectionError: rate limit" 这一行。
    """
    log = tmp_path / "multi.log"
    log.write_text(
        "INFO: starting\n"
        "ERROR: rate limit hit\n"
        "  ConnectionError: rate limit\n"
        "\n",  # 末尾空行
        encoding="utf-8",
    )
    out = jobs_mod._failure_hint(log, code=1)
    assert out == "exit 1: ConnectionError: rate limit"


def test_failure_hint_long_line_truncated_to_limit(jobs_mod, tmp_path):
    """R32 #5：单行长度超 limit → 截断到 limit 字符（默认 300）。"""
    log = tmp_path / "long.log"
    # 写 500 字符的单行
    log.write_text("A" * 500 + "\n", encoding="utf-8")
    out = jobs_mod._failure_hint(log, code=3)
    # 应为 "exit 3: " + 300 个 A
    assert out == "exit 3: " + "A" * 300


def test_failure_hint_invalid_utf8_decodes_with_replace(jobs_mod, tmp_path):
    """R32 #5：D-9 锁定 — 含非法 UTF-8 字节 → errors="replace" 仍可解析（不抛 UnicodeDecodeError）。

    0xFF 0xFE 不是合法 UTF-8 起始字节；不开启 errors="replace" 会抛 UnicodeDecodeError
    抛穿 jobs.py 主流程。验证 _failure_hint 静默 fallback 为 U+FFFD。
    """
    log = tmp_path / "bad_utf8.log"
    # 合法前缀 + 非法字节 + 合法后缀
    log.write_bytes(b"Error: \xff\xfe invalid bytes \xe2\x80\x94 em dash\n")
    out = jobs_mod._failure_hint(log, code=7)
    # 不抛异常 + 含解码后内容
    assert out.startswith("exit 7: ")
    assert "invalid bytes" in out
    assert "Error:" in out
