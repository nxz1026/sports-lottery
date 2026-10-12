"""P2-12 镜像对账：web/services/haf.compute_haf ↔ scripts/derive/haf.probs。

P2-12 迁移后 web 端是薄包装（保留旧 import 路径），实际算法权威源
在 scripts/derive/haf.probs。本测试断言两版本在固定 (λh, λa, line, max_goals)
输入下逐字段相等，确保后续若算法在 scripts 侧改，CI 即刻捕获 web 端漂移。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


_FIXTURES = [
    # (label, lambda_home, lambda_away, line, max_goals)
    ("典型主胜 -1", 1.5, 1.2, -1, 10),
    ("平手 0", 1.3, 1.3, 0, 10),
    ("客让 +1", 0.9, 1.6, 1, 10),
    ("强主队 -2", 2.2, 0.8, -2, 10),
    ("低 λ", 0.3, 0.4, -1, 10),
    ("max_goals=6 截断", 1.5, 1.2, -1, 6),
]


def test_compute_haf_matches_scripts_derive_probs():
    """web.compute_haf 输出 ≡ scripts.derive.haf.probs 输出。"""
    from web.services.haf import compute_haf
    from derive.haf import probs as scripts_probs

    for label, lh, la, line, mg in _FIXTURES:
        web = compute_haf(lh, la, line=line, max_goals=mg)
        scripts = scripts_probs(lh, la, line=line, max_goals=mg)
        assert web == scripts, f"{label}: web={web} scripts={scripts}"


def test_compute_haf_uses_scripts_authoritative_constants():
    """HT_RATIO 与 HAF_ORDER 在 web / scripts 端必须一致（任何一边改都该同步）。"""
    from web.services import haf as web_haf
    from derive import haf as scripts_haf

    assert web_haf.HT_RATIO == scripts_haf.HT_RATIO
    assert web_haf.HAF_ORDER == scripts_haf.HAF_ORDER


def test_compute_haf_invalid_inputs_match():
    """非法 λ → web / scripts 都返 None（同步语义）。"""
    from web.services.haf import compute_haf
    from derive.haf import probs as scripts_probs

    invalid = [(None, 1.0), (1.0, None), (0.0, 1.0), (-1.0, 1.0),
               ("x", 1.0), (1.0, "x")]
    for lh, la in invalid:
        assert compute_haf(lh, la) is None
        assert scripts_probs(lh, la) is None


def test_compute_haf_zero_line_matches_no_line():
    """line=0 与省略 line 在 web / scripts 两端都等价。"""
    from web.services.haf import compute_haf
    from derive.haf import probs as scripts_probs

    lh, la = 1.5, 1.2
    assert compute_haf(lh, la, line=0) == compute_haf(lh, la)
    assert scripts_probs(lh, la, line=0) == scripts_probs(lh, la)
    # 跨实现一致
    assert compute_haf(lh, la, line=0) == scripts_probs(lh, la, line=0)
