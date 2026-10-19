"""P1-2 验收测试：dailyimage.html XSS 路径修复。

修复点：dailyimage.html 的 innerHTML 内插 `d.note` / `e.message`
未转义，API 形态允许任意字符串流入这两个字段，新增通道即 XSS。
本测试通过正则扫描 dailyimage.html 源码，确认：
1. 脚本块已定义 esc()
2. 两处 innerHTML 拼接都经过 esc() 包装
3. esc 的实现覆盖 & < > " ' 五个字符（OWASP HTML 编码表）
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1].parent


def test_dailyimage_html_has_esc_utility():
    src = (REPO_ROOT / "static" / "dailyimage.html").read_text(encoding="utf-8")
    # 抓 inline <script> 块
    m = re.search(r"<script>(.*?)</script>", src, re.DOTALL)
    assert m, "dailyimage.html should have inline <script>"
    script = m.group(1)
    # 必须有 esc 实现
    assert re.search(r"const\s+esc\s*=", script), (
        "expected `const esc =` in dailyimage.html script block"
    )


def test_esc_implementation_covers_owasp_chars():
    """esc 必须转义 & < > \" ' 五个字符（OWASP HTML 编码）。

    esc 定义是单表达式 `const esc = v => ...`，整条逻辑在同一行直到
    行尾 ';'；通过查找 esc 所在行并断言该行覆盖五个 entity 映射。
    """
    src = (REPO_ROOT / "static" / "dailyimage.html").read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", src, re.DOTALL)
    script = m.group(1)
    # 整行匹配（esc 实现是单行箭头体）
    esc_line = re.search(r"const\s+esc\s*=[^\n]+", script)
    assert esc_line, "esc not defined on a single line"
    body = esc_line.group(0)
    for entity in ["&amp;", "&lt;", "&gt;", "&quot;", "&#39;"]:
        assert entity in body, f"esc missing mapping for {entity!r}"


def test_unavailable_block_uses_esc_on_note():
    """d.note 必须经 esc 包装（XSS 路径：d.note 来自 API）。"""
    src = (REPO_ROOT / "static" / "dailyimage.html").read_text(encoding="utf-8")
    # 找 host.innerHTML='<div class="empty"><h2>还没有今天的图</h2><p>'+(d.note||...)
    # P1-2 修复后必须包 esc()
    pat = re.compile(
        r"还没有今天的图.*?\+\s*(esc\()?d\.note", re.DOTALL
    )
    assert pat.search(src), (
        "expected `esc(d.note...)` in '还没有今天的图' branch; "
        "raw d.note in innerHTML is XSS path"
    )


def test_error_block_uses_esc_on_message():
    """e.message 必须经 esc 包装。"""
    src = (REPO_ROOT / "static" / "dailyimage.html").read_text(encoding="utf-8")
    pat = re.compile(
        r"生成失败.*?\+\s*(esc\()?e\.message", re.DOTALL
    )
    assert pat.search(src), (
        "expected `esc(e.message...)` in catch branch; "
        "raw e.message in innerHTML is XSS path"
    )


def test_esc_renders_payload_safely():
    """手工跑 esc 函数（提取后执行），证明它对 XSS payload 安全。"""
    src = (REPO_ROOT / "static" / "dailyimage.html").read_text(encoding="utf-8")
    m = re.search(r"const\s+esc\s*=[^\n]+", src)
    assert m, "esc implementation not found"
    import os
    import subprocess
    js_src = (
        m.group(0)
        + "\nconst result = esc('<img onerror=alert(1)>');\nprocess.stdout.write(result);\n"
    )
    path = os.path.join(os.environ.get("TEMP", "/tmp"), "_esc_test.js")
    open(path, "w", encoding="utf-8").write(js_src)
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=5)
        out = r.stdout
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    assert "<img" not in out, f"esc failed to escape <img, got: {out!r}"
    assert "&lt;img" in out, f"esc should produce &lt;img, got: {out!r}"
