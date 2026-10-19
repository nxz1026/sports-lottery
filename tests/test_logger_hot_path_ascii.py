"""R43 regression test: hot-path logger/print 文案必须 ASCII。

Q5 修复 `4a6dc8b` 目标：cron / systemd journal 输出避免中文乱码（Windows cp1252/GBK）
导致告警脚本 grep 失败。但 R43 扫库发现 13 处遗漏：
- predict.py: 3 处
- bball/run.py: 3 处
- core/retrain_baseline.py: 7 处
- core/observability.py: 1 处

本测试用 AST 找 logger.{info,warning,error,debug} 和 print() 调用的字符串字面量，
断言所有字面量为 ASCII（ord(c) < 128）。未来任何人加非 ASCII 文案会被这测拦截。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

# R43 Q5 修复一致性收口：cron 跑的 hot-path 文件必须有 ASCII logger 文案
HOT_PATH_FILES = [
    SCRIPTS / "predict.py",
    SCRIPTS / "bball" / "run.py",
    SCRIPTS / "core" / "retrain_baseline.py",
    SCRIPTS / "core" / "observability.py",
]

LOG_METHODS = {"info", "warning", "error", "debug"}


def _extract_string_literals(filepath: Path) -> list[tuple[int, str, str]]:
    """AST 扫文件，返回 [(line_no, method_name, literal)] 列表，仅含 logger/print 调用的字符串字面量。"""
    src = filepath.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(filepath))
    out: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # 只看 logger.info(...) 这种 attribute call（print(...) 也是 ast.Name）
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
        elif isinstance(node.func, ast.Name):
            method = node.func.id
        else:
            continue
        if method not in LOG_METHODS and method != "print":
            continue
        # 看 args 中是否有字符串字面量
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append((node.lineno, method, arg.value))
        # 看 keyword args（log_event 的 message 参数等也包含）
        for kw in node.keywords:
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                out.append((node.lineno, method, kw.value.value))
    return out


@pytest.mark.parametrize("filepath", HOT_PATH_FILES, ids=lambda p: p.name)
def test_hot_path_logger_literals_are_ascii(filepath):
    """hot-path 文件所有 logger.{info,warning,error,debug} 与 print 文案必须 ASCII。

    实证：R43 扫库发现 13 处遗漏（predict.py 3 / bball/run.py 3 / retrain_baseline.py 7
    / observability.py 1），cron 跑到这些分支时会因 GBK/cp1252 写盘变乱码。本测试拦截回退。
    """
    literals = _extract_string_literals(filepath)
    violations = [(line, method, lit) for line, method, lit in literals
                  if any(ord(c) > 127 for c in lit)]
    assert not violations, (
        f"{filepath.name} 有 {len(violations)} 处非 ASCII logger/print 字面量，"
        f"可能撞 GBK/cp1252 编码乱码:\n" +
        "\n".join(f"  L{line} {method}({lit!r})" for line, method, lit in violations)
    )


def test_hot_path_files_exist():
    """守门：HOT_PATH_FILES 里每个文件存在；删除文件时本测试也失败提示。"""
    for p in HOT_PATH_FILES:
        assert p.exists(), f"hot-path 文件 {p} 不存在（已删除？更新 HOT_PATH_FILES 列表）"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))