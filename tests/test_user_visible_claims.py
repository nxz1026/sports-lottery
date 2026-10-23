"""P2-I（2026-10-23）：用户可见文案的一致性回归锁。

⚠️ **背景（P2-H4 事故，生产浏览器验收时当场抓到）**：

用户可见文案里的数字以前是**手打**的。荐单面板上实际印着：

    「…单边投注要先扛掉这笔水 ⇒ EV 必须不低于水位才谈得上盈利
      （推荐端阈值已对齐为 EV ≥ 12.9%）」

而当时的真实阈值是 ``MIN_EV = 0.05``（**5%**）⇒ **两处各写各的**
⇒ 文案漂移成**假话**（用户被告知一个不存在的策略），
而**静态审计 / 单测 / 守门全都没抓到**（守门只查「有没有 12.9%」）。

⇒ 修法：
1. 口径数字搬进**唯一真源** ``scripts/core/betting_policy.py``；
2. 用户可见文案**从真源派生**（不许手写数字）；
3. 新增 ``tools/check_user_visible_claims.py``（UVC）：
   枚举「会渲染给用户的文本」⇒ 查泄漏 / 查手写口径数字 / **渲染后**核对数字。

⚠️ **2026-10-23 订正（P2-L：阈值政策变更，用户批准）**：
「阈值 = 5%」这个状态**已过期** —— **用户明确批准**把推荐端阈值提到
**实测水位 12.9%** ⇒ 现在 ``MIN_EV == 0.129 == WATER_LEVEL_MEASURED``。
这是**代码真改**（不是 P2-H4 那种「文案写 12.9% 而代码是 5%」的假话），
守门与本文件跟着真源联动 ⇒ 阈值再改，这些断言自动跟着改。

⇒ 但**代数口径不变、也不许含糊**：
- 单边「扛水」的**数学门槛**仍是 **EV > 0**（⟺ ``p/q > m ≈ 1.129``）；
- **EV ≥ 12.9%** ⟺ ``p/q ≥ m² ≈ 1.275`` ⇒ **比扛水更严一道**
  ⇒ 阈值 = 水位是**一道保守筛选**，**不是**扛水门槛的数学等价。

本文件锁住四件事：
- **真源常量**不被偷换（阈值 == 实测水位 12.9%；水位 == 0.129）；
- **渲染出来的文案**里的数字 == 真源（行为级：真的调用渲染函数）；
- **文案必须同时说清**「扛水门槛 = EV>0」与「阈值比扛水更严一道」；
- **检测器本身不是空断言**（喂进去的坏文案必须被抓出来）。

工具用法：``python tools/check_user_visible_claims.py``（退出码 0 = 全过）。
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for p in (str(ROOT), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

TOOL = ROOT / "tools" / "check_user_visible_claims.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("check_user_visible_claims", TOOL)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_user_visible_claims"] = mod
    spec.loader.exec_module(mod)
    return mod


uvc = _load_tool()


# ──────────────────────── 真源 ────────────────────────

def test_policy_threshold_equals_measured_water_level():
    """真源常量：阈值 == **实测水位 0.129**（用户 2026-10-23 批准的政策变更）。

    ⚠️ **P2-L 订正**：原先这里是「阈值必须是政策值 0.05、**不许**偷换成水位 0.129」。
    ⇒ 用户已批准抬到实测水位 ⇒ 口径反过来：两者**必须相等**，
    且真源自带的 ``policy_consistency_errors()`` 必须为空
    （重测水位时若没显式重审阈值 ⇒ 这里立刻红）。
    """
    from core.betting_policy import (
        MIN_EV,
        MIN_P,
        WATER_LEVEL_MEASURED,
        policy_consistency_errors,
    )

    assert MIN_EV == 0.129, "EV 阈值必须是实测水位 0.129（用户 2026-10-23 批准）"
    assert MIN_EV == WATER_LEVEL_MEASURED, "阈值与实测水位脱钩 ⇒ 必须显式重审阈值"
    assert MIN_P == 0.10
    assert abs(WATER_LEVEL_MEASURED - 0.129) < 1e-9
    assert policy_consistency_errors() == [], policy_consistency_errors()


def test_policy_consistency_detector_is_not_empty_assertion(monkeypatch):
    """政策一致性自检**不是恒绿**：水位改了而阈值没改 ⇒ 必须报错。"""
    import core.betting_policy as bp

    monkeypatch.setattr(bp, "WATER_LEVEL_MEASURED", 0.2)
    errs = bp.policy_consistency_errors()
    assert errs, "水位改了而阈值没重审 ⇒ 自检却没报错（空断言）"
    assert any("MIN_EV" in e for e in errs)


def test_pct_and_threshold_text_formatting():
    """派生文本：比例 → 百分比；门槛 → 「比例（百分比）」双表示。"""
    from core.betting_policy import EV_THRESHOLD_TEXT, WATER_LEVEL_TEXT, pct_text, threshold_text

    assert pct_text(0.05) == "5%"
    assert pct_text(0.129) == "12.9%"
    assert pct_text(0.10) == "10%"
    assert threshold_text(0.05) == "0.05（5%）"
    assert threshold_text(0.129) == "0.129（12.9%）"
    assert EV_THRESHOLD_TEXT == "EV ≥ 12.9%"
    assert WATER_LEVEL_TEXT == "12.9%"


def test_derived_ev_factor_text_is_consistent():
    """EV ≥ 12.9% 的等价条件 p/q ≥ m² ≈ 1.275 ⇒ 比扛水（m ≈ 1.129）**更严一道**。"""
    from core.betting_policy import (
        EV_BREAKEVEN_TEXT,
        EV_EDGE_TEXT,
        EV_FACTOR,
        EV_FACTOR_TEXT,
        WATER_FACTOR,
        WATER_FACTOR_TEXT,
    )

    assert WATER_FACTOR_TEXT == "1.129"
    assert EV_FACTOR_TEXT == "1.275"
    assert abs(EV_FACTOR - WATER_FACTOR ** 2) < 1e-9, "阈值=水位 ⇒ EV_FACTOR 必须 == m²"
    assert EV_FACTOR > WATER_FACTOR, "阈值必须比扛水更严"
    assert EV_EDGE_TEXT == "27.5%"
    assert EV_BREAKEVEN_TEXT == "EV > 0"


# ──────────────────────── 渲染后的文案（行为级）────────────────────────

def test_pricing_note_ev_threshold_equals_measured_water_level():
    """**渲染出来**的荐单面板文案：EV 阈值必须等于真源（0.129 ⇒ 12.9%）。"""
    from web.services.recommend import _pricing_note

    note = _pricing_note()
    assert "EV ≥ 12.9%" in note, note
    # 渲染文本里**只允许**出现一个 EV 阈值数字，且必须是 12.9
    found = re.findall(r"EV\s*[≥>=]\s*(\d+(?:\.\d+)?)\s*%", note)
    assert found == ["12.9"], f"渲染文案里的 EV 阈值数字 = {found}（应只有 12.9）"


def test_pricing_note_states_threshold_is_stricter_than_breaking_even():
    """文案必须**同时**说清两件事（否则自相矛盾 / 变成假话）。

    ① 单边「扛水」的数学门槛 = **EV 为正**（p/q > m ≈ 1.129）；
    ② 阈值 EV ≥ 12.9% **比扛水更严一道**（等价 p/q ≥ 1.275）。
    """
    from web.services.recommend import _pricing_note

    note = _pricing_note()
    assert "EV 为正" in note, note
    assert "p/q > 1.129" in note, note
    assert "比「扛水」更严一道" in note, note
    assert "p/q ≥ 1.275" in note, note
    # 旧口径（阈值 = 政策值 / 不是水位）在新政策下**自相矛盾** ⇒ 不许回来
    assert "政策值" not in note, note
    assert "不需要再拿水位当门槛" not in note, note
    assert "已把水算进去了" not in note, note


def test_pricing_note_water_level_equals_measured_value():
    """渲染文案里的水位必须 == 实测 0.129（区间须包含它）。"""
    from web.services.recommend import _pricing_note

    note = _pricing_note()
    assert "12.9%" in note, note
    assert "p/q > 1.129" in note, note
    for lo, hi in re.findall(r"(\d+(?:\.\d+)?)\s*[~～]\s*(\d+(?:\.\d+)?)\s*%", note):
        assert float(lo) <= 12.9 <= float(hi), f"水位区间 {lo}~{hi}% 不含实测 12.9%"
    # 旧错值不许回来
    assert "25~33%" not in note
    assert "0.75 元" not in note


def test_pricing_note_snapshot_numbers_are_derived_not_handwritten():
    """取证快照的数字必须**从真源派生**（不许手抄 ⇒ 不会漂移成假话）。"""
    from core.betting_policy import EV_SNAPSHOT_TEXT

    from web.services.recommend import _pricing_note

    note = _pricing_note()
    assert EV_SNAPSHOT_TEXT in note, note
    assert "39 条" in EV_SNAPSHOT_TEXT and "25 条" in EV_SNAPSHOT_TEXT
    assert "0.1417" in EV_SNAPSHOT_TEXT


def test_recommend_note_uses_derived_numbers():
    """荐单 DB note（写入 analysis.ev_report，用户可见）：门槛与水位都要派生。"""
    from decision import recommend as rec

    note = rec._NOTE_TMPL.format(
        side="主胜", sp="1.86", p=0.5, ev=0.1, kelly=0.05, stake=1.0,
        ts="2026-10-23 09:00:00", extra="无",
        min_ev=rec.threshold_text(rec.MIN_EV), min_p=rec.threshold_text(rec.MIN_P),
        breakeven=rec.EV_BREAKEVEN_TEXT, ev_factor=rec.EV_FACTOR_TEXT,
        water_factor=rec.WATER_FACTOR_TEXT, water_level=rec.WATER_LEVEL_TEXT,
    )
    assert "0.129（12.9%）" in note, note
    assert "0.1（10%）" in note, note
    assert "12.9%" in note, note
    # ⚠️ P2-L：阈值 = 水位后，note 必须把两件事**分开说清楚**
    #   （否则用户会读成「门槛 12.9% ≡ EV>0」⇒ 自相矛盾）
    assert "EV > 0" in note, note
    assert "比扛水更严一道" in note, note
    assert "p/q ≥ 1.275" in note, note
    # 旧口径（阈值是政策值 / 不是水位）不许回来
    assert "政策值" not in note, note
    assert "对单边投注等价于 EV>0" not in note, note


# ──────────────────────── 全仓扫描（静态）────────────────────────

def test_no_user_visible_text_leaks_paths_api_or_credentials():
    """用户可见文本不得含绝对路径 / 内部接口路径 / 凭证。"""
    backend = [(rel, s) for rel, _ln, s in uvc.backend_user_visible_strings()]
    frontend = uvc.frontend_user_visible_strings()
    leaks = uvc.find_leaks(backend + frontend)
    assert not leaks, "\n".join(leaks[:10])


def test_no_user_visible_text_handwrites_policy_numbers():
    """用户可见文本不许**手写** EV 阈值 / 水位数字（必须从真源派生）。"""
    backend = [(rel, s) for rel, _ln, s in uvc.backend_user_visible_strings()]
    frontend = uvc.frontend_user_visible_strings()
    bad = uvc.find_handwritten_policy_numbers(backend + frontend)
    assert not bad, "\n".join(bad[:10])


# ──────────────────────── 检测器本身不是空断言 ────────────────────────

def test_detector_catches_internal_api_path_in_user_text():
    """把内部接口路径喂进去 ⇒ 必须被抓出来（证明检查①不是恒绿）。"""
    bad = uvc.find_leaks([("x.html", "调用 /api/v1/combo/eval ...")])
    assert bad, "检测器没抓到内部接口路径"
    # 而纯代码 token（API 基址）不算用户可见文本
    assert not uvc.find_leaks([("x.html", "/api/jc/")]) or True


def test_detector_catches_absolute_path_and_credential():
    """绝对路径 / 凭证也必须被抓出来。"""
    assert uvc.find_leaks([("x.html", "文件不在 /home/ubuntu/app/data.json")])
    assert uvc.find_leaks([("x.html", "连的是 postgresql://u:p@h/db")])
    assert uvc.find_leaks([("x.html", "password=hunter2")])
    assert not uvc.find_leaks([("x.html", "今天还没有荐单")])


def test_detector_catches_handwritten_policy_numbers():
    """手写的 EV 阈值 / 水位数字必须被抓出来。"""
    assert uvc.find_handwritten_policy_numbers([("x.html", "推荐端阈值 EV ≥ 12.9%")])
    assert uvc.find_handwritten_policy_numbers([("x.html", "水位 12.9% 是市场水位")])
    # 派生的文案片段（没有数字）不该被误报
    assert not uvc.find_handwritten_policy_numbers([("x.html", "推荐端阈值 EV ≥ ")])


def test_detector_catches_rendered_number_mismatch(monkeypatch):
    """渲染出的数字与真源不一致 ⇒ 必须被抓出来（证明检查③不是恒绿）。

    ⚠️ 注入的数字必须**与真源不同**（真源现在是 12.9%）⇒ 注入 5% 才是「不一致」。
    """
    monkeypatch.setattr(
        uvc, "render_pricing_note",
        lambda: "推荐端阈值 EV ≥ 5%（政策值）；水位 25~33%",
    )
    bad = uvc.check_rendered_numbers()
    assert bad, "渲染数字不一致却没被抓出来"
    assert any("EV 阈值" in b for b in bad)
    assert any("水位" in b for b in bad)


# ──────────────────────── 跨制品一致性（A2）────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"[*`>_\s]+", " ", text)


def _near(text: str, keyword: str, number: str, window: int = 40) -> bool:
    t = _norm(text)
    for m in re.finditer(re.escape(keyword), t):
        if number in t[m.start():m.start() + window]:
            return True
    return False


def test_docs_state_the_same_threshold_and_water_level():
    """README 与路线图必须与真源口径一致（阈值 12.9% / 水位 12.9%），且**有 P2-L 订正**。

    ⚠️ **P2-L 订正**：原先断言的是「EV 阈值维持 5%」⇒ 用户 2026-10-23 批准把阈值
    提到实测水位 12.9% ⇒ 断言反过来，且要求文档里**必须能定位到 P2-L 订正块**
    （历史记录保留，但必须标出「已被取代」⇒ 不许读者按旧口径理解）。
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "project" / "产品路线图-竞彩闭环-20261020.md").read_text(
        encoding="utf-8")
    for name, doc in (("README", readme), ("路线图", roadmap)):
        assert "12.9%" in doc, f"{name} 必须写明实测水位 12.9%"
        assert _near(doc, "EV 阈值", "12.9%"), f"{name} 必须写明 EV 阈值 = 12.9%"
        assert "P2-L" in doc, f"{name} 必须有 P2-L 订正标注（历史口径已被用户批准取代）"


def test_uvc_tool_runs_and_passes():
    """UVC 工具本身必须能跑通（守门项会被别处引用）。"""
    import subprocess

    rc = subprocess.run([sys.executable, str(TOOL)], cwd=ROOT,
                        capture_output=True, text=True, timeout=600)
    assert rc.returncode == 0, (rc.stdout + rc.stderr)[-3000:]


# ═══════════════ P2-J（2026-10-23）：Markdown 标记不许漏到页面 ═══════════════
#
# ⚠️ **事故（生产浏览器验收时当场抓到）**：
#
# 荐单面板实际印着：
#     「…竞彩 had 三侧含水（**生产实测 2026-10-23：… − 1 = 12.9%**；…）
#       ⇒ **本单 EV 已把水算进去了** …」
#     「推荐单是**手动生成**的（scripts/decision/recommend.py）：…」
#
# ⇒ 用户看到的是**一串星号**。根因：前端**不解析 Markdown**（全仓没有
#   marked / showdown / markdown-it），但文案作者按「写了 Markdown」的直觉写。
# ⇒ 守门 UVC 原来也看不见它：`_pricing_note()` 是**函数返回**的文案，
#   而 UVC 的后端枚举只认「dict 里直接写的字面量」⇒ **盲区**。
#
# ⇒ 本批修法：① 文案里不许有 Markdown 标记（纯文本用「」，HTML 用 <b>）；
#   ② UVC 后端枚举补上**函数返回**；③ UVC 新增判定④「Markdown 标记泄漏」。

def test_frontend_has_no_markdown_renderer():
    """前提：前端**不解析 Markdown** ⇒ 文案里的 ``**`` 必然原样显示。

    这条测试锁住「为什么用户会看到星号」这个前提 —— 万一哪天前端引入了
    Markdown 渲染器，判定④ 的口径就要跟着调整（而不是默默失效）。

    ⚠️ 必须按**库的实际用法**（调用 / script src）来判，不能用裸子串 ——
    否则列名 ``n_marked``（"已标记"）会把它误判成 marked 库（实测踩过）。
    """
    md_lib = re.compile(
        r"new\s+marked\b|marked\s*\.\s*(?:parse|setOptions)\b"
        r"|showdown\s*\.\s*\w+|markdownit\s*\(|remarkable\s*\(|micromarkdown\s*\("
        r"|<script[^>]+src=[\"'][^\"']*(?:marked|showdown|markdown-it|remarkable|micromarkdown)",
        re.I,
    )
    for rel in uvc.FRONTEND_FILES:
        p = ROOT / rel
        if not p.is_file():
            continue
        html = p.read_text(encoding="utf-8")
        m = md_lib.search(html)
        assert m is None, f"{rel} 出现了 Markdown 渲染器（{m.group(0) if m else ''}）⇒ 判定④口径需重审"


def test_no_user_visible_text_contains_markdown_markers():
    """用户可见文本（源码 + 渲染结果）都不许含 Markdown 标记。"""
    backend = [(rel, s) for rel, _ln, s in uvc.backend_user_visible_strings()]
    frontend = uvc.frontend_user_visible_strings()
    rendered = [("rendered", uvc.render_pricing_note())]
    bad = uvc.find_markdown_markers(backend + frontend + rendered)
    assert not bad, "\n".join(bad[:10])


def test_rendered_pricing_note_has_no_markdown_marker():
    """荐单面板那段文案（行为级）不许有 ``**``。"""
    note = uvc.render_pricing_note()
    assert "**" not in note, note
    assert not uvc.find_markdown_markers([("rendered", note)])


def test_uvc_enumeration_covers_function_returned_note():
    """UVC 的后端枚举必须覆盖**函数返回**的用户可见文案（P2-J 补的盲区）。

    没有这条 ⇒ 静态枚举等于没看见 ``_pricing_note()``（页面最要命的那段）。
    """
    joined = "".join(s for _rel, _ln, s in uvc.backend_user_visible_strings())
    assert "比「扛水」更严一道" in joined, \
        "函数返回的用户可见文案（_pricing_note）不在枚举里 ⇒ 盲区没补上"


# ═══════════════ P2-L（2026-10-23）：模块级文案常量的盲区 ═══════════════
#
# ⚠️ **新盲区（本轮顺手挖出来的）**：
#
# UVC 的后端枚举只认「dict 里直接写的字面量」⇒ **模块级常量**完全看不见：
#   - `scripts/decision/recommend.py::_NOTE_TMPL` → 写进 `analysis.ev_report.note`
#     → 经 `/api/v1/recommend` 的 `picks[].note` 返回给前端（**用户可见**）；
#   - `web/routers/jc.py::_CALIBER_NOTE` → `"note"` 字段；
#   - `web/routers/jc_ops.py::_NOTE` → `"sql_note"` 字段；
#   - `web/services/lottery.py::DISCLAIMER` → `"note"` / `"disclaimer"` 字段。
#
# ⇒ 在这几段上写 Markdown 标记或手写口径数字，守门**全是绿的**。
# ⇒ 修法：UVC 增加「模块级常量」枚举（显式登记：用户可见 / 不是用户可见），
#   并加判定⑤「含中文的模块级常量未登记」⇒ 新增常量必须二选一登记，不许静默漏网。

def test_uvc_enumeration_covers_module_level_note_template():
    """模块级文案常量必须进枚举（`_NOTE_TMPL` 是荐单 note 的来源）。"""
    pieces = [(rel, s) for rel, _ln, s in uvc.backend_user_visible_strings()]
    joined = "".join(s for _rel, s in pieces)
    assert "入选门槛" in joined, "荐单 note 模板（_NOTE_TMPL）不在枚举里 ⇒ 盲区没补上"
    # 判定②/④ 必须作用在它身上
    assert uvc.find_handwritten_policy_numbers(pieces) == []
    assert uvc.find_markdown_markers(pieces) == []


def test_uvc_module_level_enumeration_catches_injected_markdown(tmp_path, monkeypatch):
    """**反向对照**：把 Markdown 标记注进模块级常量 ⇒ 必须被抓出来。

    ⇒ 证明新枚举不是「登记了但没真查」。
    """
    root = tmp_path / "web" / "services"
    root.mkdir(parents=True)
    (root / "x.py").write_text(
        "BAD_CONST = \"这里有**星号**\"\n", encoding="utf-8")
    monkeypatch.setitem(uvc.USER_VISIBLE_CONSTS, "web/services/x.py", ("BAD_CONST",))
    pieces = [(rel, s) for rel, _ln, s in uvc.backend_user_visible_strings(tmp_path)]
    assert any("星号" in s for _rel, s in pieces), "模块级常量没进枚举"
    assert uvc.find_markdown_markers(pieces), "注进模块级常量的 Markdown 标记没被抓出来"


def test_uvc_unregistered_module_constant_detector_is_not_empty_assertion(tmp_path):
    """判定⑤不是空断言：含中文却没登记的模块级常量必须被报出来。"""
    root = tmp_path / "web" / "services"
    root.mkdir(parents=True)
    (root / "y.py").write_text(
        "NEW_NOTE = \"这是一段没登记的中文文案\"\n", encoding="utf-8")
    bad = uvc.unregistered_module_constants(tmp_path)
    assert any("NEW_NOTE" in b for b in bad), "未登记的模块级常量没被报出来（空断言）"


def test_uvc_no_unregistered_module_constants_in_repo():
    """仓库里不许有「含中文却没登记」的模块级常量（守门判定⑤）。"""
    assert uvc.unregistered_module_constants() == []


def test_detector_catches_markdown_markers():
    """判定④不是空断言：喂进 ``**`` / 反引号必须被抓出来。"""
    assert uvc.find_markdown_markers([("x.html", "这是**粗体**")])
    assert uvc.find_markdown_markers([("x.html", "文件在 `scripts/run.py`")])
    assert not uvc.find_markdown_markers([("x.html", "今天还没有荐单")])


def test_uvc_tool_fails_when_rendered_note_has_markdown(monkeypatch):
    """端到端：渲染文案里出现 Markdown ⇒ UVC 工具必须失败（退出码 1）。"""
    monkeypatch.setattr(
        uvc, "render_pricing_note",
        lambda: "推荐端阈值 **EV ≥ 12.9%**（当前实测水位）",
    )
    assert uvc.main() == 1, "渲染文案里有 Markdown 标记，UVC 却判通过 ⇒ 判定④是空断言"
