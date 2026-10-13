"""跨源队名归一化 — 共用 helper（P5-3 抽出）。

``store.align.normalize_team``（AF↔FD 对齐）与 ``core.data.odds_enrich.normalize_team``
（FD↔ESPN 对齐）两套实现骨架 80% 重叠，本模块只抽**字符级预处理**这一 100%
等价的 3 行：NFKD 分解 + 去除组合重音（Mn 类别）+ lowercase。

STOP 表 / 别名表 / token 切分 / 排序 等下游动作各模块自留 —— P4-5 行为锁
已就位，未来谁动 STOP / 别名，PR review 时由两边 mirror 测试对照即可定位。
"""
from __future__ import annotations

import unicodedata


def strip_accents_and_lowercase(name) -> str:
    """NFKD 分解 → 删 Mn（组合重音）→ lowercase 后的"字面归一串"。

    输入域为字符串（``str(name or "")`` 自动容错 None/空）。返回串仍可能含
    非字母数字字符（如 CJK、连字符等），下游负责二次切分与 STOP 词过滤。
    """
    flat = "".join(c for c in unicodedata.normalize("NFKD", str(name or ""))
                   if not unicodedata.combining(c))
    return flat.lower()
