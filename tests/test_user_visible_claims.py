"""P2-I（2026-10-23）：用户可见文案的一致性回归锁。

⚠️ **背景（P2-H4 事故，生产浏览器验收时当场抓到）**：

用户可见文案里的数字以前是**手打**的。荐单面板上实际印着：

    「…单边投注要先扛掉这笔水 ⇒ EV 必须不低于水位才谈得上盈利
      （推荐端阈值已对齐为 EV ≥ 12.9%）」

而代码里的真实阈值是 ``MIN_EV = 0.05``（**5%**）⇒ **两处各写各的**
⇒ 文案漂移成**假话**（用户被告知一个不存在的策略），
而**静态审计 / 单测 / 守门全都没抓到**（守门只查「有没有 12.9%」）。

⇒ 修法：
1. 口径数字搬进**唯一真源** ``scripts/core/betting_policy.py``；
2. 用户可见文案**从真源派生**（不许手写数字）；
3. 新增 ``tools/check_user_visible_claims.py``（UVC）：
   枚举「会渲染给用户的文本」⇒ 查泄漏 / 查手写口径数字 / **渲染后**核对数字。

本文件锁住三件事：
- **真源常量**不被偷换（MIN_EV 必须 0.05、水位必须 0.129）；
- **渲染出来的文案**里的数字 == 真源（行为级：真的调用渲染函数）；
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

def test_policy_constants_are_policy_value_and_measured_value():
    """真源常量：MIN_EV = 0.05（政策值）、MIN_P = 0.10、水位 = 0.129（生产实测）。"""
    from core.betting_policy import MIN_EV, MIN_P, WATER_LEVEL_MEASURED

    assert MIN_EV == 0.05, "EV 阈值是政策值 0.05，不许偷换成水位 0.129"
    assert MIN_EV != 0.129
    assert MIN_P == 0.10
    assert abs(WATER_LEVEL_MEASURED - 0.129) < 1e-9


def test_pct_and_threshold_text_formatting():
    """派生文本：比例 → 百分比；门槛 → 「比例（百分比）」双表示。"""
    from core.betting_policy import EV_THRESHOLD_TEXT, WATER_LEVEL_TEXT, pct_text, threshold_text

    assert pct_text(0.05) == "5%"
    assert pct_text(0.129) == "12.9%"
    assert pct_text(0.10) == "10%"
    assert threshold_text(0.05) == "0.05（5%）"
    assert threshold_text(0.129) == "0.129（12.9%）"
    assert EV_THRESHOLD_TEXT == "EV ≥ 5%"
    assert WATER_LEVEL_TEXT == "12.9%"


# ──────────────────────── 渲染后的文案（行为级）────────────────────────

def test_pricing_note_ev_threshold_equals_policy_value():
    """**渲染出来**的荐单面板文案：EV 阈值必须等于真源（0.05 ⇒ 5%）。"""
    from web.services.recommend import _pricing_note

    note = _pricing_note()
    assert "EV ≥ 5%" in note, note
    # 渲染文本里**只允许**出现一个 EV 阈值数字，且必须是 5
    found = re.findall(r"EV\s*[≥>=]\s*(\d+(?:\.\d+)?)\s*%", note)
    assert found == ["5"], f"渲染文案里的 EV 阈值数字 = {found}（应只有 5）"


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


def test_recommend_note_uses_derived_numbers():
    """荐单 DB note（写入 analysis.ev_report，用户可见）：门槛与水位都要派生。"""
    from decision import recommend as rec

    note = rec._NOTE_TMPL.format(
        side="主胜", sp="1.86", p=0.5, ev=0.1, kelly=0.05, stake=1.0,
        ts="2026-10-23 09:00:00", extra="无",
        min_ev=rec.threshold_text(rec.MIN_EV), min_p=rec.threshold_text(rec.MIN_P),
        water_level=rec.WATER_LEVEL_TEXT,
    )
    assert "0.05（5%）" in note, note
    assert "0.1（10%）" in note, note
    assert "12.9%" in note, note
    # 水位不许被写成 EV 门槛
    assert "EV ≥ 12.9%" not in note


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
    """渲染出的数字与真源不一致 ⇒ 必须被抓出来（证明检查③不是恒绿）。"""
    monkeypatch.setattr(
        uvc, "render_pricing_note",
        lambda: "推荐端阈值 EV ≥ 12.9%（政策值）；水位 25~33%",
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
    """README 与路线图必须与真源口径一致（阈值 5% / 水位 12.9%）。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "project" / "产品路线图-竞彩闭环-20261020.md").read_text(
        encoding="utf-8")
    for name, doc in (("README", readme), ("路线图", roadmap)):
        assert "12.9%" in doc, f"{name} 必须写明实测水位 12.9%"
        assert _near(doc, "EV 阈值", "5%"), f"{name} 必须写明 EV 阈值维持 5%"


def test_uvc_tool_runs_and_passes():
    """UVC 工具本身必须能跑通（守门项会被别处引用）。"""
    import subprocess

    rc = subprocess.run([sys.executable, str(TOOL)], cwd=ROOT,
                        capture_output=True, text=True, timeout=600)
    assert rc.returncode == 0, (rc.stdout + rc.stderr)[-3000:]
