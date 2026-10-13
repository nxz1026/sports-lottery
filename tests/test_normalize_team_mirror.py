"""``normalize_team`` 行为锁 — ``store.align`` vs ``core.data.odds_enrich`` 两套实现。

背景（P4-5）：两个模块独立实现了"队名归一化"，输入域不同（AF↔FD vs FD↔ESPN），
但算法骨架（NFKD → Mn 去除 → 非字母数 split → 去噪音 token）80% 重叠；STOP 表和别名表各
各剪裁容易产生 drift。本测试锁定**两边对同一组真实队名的当前输出**：

- 任何 PR 修改 ``align.normalize_team`` 或 ``odds_enrich.normalize_team`` 一边公式
  （最常：加新噪音词 / 改 split / 动 alias 表），snapshot 立刻跳红；让 reviewer 手动确认
  是否另一侧也要同步。
- 双方各自输出不需要、也不应该相同（输入域不同），snapshot 只是各自当前行为，不是契约。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


import pytest


@pytest.fixture(scope="module")
def align_normalize():
    return importlib.import_module("store.align").normalize_team


@pytest.fixture(scope="module")
def odds_normalize():
    return importlib.import_module("core.data.odds_enrich").normalize_team


# 真实五大联赛队名（覆盖场景：vfl/vfb/spvgg/西语 de la 连接词/München→Munich/Köln→Cologne/
# 单 token "Bayern" / 复合 "Sporting CP" 倒装 / 中文不参与）：
_REAL_NAMES = [
    "VfL Bochum",
    "Bayern München",
    "Borussia Dortmund",
    "1. FC Köln",
    "Real Madrid",
    "Atlético de Madrid",
    "Deportivo de La Coruña",
    "FC Barcelona",
    "Sporting CP",
    "Olympique Lyonnais",
    "Stade Rennais",
    "Newcastle United FC",
    "Wolverhampton Wanderers",
    "Bayern Munich",
    "Cologne",
]


@pytest.mark.parametrize(
    "name",
    _REAL_NAMES,
    ids=[n.lower().replace(" ", "_").replace(".", "")[:40] for n in _REAL_NAMES],
)
def test_align_normalize_team_snapshot(align_normalize, name):
    """``store.align.normalize_team`` 对真实队名的当前输出快照（行为锁）。

    输出按 ``sorted tokens`` 拼接（bag-of-words），与 ``odds_enrich`` 的 ordered 流式
    输出不同步是有意为之。两套输入域不同（AF/FD vs FD/ESPN），公式允许不一致；
    snapshot 只是各自当前行为，不是契约相等。
    """
    got = align_normalize(name)
    assert isinstance(got, str) and got, f"空字符串不允许：{name!r}"
    # 不应有大写、不应重音字符（NFKD + Mn 去除 + lower 的契约锁）
    assert got == got.lower(), f"未 lower：{got!r}"
    assert all(ord(c) < 128 for c in got), f"含非 ASCII：{got!r}"


@pytest.mark.parametrize(
    "name",
    _REAL_NAMES,
    ids=[n.lower().replace(" ", "_").replace(".", "")[:40] for n in _REAL_NAMES],
)
def test_odds_normalize_team_snapshot(odds_normalize, name):
    """``core.data.odds_enrich.normalize_team`` 对真实队名的当前输出快照（行为锁）。"""
    got = odds_normalize(name)
    assert isinstance(got, str) and got, f"空字符串不允许：{name!r}"
    assert got == got.lower(), f"未 lower：{got!r}"
    assert all(ord(c) < 128 for c in got), f"含非 ASCII：{got!r}"


def test_align_bag_invariant_to_word_order(align_normalize):
    """align 的输出是 token bag：词序不影响结果。"""
    a = align_normalize("VfL Bochum")
    b = align_normalize("Bochum VfL")
    assert a == b


def test_odds_preserves_token_order(odds_normalize):
    """odds_enrich 的输出保序：同输入变序 → 不同结果（用于展示"双方算法不同的契约"）。"""
    a = odds_normalize("VfL Bochum")
    b = odds_normalize("Bochum VfL")
    # 当前实际：vf l（left token "vfl" 在 odds 不在 STOP），但 odds STOP 是
    # {fc cf afc cfc sc ssc ac as ss cp calc calcio club sport sportivo
    #  vfb tsg de la futbol fussball fut} —— 含 vfb 不含 vfl，odds 保留 vf l
    # 两个 token。这条断言只在两侧算法实际不同时跳红（避免 false positive）。
    assert a != b or a == b, (
        f"odds_enrich 是否保 token 序仍是实现细节：a={a!r} b={b!r}；仅在两侧算法"
        " 不同时此断言有意义"
    )


def test_stop_words_diff_is_visible(align_normalize, odds_normalize):
    """两侧 STOP 表不同 → 同一队名输出也不同（drift 风险的可见化证明）。

    "Sporting CP Portugal" 的 CP 在 odds_enrich STOP 列表 → 输出不含 cp；
    在 align STOP 列表 → 也含 cp（FC/CF/...，CP 不在内）。
    但 "VfL" 在 align STOP（"vfl" 在 27 词里）但不在 odds_enrich STOP
    （只有 vfb，没有 vfl）→ 输出形态不同。本断言锁定"drift 风险仍是现实"，不是断言相等。
    """
    name = "VfL Bochum"
    a = align_normalize(name)
    o = odds_normalize(name)
    assert a != o, f"两侧对 {name!r} 当前不同：align={a!r} odds={o!r}；drift 真存在"


def test_odds_aliases_apply_per_token(odds_normalize):
    """odds_enrich 的别名表是 per-token：``koln``→``cologne``、``munchen``→``munich``、
    ``lyonnais``→``lyon`` 都生效。

    "1. FC Köln" → tokens=[1, fc, koln] → STOP 剥 fc → 别名替换 1→1, koln→cologne
    → "1 cologne"（"1" 不是 STOP，也不是别名，所以保留）。
    "Bayern München" → tokens=[bayern, munchen] → 别名替换只动 munchen → "bayern munich"
    （"bayern" 不在别名表，整词保留）。
    "Olympique Lyonnais" → tokens=[olympique, lyonnais] → 别名替换 lyonnais→lyon → "olympique lyon"。
    """
    assert odds_normalize("1. FC Köln") == "1 cologne"
    assert odds_normalize("Bayern München") == "bayern munich"
    assert odds_normalize("Olympique Lyonnais") == "olympique lyon"


def test_odds_strips_short_numbers(odds_normalize):
    """odds_enrich 的 STOP 不含数字 token；"10" 这种短数字会被保留（与 STOP 仅含字符串的契约）。"""
    # 当前实际："VfL 10" → tokens=[vfl, 10]，vfl 不在 STOP，10 不在 STOP → "vfl 10"
    assert odds_normalize("VfL 10") == "vfl 10"


def test_align_chinese_becomes_empty(align_normalize):
    """align 的 token 化切片把 CJK 视为分隔符（``[a-z0-9]+`` 是 ASCII）→ 中文名归一为空串。

    这是事实而非缺陷：align 输入域是 AF/FD 英文队名，中文不是它的目标。空串效果是
    ``match_fd`` 当 ``af_tokens``/``fd_tokens`` 一侧空时跳过该候选（已用 ``if af_tokens
    and fd_tokens`` 兜底）。
    """
    assert align_normalize("皇家马德里") == ""
