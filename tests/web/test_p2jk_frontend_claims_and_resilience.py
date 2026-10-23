"""P2-J / P2-K（2026-10-23）：前端「用户可见文案」与「首屏健壮性」的回归锁。

⚠️ **这两条都是生产浏览器验收时当场抓到的**（静态审计 + 单测全绿，但页面上看得见）：

**P2-J：Markdown 标记漏到页面上**
    荐单面板实际印着「…竞彩 had 三侧含水（**生产实测 … 12.9%**；…）…」
    「推荐单是**手动生成**的（scripts/decision/recommend.py）：…」
    ⇒ 用户看到的是**一串星号**。根因：前端**不解析 Markdown**（全仓无 marked/showdown），
    而文案作者按「写了 Markdown」的直觉写。

**P2-K：首屏永远停在「连接中…」**
    整块页面停在「连接中…」，所有面板停在「正在加载…」，顶部时间戳不变，
    用户**看不到任何错误提示、也没有重试入口**。
    nginx 侧证据：那一轮 8 个 API **全部 200 且响应体完整发出**，随后客户端自己断开（499）。
    ⇒ 服务端没拖住；但页面代码**确实有真缺口**：
    ``Promise.all([...])`` 里**任一** loader 的 fetch **永不 settle**
    ⇒ Promise.all **永不 resolve** ⇒ ``.finally`` 不执行 ⇒ 首屏永远「连接中…」。

本文件是**静态层 + 运行时层**的双层锁：
* **运行时层**（真调用函数、真渲染面板）：``tools/dashboard_smoke.js``（Node + vm 桩）。
  每条断言都有一个**稳定 key**（``p2j_*`` / ``p2k_*``），本文件按 key 逐条断言它被报告为 OK。
* **反向对照**：把每条修复**回退**（还原事故现场）⇒ 冒烟**必须报 FAIL**。
  没有反向对照，就无法证明这些断言不是「空断言」（怎么改都绿）。

零真库、零真网络、零真锁：只读静态文件 + 起一个 node 子进程。
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = REPO_ROOT / "static" / "dashboard.html"
SMOKE = REPO_ROOT / "tools" / "dashboard_smoke.js"
UVC_TOOL = REPO_ROOT / "tools" / "check_user_visible_claims.py"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="当前环境未安装 node ⇒ 本项未检查（非失败）")

# 冒烟断言 key（tools/dashboard_smoke.js 里的 P2A_CHECKS）
P2JK_KEYS: dict[str, list[str]] = {
    "J1": ["p2j_normal_panels_no_markdown_marker"],
    "J2": ["p2j_degraded_panels_no_markdown_marker"],
    "K1": ["p2k_hanging_request_times_out"],
    "K2": ["p2k_live_not_claimed_synced_when_partial_failure"],
}


def _load_uvc():
    spec = importlib.util.spec_from_file_location("uvc_p2jk", UVC_TOOL)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["uvc_p2jk"] = mod
    spec.loader.exec_module(mod)
    return mod


uvc = _load_uvc()


def _run_smoke(html_path: Path | None = None) -> subprocess.CompletedProcess:
    """跑看板运行时冒烟。

    ⚠️ 必须显式 encoding="utf-8"：node 吐的是中文，Windows 默认按 GBK 解码会抛
    UnicodeDecodeError ⇒ 抓不到输出也读不到结论。
    """
    cmd = [shutil.which("node"), str(SMOKE)]
    if html_path is not None:
        cmd += ["--html", str(html_path)]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def smoke_out() -> subprocess.CompletedProcess:
    """冒烟只跑一次（一个模块共用），避免每条用例都起一个 node。"""
    return _run_smoke()


@pytest.fixture(scope="module")
def dash_code() -> str:
    """看板内联 JS 的**代码**（去掉注释）—— 结构性断言打在它上面。"""
    src = DASHBOARD.read_text(encoding="utf-8")
    m = re.search(r"(?s)<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>", src)
    assert m, "页面里应有内联 script"
    code = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return re.sub(r"(?m)^[ \t]*//[^\n]*$", "", code)


def _assert_keys_reported_ok(smoke_out: subprocess.CompletedProcess, item: str) -> None:
    out = smoke_out.stdout + smoke_out.stderr
    for key in P2JK_KEYS[item]:
        assert f"OK  : [{key}]" in out, f"{item} 的运行时断言 {key} 没被报告为 OK：\n{out}"
        assert f"FAIL: [{key}]" not in out, f"{item} 的运行时断言 {key} 报了 FAIL：\n{out}"


# ══════════════════════════════════════════════════════════════════════
# P2-J · Markdown 标记不许漏到页面上
# ══════════════════════════════════════════════════════════════════════


def test_p2j_runtime_panels_have_no_markdown(smoke_out):
    """正常态面板的渲染结果里不许出现 Markdown 标记。"""
    _assert_keys_reported_ok(smoke_out, "J1")


def test_p2j_runtime_degraded_panels_have_no_markdown(smoke_out):
    """降级态面板（「⚠️ 读取失败（…）」）里也不许出现 Markdown 标记。"""
    _assert_keys_reported_ok(smoke_out, "J2")


def test_p2j_frontend_user_visible_text_has_no_markdown():
    """前端「会渲染给用户的文本」里不许有 Markdown 标记（UVC 口径）。

    ⚠️ 复用 UVC 的枚举（它会剥掉 JS 注释 ⇒ 注释里的星号不算），
    纯代码 token 也不算（否则 `/api/jc/` 这类会被误判）。
    """
    bad = uvc.find_markdown_markers(uvc.frontend_user_visible_strings())
    assert not bad, "\n".join(bad[:10])


def test_p2j_frontend_has_no_markdown_renderer():
    """前提：前端**不解析 Markdown** ⇒ 文案里的星号必然原样显示。

    这条锁住「为什么用户会看到星号」的前提 —— 万一哪天前端引入了 Markdown
    渲染器，判定④的口径就要跟着调整（而不是默默失效）。
    """
    html = DASHBOARD.read_text(encoding="utf-8")
    m = re.search(r"new\s+marked\b|marked\s*\.\s*parse|showdown\s*\.|markdownit\s*\(",
                  html, re.I)
    assert m is None, f"前端出现了 Markdown 渲染器（{m.group(0) if m else ''}）⇒ 判定④口径需重审"


# ══════════════════════════════════════════════════════════════════════
# P2-K · 首屏不许永远停在「连接中…」
# ══════════════════════════════════════════════════════════════════════


def test_p2k_runtime_hanging_request_times_out(smoke_out):
    """请求永远挂着 ⇒ 必须超时失败（promise 永不 settle 是最坏的失败形态）。"""
    _assert_keys_reported_ok(smoke_out, "K1")


def test_p2k_runtime_live_text_is_honest_on_partial_failure(smoke_out):
    """首屏有数据没取到 ⇒ 顶部不许写「最近同步」（那是假时间戳）。"""
    _assert_keys_reported_ok(smoke_out, "K2")


def test_p2k_api_has_timeout(dash_code):
    """`api()` / `jcApi()` 必须走带超时的 fetchTo（不许裸 fetch）。"""
    assert "function fetchTo(" in dash_code, "看板缺 fetchTo()（带超时的 fetch 包装）"
    assert "AbortController" in dash_code, "超时必须用 AbortController（不许靠 Promise.race 硬等）"
    assert "API_TIMEOUT_MS" in dash_code, "必须有超时阈值常量"
    api_fn = dash_code[dash_code.find("async function api("):
                       dash_code.find("async function api(") + 600]
    assert "fetchTo(API+path" in api_fn, "api() 必须走 fetchTo（超时）"
    jc_fn = dash_code[dash_code.find("async function jcApi("):
                      dash_code.find("async function jcApi(") + 600]
    assert "fetchTo(JCAPI+path" in jc_fn, "jcApi() 必须走 fetchTo（超时）"
    # 裸 fetch 不许再直接出现在这两个函数里
    assert "await fetch(API+path" not in api_fn, "api() 又变回裸 fetch 了 ⇒ 请求可能永远挂着"
    assert "await fetch(JCAPI+path" not in jc_fn, "jcApi() 又变回裸 fetch 了 ⇒ 请求可能永远挂着"


def test_p2k_boot_uses_allsettled_and_honest_timestamp(dash_code):
    """首屏批量加载必须用 allSettled；顶部时间戳在失败时如实写「未取到」。"""
    boot = dash_code[dash_code.find("async function _probeAuthAndBoot("):
                     dash_code.find("async function _probeAuthAndBoot(") + 2000]
    assert "Promise.allSettled" in boot, "首屏必须用 Promise.allSettled（一个面板挂掉不拖死其它）"
    assert "Promise.all([loadToday()" not in boot, \
        "首屏又变回 Promise.all 了 ⇒ 任一 loader 挂住就永远停在「连接中…」"
    assert "未取到" in boot, "有数据没取到时，顶部必须如实写「N 项未取到」"


# ══════════════════════════════════════════════════════════════════════
# 反向对照：把修复换回旧实现 ⇒ 对应断言必须报 FAIL
# ══════════════════════════════════════════════════════════════════════

_REVERT_DASHBOARD = [
    ("J2", "p2j_degraded_panels_no_markdown_marker",
     "⚠️ CLV 读取失败（<b>不是「没有数据」</b>）",
     "⚠️ CLV 读取失败（**不是「没有数据」**）"),
    ("K1", "p2k_hanging_request_times_out",
     "r=await fetchTo(API+path,{credentials:\"same-origin\",headers:{Accept:\"application/json\"}},timeoutMs)",
     "r=await fetch(API+path,{credentials:\"same-origin\",headers:{Accept:\"application/json\"}})"),
    ("K2", "p2k_live_not_claimed_synced_when_partial_failure",
     "if(lt)lt.textContent=\"⚠️ \"+failed+\" 项未取到 · \"+bjtClock(new Date(),false)+\" BJT（各面板内有提示与重试）\";",
     "if(lt)lt.textContent=\"最近同步 · \"+bjtClock(new Date(),false)+\" BJT\";"),
]


@pytest.mark.parametrize("item,key,new_src,old_src", _REVERT_DASHBOARD,
                         ids=[f"{c[0]}-{c[1]}" for c in _REVERT_DASHBOARD])
def test_revert_dashboard_fix_makes_smoke_fail(item, key, new_src, old_src, tmp_path):
    """反向对照：回退修复 ⇒ 对应冒烟断言必须报 FAIL（证明它不是空断言）。"""
    html = DASHBOARD.read_text(encoding="utf-8")
    assert new_src in html, f"没找到要回退的片段（{item} / {key}）⇒ 修复形态变了，反向对照失配"
    out = tmp_path / f"dashboard_reverted_{item}.html"
    out.write_text(html.replace(new_src, old_src, 1), encoding="utf-8")

    proc = _run_smoke(html_path=out)
    assert proc.returncode == 1, \
        f"回退 {item}（{key}）后冒烟居然通过了 ⇒ 该断言是空断言：\n{proc.stdout}\n{proc.stderr}"
    assert f"FAIL: [{key}]" in (proc.stdout + proc.stderr), \
        f"回退 {item} 后冒烟没报 {key} 失败：\n{proc.stdout}\n{proc.stderr}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
