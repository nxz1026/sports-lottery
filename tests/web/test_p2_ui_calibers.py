"""P2-A（2026-10-23）：**前端**口径修正的回归锁（static/dashboard.html + static/jc.html）。

逐条对应 2026-10-23 三路审计（静态 + 生产动态运行 + web 逐元素）出的 24 条 P2 里，
分给「前端」这一组的 10 条：

  A1 · ``pct(null)`` / ``pct("")`` 印成「0.0%」—— 把「**没有值**」印成「**0**」。
  A2 · 篮球缺 ``win_prob`` 时印「客胜 100.0%」—— 凭空造出确定性。
  A3 · 推荐单面板「数据截至 …」用**浏览器本地时区** ⇒ 同一快照在不同机器上显示不同时间。
  A4 · 定价快照时间戳按 **UTC** 打印（「00:22:48 UTC」其实是 BJT 08:22）。
  A5 · 「当日额度」口径必须按 **BJT 自然日**；前端不许再拿 UTC 日期切片当「今天」。
  A6 · 60 秒定时器**只刷推荐单**却把全页「最近同步」时间戳也更新了。
  A7 · AI 分析面板用**原始英文**（``no analysis yet（先 POST /api/v1/jobs/ai-analyze）``）
        并把**内部 API 路径**甩给终端用户。
  A8 · 右侧「信心榜」空态被清空 ⇒ 面板整块空白。
  A9 · 临时赛事表用硬编码浅黄底 ``#ffe082`` ⇒ 暗色模式下一块刺眼的浅黄。
  A10 · ``static/jc.html`` 的 API 基址写死根路径 ⇒ 生产**永久显示「请先登录」**。

本文件是**静态层 + 运行时层**的双层锁：

* **运行时层**（真调用函数、真渲染面板）：``tools/dashboard_smoke.js``（Node + vm 桩）。
  每条断言都有一个**稳定 key**（``p2a_*``），本文件按 key 逐条断言它被报告为 OK。
* **反向对照**：把每条修复**回退**（还原事故现场）⇒ 冒烟**必须报 FAIL**。
  没有反向对照，就无法证明这些断言不是「空断言」（怎么改都绿）。

零真库、零真网络、零真锁：只读静态文件 + 起一个 node 子进程。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = REPO_ROOT / "static" / "dashboard.html"
JC_HTML = REPO_ROOT / "static" / "jc.html"
SMOKE = REPO_ROOT / "tools" / "dashboard_smoke.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="当前环境未安装 node ⇒ 本项未检查（非失败）")


def _inline_script(src: str) -> str:
    """取页面里的内联 script（无 src 的）正文。"""
    m = re.search(r"(?s)<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>", src)
    assert m, "页面里应有内联 script"
    return m.group(1)


def _strip_js_comments(src: str) -> str:
    """去掉块注释与**整行**行注释，只留代码。

    ⚠️ 结构性断言必须打在**代码**上：修复旁边的说明注释会引用旧实现
    （例如「旧实现 const w=Number(p.win_prob)」），拿整份文件去搜会把注释也搜进去 ⇒ 假红。
    """
    out = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^[ \t]*//[^\n]*$", "", out)


def _slice_function(code: str, name: str) -> str:
    """取 ``function <name>(`` 起到下一个顶层 ``function `` 之前的源码片段。

    断言必须**限定在被修的那个函数**上：同一个页面里别处也可能出现同形代码
    （例如 confBlock 里也有 ``const w=Number(p.win_prob)``，但它**本来就有判空**）。
    """
    i = code.find(f"function {name}(")
    assert i >= 0, f"找不到函数 {name}"
    j = code.find("\nfunction ", i + 1)
    return code[i:j if j > 0 else len(code)]

# ── 每条 P2-A 对应的冒烟断言 key（tools/dashboard_smoke.js 里的 P2A_CHECKS）──
P2A_KEYS: dict[str, list[str]] = {
    "A1": ["p2a_pct_null_is_dash", "p2a_pct_real_number_ok"],
    "A2": ["p2a_basketball_missing_winprob_unknown", "p2a_basketball_winprob_ok"],
    "A3": ["p2a_stamp_is_bjt"],
    "A4": ["p2a_snapshot_stamp_is_bjt"],
    "A5": ["p2a_quota_label_is_bjt_day", "p2a_history_date_is_bjt"],
    "A6": ["p2a_live_partial_refresh_label", "p2a_live_full_sync_label", "p2a_live_clock_is_bjt"],
    "A7": ["p2a_ai_analyze_no_internal_path"],
    "A8": ["p2a_ranklist_empty_state"],
    "A9": ["p2a_temp_leagues_no_hardcoded_color"],
    "A10": ["p2a_jc_api_base_from_url"],
}


def _run_smoke(html_path: Path | None = None, jc_html_path: Path | None = None) -> subprocess.CompletedProcess:
    """跑看板运行时冒烟。

    ⚠️ 必须显式 encoding="utf-8"：node 吐的是中文，Windows 默认按 GBK 解码会抛
    UnicodeDecodeError（仓库既有 CLI 用例踩过同一个坑）⇒ 抓不到输出也读不到结论。
    """
    cmd = [shutil.which("node"), str(SMOKE)]
    if html_path is not None:
        cmd += ["--html", str(html_path)]
    if jc_html_path is not None:
        cmd += ["--jc-html", str(jc_html_path)]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def smoke_out() -> subprocess.CompletedProcess:
    """冒烟只跑一次（一个模块共用），避免每条用例都起一个 node。"""
    return _run_smoke()


@pytest.fixture(scope="module")
def dash_src() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dash_code() -> str:
    """看板内联 JS 的**代码**（去掉注释）—— 结构性断言打在它上面。"""
    return _strip_js_comments(_inline_script(DASHBOARD.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def jc_src() -> str:
    return JC_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def jc_code() -> str:
    """static/jc.html 内联 JS 的**代码**（去掉注释）。"""
    return _strip_js_comments(_inline_script(JC_HTML.read_text(encoding="utf-8")))


def _assert_keys_reported_ok(smoke_out: subprocess.CompletedProcess, item: str) -> None:
    """该条的每个冒烟断言都必须被报告为 OK（key 出现在 OK 行里，且不在 FAIL 行里）。"""
    out = smoke_out.stdout + smoke_out.stderr
    for key in P2A_KEYS[item]:
        assert f"OK  : [{key}]" in out, f"{item} 的运行时断言 {key} 没被报告为 OK：\n{out}"
        assert f"FAIL: [{key}]" not in out, f"{item} 的运行时断言 {key} 报了 FAIL：\n{out}"


# ══════════════════════════════════════════════════════════════════════
# A1 · pct()：null / undefined / 空串 / 非数字 ⇒ 「—」，不许印「0.0%」
# ══════════════════════════════════════════════════════════════════════


def test_a1_pct_missing_value_is_dash(smoke_out, dash_code):
    """Number(null)===0 ⇒ 旧实现把「没有值」印成「0.0%」（页面别处约定用「—」）。"""
    _assert_keys_reported_ok(smoke_out, "A1")
    assert "function pct(v)" in dash_code, "看板的 pct helper 不见了"
    # 旧写法（无任何判空）不许回来
    assert "function pct(v){const n=Number(v);" not in dash_code, \
        "pct 又变回无判空的 Number(v) 了 ⇒ null 会被印成 0.0%"


# ══════════════════════════════════════════════════════════════════════
# A2 · 篮球缺 win_prob ⇒ 明说「还没算出赢面」，不许编「客胜 100.0%」
# ══════════════════════════════════════════════════════════════════════


def test_a2_basketball_missing_win_prob_is_unknown(smoke_out, dash_code):
    """``Number(null)===0`` 且 ``Number.isFinite(0)===true`` ⇒ 缺值被当成「主胜 0%」。"""
    _assert_keys_reported_ok(smoke_out, "A2")
    assert "function winProbOf(p)" in dash_code, "看板缺 winProbOf() 判空 helper"
    # ⚠️ 只看 probBlock：confBlock 里也有 ``const w=Number(p.win_prob)``，但它**本来就有判空**
    #    （``p.win_prob!=null&&p.win_prob!==""``）⇒ 不在本次修复范围内。
    prob_block = _slice_function(dash_code, "probBlock")
    assert "const w=winProbOf(p);" in prob_block, "probBlock 必须走 winProbOf() 判空"
    assert "Number(p.win_prob)" not in prob_block, \
        "probBlock 又变回 Number(p.win_prob) 了 ⇒ 缺值会被印成「客胜 100.0%」"


# ══════════════════════════════════════════════════════════════════════
# A3 · 「数据截至」按 BJT 渲染（不是浏览器本地时区）
# ══════════════════════════════════════════════════════════════════════


def test_a3_data_as_of_stamp_is_bjt(smoke_out, dash_code):
    """同一个快照在不同机器上显示不同时间 ⇒ 口径必须钉死成 BJT。"""
    _assert_keys_reported_ok(smoke_out, "A3")
    assert "数据截至 " in dash_code, "推荐单面板的「数据截至」文案不见了"
    assert "（BJT）" in dash_code, "「数据截至」必须标明 BJT"
    # 旧写法：toLocaleTimeString 不带 timeZone ⇒ 跟着浏览器本地时区走
    assert "fetchedAt.toLocaleTimeString(" not in dash_code, \
        "「数据截至」又变回浏览器本地时区了"
    assert "Asia/Shanghai" in dash_code, "看板必须显式指定 Asia/Shanghai"


# ══════════════════════════════════════════════════════════════════════
# A4 · 定价快照时间戳按 BJT 印（不写「UTC」）
# ══════════════════════════════════════════════════════════════════════


def test_a4_pricing_snapshot_stamp_is_bjt(smoke_out, dash_code):
    """「00:22:48 UTC」看着像凌晨采的，实际是 BJT 08:22。"""
    _assert_keys_reported_ok(smoke_out, "A4")
    assert 'replace("+00:00"," UTC")' not in dash_code, \
        "定价快照时间戳又变回直接印 UTC 了"
    assert "function bjtStamp(v)" in dash_code, "看板缺 bjtStamp() 换算 helper"


# ══════════════════════════════════════════════════════════════════════
# A5 · 「当日额度」按 BJT 自然日 + 前端不许拿 UTC 切片当「今天」
# ══════════════════════════════════════════════════════════════════════


def test_a5_quota_is_bjt_day_and_today_is_not_utc_slice(smoke_out, dash_code):
    """BJT 00:00~08:00 时，``toISOString().slice(0,10)`` 给的是**昨天**。"""
    _assert_keys_reported_ok(smoke_out, "A5")
    assert "今日额度（BJT 自然日）" in dash_code, "「当日额度」必须写明按 BJT 自然日"
    assert "toISOString().slice(0,10)" not in dash_code, \
        "前端还在用 UTC 日期切片判断「今天」（BJT 凌晨会读到昨天）"
    assert "bjtToday()" in dash_code, "「今天」必须走 bjtToday()（BJT 自然日）"


# ══════════════════════════════════════════════════════════════════════
# A6 · 60 秒只刷推荐单 ⇒ 顶部不许写「最近同步」
# ══════════════════════════════════════════════════════════════════════


def test_a6_partial_refresh_must_not_claim_full_sync(smoke_out, dash_code):
    """一个只刷了局部的时间戳，不许冒充全页同步时间。"""
    _assert_keys_reported_ok(smoke_out, "A6")
    assert "推荐单已刷新" in dash_code, "只刷推荐单的定时器必须写「推荐单已刷新」"
    assert "最近同步" in dash_code, "首屏全量刷完仍应写「最近同步」"
    assert "function tickRecommend()" in dash_code and "function setLiveFullSync()" in dash_code, \
        "两个语义必须拆成两个独立文案（否则又是一个时间戳冒充两种口径）"


# ══════════════════════════════════════════════════════════════════════
# A7 · AI 分析面板：中文用户态文案，不许出现内部 API 路径
# ══════════════════════════════════════════════════════════════════════


def test_a7_ai_analyze_empty_state_is_chinese_without_api_path(smoke_out, dash_code):
    """后端 note 原文含 ``POST /api/v1/jobs/ai-analyze`` ⇒ 不许直接印给彩民。"""
    _assert_keys_reported_ok(smoke_out, "A7")
    assert "no analysis yet" not in dash_code, "原始英文 note 不许出现在页面上"
    assert "/api/v1/jobs/ai-analyze" not in dash_code, "内部 API 路径不许出现在页面上"
    assert "尚未生成分析" in dash_code, "AI 分析空态必须有中文用户态文案"
    assert "const note=d.note" not in dash_code, "空态不许直接渲染后端 note"


# ══════════════════════════════════════════════════════════════════════
# A8 · 信心榜空态必须有文案
# ══════════════════════════════════════════════════════════════════════


def test_a8_rank_list_has_empty_state(smoke_out, dash_code):
    """upcomingCards 为空时（今天全已完赛）innerHTML 变成 "" ⇒ 面板整块空白。"""
    _assert_keys_reported_ok(smoke_out, "A8")
    assert "暂无推荐单" in dash_code, "信心榜空态必须有文案"
    assert '$("#rankList").innerHTML=upcomingCards.slice(0,4)' not in dash_code, \
        "rankList 又变回无空态的直写（空数组 ⇒ 整块空白）"


# ══════════════════════════════════════════════════════════════════════
# A9 · 临时赛事表不许再出现硬编码色值
# ══════════════════════════════════════════════════════════════════════


def test_a9_temp_leagues_table_uses_css_variables(smoke_out, dash_code):
    """硬编码浅黄底在暗色模式下一块刺眼 ⇒ 必须走页面已有的语义变量。"""
    _assert_keys_reported_ok(smoke_out, "A9")
    assert not re.search(r"background\s*:\s*#ffe082", dash_code), \
        "临时赛事表不许再写死浅黄底 #ffe082"
    assert not re.search(r"solid\s+#f5e0a1", dash_code), \
        "临时赛事表不许再写死浅黄边 #f5e0a1"
    assert "background:var(--subtle)" in dash_code, "临时赛事表头必须用语义变量"


# ══════════════════════════════════════════════════════════════════════
# A10 · static/jc.html 的 API 基址按当前 URL 推导
# ══════════════════════════════════════════════════════════════════════


def test_a10_jc_html_api_base_is_derived_from_url(smoke_out, jc_code):
    """生产挂在 /jc/static/jc.html ⇒ 根路径请求打不到后端 ⇒ 永远「请先登录」。"""
    _assert_keys_reported_ok(smoke_out, "A10")
    assert 'fetch("/api/jc/' not in jc_code, "jc.html 不许再写死根路径 API"
    assert "location.pathname" in jc_code, "jc.html 必须按当前 URL 推导 API 基址"
    assert "API_BASE" in jc_code, "jc.html 必须用推导出来的基址拼请求"


# ══════════════════════════════════════════════════════════════════════
# 反向对照：把每条修复**回退** ⇒ 冒烟必须报 FAIL
# （否则这些断言就是「怎么改都绿」的空断言）
# ══════════════════════════════════════════════════════════════════════

# (用例名, 目标 key, 旧实现片段, 修复后片段) —— 替换方向：new → old
_REVERT_DASHBOARD: list[tuple[str, str, str, str]] = [
    ("A1", "p2a_pct_null_is_dash",
     'function pct(v){let n;if(typeof v==="number")n=v;else if(typeof v==="string"&&v.trim()!=="")n=Number(v);else return"—";if(!Number.isFinite(n))return"—";return `${(n<=1?n*100:n).toFixed(1)}%`}',
     'function pct(v){const n=Number(v);return Number.isFinite(n)?`${(n<=1?n*100:n).toFixed(1)}%`:"—"}'),
    ("A2", "p2a_basketball_missing_winprob_unknown",
     "const w=winProbOf(p);if(isBasketball(p)&&w!==null)",
     "const w=Number(p.win_prob);if(isBasketball(p)&&Number.isFinite(w))"),
    ("A3", "p2a_stamp_is_bjt",
     "const stamp='<p style=\"margin:0 0 10px;color:var(--dim);font-size:11px\">数据截至 '+bjtClock(fetchedAt)+'（BJT） · 本面板每 60 秒自动刷新</p>';",
     "const stamp='<p style=\"margin:0 0 10px;color:var(--dim);font-size:11px\">数据截至 '+fetchedAt.toLocaleTimeString(\"zh-CN\",{hour12:false})+' · 本面板每 60 秒自动刷新</p>';"),
    ("A4", "p2a_snapshot_stamp_is_bjt",
     'const frWhen=fr.latest_snap?bjtStamp(fr.latest_snap)+" BJT":"";',
     'const frWhen=fr.latest_snap?String(fr.latest_snap).replace("T"," ").replace("+00:00"," UTC").replace("+00"," UTC"):"";'),
    ("A5", "p2a_quota_label_is_bjt_day",
     '<label>今日额度（BJT 自然日）</label><strong>${q.used||0}/${q.limit||"—"}</strong><small>按 BJT 自然日统计${q.day?"（"+esc(q.day)+"）":""}</small>',
     '<label>今日额度</label><strong>${q.used||0}/${q.limit||"—"}</strong>'),
    ("A5", "p2a_history_date_is_bjt",
     '$("#historyDate").value=bjtToday();',
     '$("#historyDate").value=new Date().toISOString().slice(0,10);'),
    ("A6", "p2a_live_partial_refresh_label",
     'if(lt)lt.textContent="推荐单已刷新 · "+bjtClock(new Date(),false)+" BJT"',
     'if(lt)lt.textContent="最近同步 · "+bjtClock(new Date(),false)+" BJT"'),
    ("A6", "p2a_live_full_sync_label",
     'if(lt)lt.textContent="最近同步 · "+bjtClock(new Date(),false)+" BJT"',
     'if(lt)lt.textContent="推荐单已刷新 · "+bjtClock(new Date(),false)+" BJT"'),
    ("A7", "p2a_ai_analyze_no_internal_path",
     'const note="尚未生成分析，点上方「生成当日分析」即可（生成需要一点时间，跑完后刷新本页）。";',
     'const note=d.note||"尚未生成";'),
    ("A8", "p2a_ranklist_empty_state",
     "upcomingCards.length?upcomingCards.slice(0,4).map((p,i)=>`<div style=\"padding:7px 0;border-bottom:1px solid var(--line)\"><span style=\"color:var(--dim)\">${i+1}</span> ${esc(p.home||p.match||\"比赛\")} <strong style=\"float:right;color:var(--gold)\">${stars(p.stars).slice(0,5)}</strong></div>`).join(\"\"):'<div class=\"empty\">暂无推荐单（今日还没有可推荐的赛事）</div>'",
     "upcomingCards.slice(0,4).map((p,i)=>`<div style=\"padding:7px 0;border-bottom:1px solid var(--line)\"><span style=\"color:var(--dim)\">${i+1}</span> ${esc(p.home||p.match||\"比赛\")} <strong style=\"float:right;color:var(--gold)\">${stars(p.stars).slice(0,5)}</strong></div>`).join(\"\")"),
    ("A9", "p2a_temp_leagues_no_hardcoded_color",
     '<tr style="background:var(--subtle)">',
     '<tr style="background:#ffe082">'),
]


@pytest.mark.parametrize("item,key,new_src,old_src", _REVERT_DASHBOARD,
                         ids=[f"{c[0]}-{c[1]}" for c in _REVERT_DASHBOARD])
def test_revert_dashboard_fix_makes_smoke_fail(item, key, new_src, old_src, tmp_path):
    """反向对照（dashboard.html）：把修复换回旧实现 ⇒ 对应断言必须报 FAIL。"""
    html = DASHBOARD.read_text(encoding="utf-8")
    assert new_src in html, f"没找到要回退的片段（{item} / {key}）⇒ 修复形态变了，反向对照失配"
    bugged = html.replace(new_src, old_src, 1)
    out = tmp_path / f"dashboard_reverted_{item}.html"
    out.write_text(bugged, encoding="utf-8")

    proc = _run_smoke(html_path=out)
    assert proc.returncode == 1, \
        f"回退 {item}（{key}）后冒烟居然通过了 ⇒ 该断言是空断言：\n{proc.stdout}\n{proc.stderr}"
    assert f"FAIL: [{key}]" in (proc.stdout + proc.stderr), \
        f"回退 {item} 后冒烟没报 {key} 失败：\n{proc.stdout}\n{proc.stderr}"


def test_revert_jc_html_base_makes_smoke_fail(tmp_path):
    """反向对照（static/jc.html）：API 基址写回根路径 ⇒ p2a_jc_api_base_from_url 必须 FAIL。"""
    jc = JC_HTML.read_text(encoding="utf-8")
    new_src = 'fetch(API_BASE + "fixtures", { credentials: "same-origin" })'
    old_src = 'fetch("/api/jc/fixtures", { credentials: "same-origin" })'
    assert new_src in jc, "没找到要回退的片段 ⇒ 修复形态变了，反向对照失配"
    out = tmp_path / "jc_reverted.html"
    out.write_text(jc.replace(new_src, old_src, 1), encoding="utf-8")

    proc = _run_smoke(jc_html_path=out)
    assert proc.returncode == 1, \
        f"回退 A10 后冒烟居然通过了 ⇒ 该断言是空断言：\n{proc.stdout}\n{proc.stderr}"
    assert "FAIL: [p2a_jc_api_base_from_url]" in (proc.stdout + proc.stderr), proc.stdout + proc.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
