"""web.services.haf — 半全场九宫格薄包装（保留旧 import 路径）。

实际算法在 ``scripts/derive/haf.py``（P2-12 迁移）。本文件保留是因为
``web/routers/haf.py`` 和 ``web/services/team_match.py`` 历史 import 路径
``from web.services.haf import compute_haf``。

P2-25 常量同步：``HAF_ORDER`` / ``HT_RATIO`` 改为从 ``scripts/derive.haf``
re-export（单一真源），不再硬编码副本。加新维度 / 调比例 → scripts
侧一处改即生效。

模块加载时把仓库根 / scripts/ 加进 sys.path，让 ``from derive.haf ...``
可解析（其他 web 测试 fixture 也用同模式，见 tests/test_team_match.py）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让 from derive.haf import ... 在 web import 时可解析（仅一次）
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
for _p in (str(_REPO_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 常量 re-export（单一真源在 scripts/derive/haf.py）
from derive.haf import HAF_ORDER, HT_RATIO  # noqa: F401 — 兼容历史 import 路径


def _pois(lmb, max_g):  # noqa: ANN001 — 兼容历史调用方签名
    """兼容占位：实际算法在 scripts/derive/haf._pois。本函数不会真被调
    （所有内部消费方已改走 scripts/derive/haf），仅留作类型签名占位，
    防历史代码 ``from web.services.haf import _pois`` 直接调用时崩。
    """
    raise NotImplementedError(
        "_pois 已迁移到 scripts/derive/haf._pois，请改 import 路径"
    )


def compute_haf(lambda_home, lambda_away, line=0, max_goals: int = 10) -> dict | None:
    """半全场九宫格概率。λ 或 line 非法 → None。

    内部委托到 ``scripts/derive/haf.probs``（同签名）。
    """
    from derive.haf import probs as _probs_impl
    return _probs_impl(lambda_home, lambda_away, line=line, max_goals=max_goals)
