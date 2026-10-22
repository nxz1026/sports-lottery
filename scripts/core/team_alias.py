"""队名归一与别名匹配（建议 3 · CLV 关联用）：**引擎侧唯一真源**。

为什么需要它：CLV 要把「预测场次」和「竞彩场次」配上，键是**中文名**。
两侧本该逐字相等（建议 1 之后 `to_cn()` 最优先查竞彩官方队名表），但仍有三类会 miss：

  ① 预测侧用了**通用叫法**（COUNTRY_CN / LLM 缓存）而竞彩侧用**官方叫法**
     —— 例：预测「曼联」vs 竞彩「曼彻斯特联」、「埃尔切」vs「埃尔沃斯堡」；
  ② 竞彩侧用**繁体/异体**（实测：「車路士」「愛華頓」「白禮頓」「紐卡素」「阿仙奴」）；
  ③ 场次本身不在模型覆盖范围内（国家队/日韩/巴甲…）⇒ 那不是名字问题，是**没预测**。

契约（与 `web/services/team_match.py` 同语义，镜像由 `tests/test_team_alias_mirror.py` 锁）：
  - `norm_name`：去空白 + 转小写，**不做**中英转换（避免过度归一）；
  - `canon_names`：本体 + 别名集合（别名表键 = 竞彩侧用词，值 = 允许的预测侧用词）；
  - `teams_match`：主客同序或反序都算同一场。

⚠️ 只收录**实测/高确定性**差异；不确定的不猜 —— 宁可该场没有 CLV，也不要错配。
错配会直接污染 CLV 均值（路线图 §6.5.3 的硬约束）。
"""
from __future__ import annotations

import re

_WS = re.compile(r"\s+")

# 键 = 竞彩侧用词，值 = 允许匹配到的预测侧用词（可多个）。
# 来源：web/services/team_match.py::\_ALIASES 的同源副本（镜像测试锁住两份一致）。
_ALIASES: dict[str, tuple[str, ...]] = {
    "曼彻斯特联": ("曼联", "曼城", "曼联1"),
    "弗洛西诺内": ("弗罗西诺内",),
    "埃尔沃斯堡": ("鄂尔士贝格",),
    "曼城": ("曼彻斯特城",),
    "阿仙奴": ("阿森纳",),
    "白禮頓": ("布莱顿",),
    "車路士": ("切尔西",),
    "愛華頓": ("埃弗顿",),
    "李斯特城": ("莱斯特城",),
    "紐卡素": ("纽卡斯尔联",),
    # ↓ 2026-10-22 新增（生产实测：这 3 个竞彩名模型**永远产不出来** ⇒ 场次被静默丢出荐单/CLV）
    #   证据：把 6 大联赛的 107 个竞彩队名与「模型侧可产出名集合」逐个对拍，
    #   只剩这 3 个对不上（官方队名表的值 / LLM 缓存 / COUNTRY_CN 都没有它们）。
    "巴伦西亚": ("瓦伦西亚",),      # 竞彩 678 = Valencia CF（ref.team 行名「瓦伦西亚」）
    "云达不来梅": ("云达不莱梅",),    # 竞彩 153 = Werder Bremen（ref.team 行名「云达不莱梅」，差一个「莱」字）
    "贝西克塔斯": ("皇家贝蒂斯",),    # 竞彩 279 = Real Betis 的**西甲**场次名；模型侧走官方队名表 ⇒ 永远叫「皇家贝蒂斯」
    # ↓ 2026-10-22 第二批（生产实测：**模型实际产出名**与理论集合对不上 ⇒ 同样是静默出局）
    #   触发方式：拿「model.pred_archive_match 里真实产出过的队名」与竞彩名对拍
    #   （第一版审计用「理论可产出集合」⇒ 漏掉了这 5 条，见路线图 §6.13.1 的订正框）。
    "巴黎圣日尔曼": ("巴黎圣日耳曼",),  # 竞彩「尔」 vs 模型「耳」
    "帕德博恩": ("帕德博恩07",),      # ESPN displayName "Paderborn 07" ⇒ 翻译残留数字后缀
    "托特纳姆热刺": ("热刺",),        # 模型侧走 COUNTRY_CN 通用叫法
    "美因茨": ("美因茨05",),          # ESPN displayName "Mainz 05" ⇒ 翻译残留数字后缀
    "赫塔费": ("赫塔菲",),            # 竞彩「费」 vs 模型「菲」
}


def norm_name(s: str) -> str:
    """归一化队名用于比较：去空白、转小写。

    不做英文/中文互转 —— 只消除确无信息量的空白与文字样本差异。
    """
    if not s:
        return ""
    return _WS.sub("", str(s)).strip().lower()


def canon_names(s: str) -> set[str]:
    """返回某队可匹配的名集（本体 + 别名）。"""
    out = {norm_name(s)}
    for alias_key, targets in _ALIASES.items():
        if norm_name(s) == norm_name(alias_key):
            out.update(norm_name(t) for t in targets)
    return out


def teams_match(h1: str, a1: str, h2: str, a2: str) -> bool:
    """判断 (h1,a1) 与 (h2,a2) 是否同一场（主客可互换但须是同一场）。"""
    s1h, s1a = canon_names(h1), canon_names(a1)
    s2h, s2a = canon_names(h2), canon_names(a2)
    if s1h & s2h and s1a & s2a:
        return True
    if s1h & s2a and s1a & s2h:
        return True
    return False


def alias_table() -> dict[str, tuple[str, ...]]:
    """别名表只读副本（供镜像测试比对）。"""
    return dict(_ALIASES)
