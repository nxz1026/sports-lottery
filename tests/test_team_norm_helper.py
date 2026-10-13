"""``core.team_norm.strip_accents_and_lowercase`` 守门 — P5-3 抽出的共用 helper。

P4-5 mirror 测试（``tests/test_normalize_team_mirror.py``）已验证 align 与
odds_enrich 两端在真实队名上输出字节等价。本测试直接覆盖 helper 自身的
边界：None / 空串 / 纯重音 / CJK / emoji / 数字 / bool / 已全 ASCII。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.team_norm import strip_accents_and_lowercase  # noqa: E402


@pytest.mark.parametrize("name,expected", [
    # 真实队名（与 test_normalize_team_mirror 共用 fixture 验证一致性）
    ("VfL Bochum", "vfl bochum"),
    ("Bayern München", "bayern munchen"),
    ("Atlético de Madrid", "atletico de madrid"),
    ("Deportivo de La Coruña", "deportivo de la coruna"),
    # 全部 ASCII
    ("Real Madrid", "real madrid"),
    # 仅重音
    ("áéíóú", "aeiou"),
    ("Café", "cafe"),
    # 数字
    ("VfL 10", "vfl 10"),
    # CJK 保留（下游按 [^a-z0-9]+ 切分时派空 tokens）
    ("皇家马德里", "皇家马德里"),
    # 上下 case
    ("UPPERCASE", "uppercase"),
    ("MiXeD", "mixed"),
    # 跨多字节字符（Ł 是单一字符非组合重音，NFKD 不分解；ó 仍正常分解为 o+重音
    # 剥重音得 o；ź NFKD 分解为 z+◌́ 组合重音，剥后得 z。Łódź → łodz）
    ("Łódź", "łodz"),
    ("Malmö FF", "malmo ff"),
])
def test_strip_accents_and_lowercase(name, expected):
    assert strip_accents_and_lowercase(name) == expected


@pytest.mark.parametrize("empty", [None, "", 0, False])
def test_empty_inputs_return_empty_string(empty):
    """``str(name or "")`` 兜 None / 空 / 0 / False → 空串。"""
    assert strip_accents_and_lowercase(empty) == ""


def test_idempotent():
    """连续两次 strip 应当幂等（已无重音可剥）。"""
    once = strip_accents_and_lowercase("Café")
    twice = strip_accents_and_lowercase(once)
    assert once == twice == "cafe"


def test_does_not_split_on_whitespace():
    """helper 不切词、不去标点 —— 下游（re.split/re.sub）负责。"""
    got = strip_accents_and_lowercase("Bayern München  e.V.")
    assert " " in got  # 空白保留
    assert "." in got  # 句点保留
