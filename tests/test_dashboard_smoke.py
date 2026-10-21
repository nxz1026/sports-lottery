"""看板内联 JS 的**运行时**冒烟（``tools/dashboard_smoke.js``）的测试。

为什么要有这一层：2026-10-21 生产事故「荐单暂不可用：num is not defined」是
**运行时**才炸的 —— 语法检查（``node --check``）查不出、接口（都 200）也查不出，
只有**真跑一次 loadClv()/loadRecommend()、看面板里是不是真内容**才算验收。
上一轮只验了 API 就以为没问题 ⇒ 漏掉了这个事故。

本文件含一条**反向对照**：把 ``num`` 挪回 ``loadAccuracy()`` 内部（还原事故现场），
冒烟脚本必须**报 FAIL** —— 一个抓不到 bug 的冒烟脚本等于没有。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "tools" / "dashboard_smoke.js"
DASHBOARD = ROOT / "static" / "dashboard.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="当前环境未安装 node ⇒ 本项未检查（非失败）")


def _run(html_path: Path) -> subprocess.CompletedProcess:
    # ⚠️ 必须显式 encoding="utf-8"：node 吐的是中文，Windows 默认按 GBK 解码会抛
    # UnicodeDecodeError（仓库既有 CLI 用例踩过同一个坑）⇒ 抓不到输出也读不到结论。
    return subprocess.run([shutil.which("node"), str(SMOKE), "--html", str(html_path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def test_smoke_passes_on_real_dashboard():
    """真实看板：三个面板都要渲染出真内容（不许出现「暂不可用」）。"""
    proc = _run(DASHBOARD)
    assert proc.returncode == 0, f"冒烟失败：\n{proc.stdout}\n{proc.stderr}"
    for panel in ("#clvPanel", "#recommendPanel", "#recordPanel"):
        assert panel in proc.stdout, proc.stdout


def test_smoke_detects_the_original_bug(tmp_path):
    """反向对照：把 ``num`` 挪回 ``loadAccuracy()`` 内部 ⇒ 冒烟必须 FAIL。

    没有这一条，就没法证明冒烟脚本真的能抓住事故那类 bug。
    """
    html = DASHBOARD.read_text(encoding="utf-8")
    # 还原事故现场：删掉全局的 num，把它塞回 loadAccuracy() 体内。
    bugged = html.replace(
        'const num=v=>v===null||v===undefined?"—":"n="+v;\n', "", 1)
    assert "const num=v=>v===null" not in bugged, "没找到要挪走的全局 num"
    bugged = bugged.replace(
        'async function loadAccuracy(){const el=$("#accuracyBreakdown");',
        'async function loadAccuracy(){const num=v=>v===null||v===undefined?"—":"n="+v;'
        'const el=$("#accuracyBreakdown");', 1)
    assert 'async function loadAccuracy(){const num=' in bugged, "没还原成功"
    out = tmp_path / "dashboard_bugged.html"
    out.write_text(bugged, encoding="utf-8")

    proc = _run(out)
    assert proc.returncode == 1, f"还原事故现场后冒烟居然通过了 ⇒ 冒烟无效：\n{proc.stdout}"
    assert "暂不可用" in (proc.stdout + proc.stderr), proc.stdout + proc.stderr


def test_smoke_detects_scope_error_in_catch_block(tmp_path):
    """反向对照 2：把 ``pendTotal`` 挪回 try 块里 ⇒ 冒烟必须 FAIL。"""
    html = DASHBOARD.read_text(encoding="utf-8")
    bugged = html.replace(
        "let pendTotal=0;\ntry{", "try{const pendTotal=0;", 1)
    assert "try{const pendTotal=0;" in bugged, "没还原成功"
    out = tmp_path / "dashboard_bugged2.html"
    out.write_text(bugged, encoding="utf-8")

    proc = _run(out)
    assert proc.returncode == 1, f"还原 try/catch 事故后冒烟居然通过了：\n{proc.stdout}"


def test_smoke_script_has_no_fffd():
    assert "\ufffd" not in SMOKE.read_text(encoding="utf-8")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
