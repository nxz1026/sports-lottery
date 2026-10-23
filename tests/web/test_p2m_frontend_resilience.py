"""P2-M（2026-10-23）：前端「首屏健壮性」补漏 —— 两个真缺口。

⚠️ **背景**：P2-K 给首屏加了兜底（``fetchTo`` + ``AbortController`` + ``allSettled``），
但**只覆盖了一半**。本批在**生产浏览器验收**时又挖出两个真缺口：

**M1：超时只保护到「响应头」，没保护到「响应体」**
    旧实现里 ``fetchTo`` 在 ``fetch()`` **返回响应头**时就把超时定时器清了
    （``finally{clearTimeout(timer)}``）⇒ 若响应头到了但**响应体永远不来**
    （半开连接 / 代理只发头不发体 / 上游挂着不吐数据），
    后面 ``await r.json()`` **永不 settle** ⇒ 该 loader 永不结束
    ⇒ 首屏仍然会**永久**停在「连接中…」⇒ **P2-K 的兜底没覆盖到这一段**。
    ⇒ 现在响应体在**同一个 AbortController 保护下**读完（``text``），
    超时一样会 abort ⇒ 挂起变成失败 ⇒ 各面板印「加载失败 + 重试」。

**M2：点「每日一图」/「分享」会把主区清空**
    导航栏里这两个按钮只有 ``data-action``、**没有** ``data-view``；
    旧实现给**所有** ``.nav button`` 都挂了 ``switchView(b.dataset.view)``
    ⇒ 传入 ``undefined`` ⇒ ``v.id==="view-undefined"`` 对谁都不成立
    ⇒ 所有 ``.view`` 的 ``active`` 都被去掉 ⇒ **主区整块空白**
    （用户在「今日推荐」页点一下「每日一图」就把整个看板点没了）。
    ⇒ 现在：只给带 ``data-view`` 的按钮挂监听，且 ``switchView`` 对未知名字**原样返回**。

本文件是**静态层 + 运行时层**的双层锁（与 P2-J/P2-K 同一范式）：
* **运行时层**：``tools/dashboard_smoke.js`` 里的两条新断言
  （``p2m_body_never_completes_times_out`` / ``p2m_switchview_unknown_name_keeps_current_view``）；
* **反向对照**：把每条修复**回退**（还原事故现场）⇒ 冒烟**必须报 FAIL**
  ⇒ 证明这些断言不是「空断言」（怎么改都绿）。

零真库、零真网络、零真锁：只读静态文件 + 起 node 子进程。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = REPO_ROOT / "static" / "dashboard.html"
SMOKE = REPO_ROOT / "tools" / "dashboard_smoke.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="当前环境未安装 node ⇒ 本项未检查（非失败）")

P2M_KEYS: dict[str, list[str]] = {
    "M1": ["p2m_body_never_completes_times_out"],
    "M2": ["p2m_switchview_unknown_name_keeps_current_view"],
}


def _run_smoke(html_path: Path | None = None) -> subprocess.CompletedProcess:
    """跑看板运行时冒烟（⚠️ 必须显式 utf-8：node 吐中文，Windows 默认 GBK 会解码失败）。"""
    cmd = [shutil.which("node"), str(SMOKE)]
    if html_path is not None:
        cmd += ["--html", str(html_path)]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def smoke_out() -> subprocess.CompletedProcess:
    return _run_smoke()


@pytest.fixture(scope="module")
def dash_code() -> str:
    """看板内联 JS 的**代码**（去掉注释）—— 结构性断言打在它上面。"""
    src = DASHBOARD.read_text(encoding="utf-8")
    m = re.search(r"(?s)<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>", src)
    assert m, "页面里应有内联 script"
    code = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return re.sub(r"(?m)^[ \t]*//[^\n]*$", "", code)


def _fn(code: str, name: str) -> str:
    """取某个函数的源码片段（从函数名到下一个同缩进收尾的 ``}`` 之前）。"""
    i = code.find(f"function {name}(")
    assert i >= 0, f"代码里找不到 {name}"
    j = code.find("\n}", i)
    return code[i: j + 2 if j > 0 else len(code)]


# ══════════════════════════════════════════════════════════════════════
# M1 · 响应体卡住也必须超时
# ══════════════════════════════════════════════════════════════════════


def test_p2m_runtime_body_hang_times_out(smoke_out):
    """响应头到了但响应体永远不来 ⇒ api() 必须超时失败。"""
    out = smoke_out.stdout + smoke_out.stderr
    for key in P2M_KEYS["M1"]:
        assert f"OK  : [{key}]" in out, f"M1 的运行时断言 {key} 没被报告为 OK：\n{out}"
        assert f"FAIL: [{key}]" not in out, f"M1 的运行时断言 {key} 报了 FAIL：\n{out}"


def test_p2m_body_read_is_inside_the_timeout_guard(dash_code):
    """``await r.text()`` 必须在 **try 块内**（超时定时器还活着的时候）读完。

    ⚠️ 关键：**在 finally 里清定时器之前**读体 ⇒ 否则响应体卡住时
    ``r.json()`` 永不 settle ⇒ 首屏又永久停在「连接中…」（P2-K 的盲区）。
    """
    fn = _fn(dash_code, "fetchTo")
    t = fn.find("try{")
    c = fn.find("catch(e){if(timedOut)")
    body = fn.find("await r.text()")
    assert t >= 0 and c > t, "fetchTo 结构不对（找不到 try/catch）"
    assert body > t and body < c, (
        "响应体必须在 try 块内读完（超时保护之内）—— "
        f"try@{t} catch@{c} body@{body}")


def test_p2m_fetch_to_still_returns_status_and_json(dash_code):
    """改完之后 fetchTo 仍必须给出 status / ok / json()（调用方都依赖它们）。"""
    fn = _fn(dash_code, "fetchTo")
    assert "status:r.status" in fn
    assert "ok:r.ok" in fn
    assert "json:()=>" in fn


# ══════════════════════════════════════════════════════════════════════
# M2 · 未知视图名不许把主区清空
# ══════════════════════════════════════════════════════════════════════


def test_p2m_runtime_switchview_unknown_name_keeps_view(smoke_out):
    """switchView(未知名字) ⇒ 当前视图必须还在（不许主区整块空白）。"""
    out = smoke_out.stdout + smoke_out.stderr
    for key in P2M_KEYS["M2"]:
        assert f"OK  : [{key}]" in out, f"M2 的运行时断言 {key} 没被报告为 OK：\n{out}"
        assert f"FAIL: [{key}]" not in out, f"M2 的运行时断言 {key} 报了 FAIL：\n{out}"


def test_p2m_nav_switch_listens_only_to_data_view_buttons(dash_code):
    """导航栏**只给带 data-view 的按钮**挂视图切换。

    ⚠️ 「每日一图」「分享」只有 data-action ⇒ 旧实现给它们也挂了
    ``switchView(undefined)`` ⇒ 点一下就把主区清空。
    """
    assert '.nav button[data-view]' in dash_code, \
        "视图切换监听必须只挂在 .nav button[data-view] 上"
    # ⚠️ 只查**监听那一处**：`switchView` 内部还有一句
    # `document.querySelectorAll(".nav button").forEach(b=>b.classList.toggle("active",…))`
    # —— 那是**高亮当前导航项**，是合法的；不许拿它当「监听挂太宽」的证据。
    assert '.nav button").forEach(b=>b.addEventListener("click",()=>switchView(' not in dash_code, \
        "不许再给所有 .nav button（含没有 data-view 的）挂视图切换"


def test_p2m_switchview_guards_unknown_names(dash_code):
    """``switchView`` 对未知视图名必须**原样返回**（不许把 active 全去掉）。"""
    assert 'if(!name||!document.getElementById("view-"+name))return;' in dash_code, \
        "switchView 必须对未知视图名直接返回（否则主区被清空）"


# ══════════════════════════════════════════════════════════════════════
# 反向对照：把修复回退 ⇒ 冒烟必须报 FAIL（证明断言不是空断言）
# ══════════════════════════════════════════════════════════════════════


def test_p2m_reverse_control_body_read_outside_guard_fails(tmp_path):
    """回退 M1（响应体在保护外读）⇒ 冒烟必须 FAIL。"""
    src = DASHBOARD.read_text(encoding="utf-8")
    patched = re.sub(
        r"  try\{\n    const r=await fetch\(url,.*?\n  \}\n  catch\(e\)\{if\(timedOut\)",
        "  try{return await fetch(url,ctl?Object.assign({},init||{},{signal:ctl.signal}):init)}\n"
        "  catch(e){if(timedOut)",
        src, flags=re.S)
    assert patched != src, "反向补丁没命中（锚点变了？）"
    p = tmp_path / "dashboard.html"
    p.write_text(patched, encoding="utf-8")
    r = _run_smoke(p)
    out = r.stdout + r.stderr
    assert r.returncode != 0, "回退 M1 后冒烟仍然通过 ⇒ 断言是空断言"
    assert "FAIL: [p2m_body_never_completes_times_out]" in out, out[-2000:]


def test_p2m_reverse_control_switchview_without_guard_fails(tmp_path):
    """回退 M2（switchView 不守卫未知名字）⇒ 冒烟必须 FAIL。"""
    src = DASHBOARD.read_text(encoding="utf-8")
    patched = src.replace('if(!name||!document.getElementById("view-"+name))return;', "")
    assert patched != src, "反向补丁没命中（锚点变了？）"
    p = tmp_path / "dashboard.html"
    p.write_text(patched, encoding="utf-8")
    r = _run_smoke(p)
    out = r.stdout + r.stderr
    assert r.returncode != 0, "回退 M2 后冒烟仍然通过 ⇒ 断言是空断言"
    assert "FAIL: [p2m_switchview_unknown_name_keeps_current_view]" in out, out[-2000:]
