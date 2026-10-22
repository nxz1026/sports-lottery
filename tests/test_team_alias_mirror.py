"""别名表**双侧镜像** + 2026-10-22 新增 8 条的契约测试。

背景（2026-10-22 生产实测）：
  ① ``scripts/core/team_alias.py`` 的 docstring 写着「镜像由 tests/test_team_alias_mirror.py 锁」
     —— **那个文件当时根本不存在** ⇒ 这句话是假的（项目纪律：文档不许说假话）。
     本文件把它变成真的：引擎侧 ``_ALIASES`` 与 web 侧 ``_ALIASES`` **必须逐条相同**。
  ② 6 大联赛的 107 个竞彩队名与「模型侧可产出名集合」逐个对拍后，只剩 3 个对不上
     ⇒ 那 3 支队对应的场次**被静默丢出荐单/CLV**（生产实测 10 场，含今天与未来场次）：
       - 巴伦西亚（竞彩 678） vs 模型侧「瓦伦西亚」
       - 云达不来梅（竞彩 153） vs 模型侧「云达不莱梅」
       - 贝西克塔斯（竞彩 279，Real Betis 的西甲场次名） vs 模型侧「皇家贝蒂斯」
  ③ **第一批审计脚本自己也漏了 5 条**（口径是「理论可产出集合」，不是「实际产出名」）：
       - 帕德博恩（竞彩） vs 模型侧「帕德博恩07」（ESPN displayName 带数字后缀）
       - 美因茨（竞彩） vs 模型侧「美因茨05」
       - 巴黎圣日尔曼（竞彩） vs 模型侧「巴黎圣日耳曼」
       - 托特纳姆热刺（竞彩） vs 模型侧「热刺」
       - 赫塔费（竞彩） vs 模型侧「赫塔菲」
     ⇒ **「理论能产出」≠「实际会产出」** —— 审计必须对着真实产出说话。

纪律：别名表只收**实测/高确定性**差异（``core/team_alias.py`` 的契约）——
宁可该场没有 CLV，也不要错配 ⇒ 本文件同时锁「能匹配」与「不误配」两侧。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.team_alias import alias_table, canon_names, teams_match  # noqa: E402


def _load_web_team_match():
    """spec 加载 web/services/team_match.py（web 侧独立副本，不 import scripts/）。"""
    spec = importlib.util.spec_from_file_location(
        "team_match_mirror_test", REPO_ROOT / "web" / "services" / "team_match.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# 2026-10-22 新增的 8 条：竞彩侧用词 → 允许匹配到的预测侧用词
#   第一批 3 条：对「理论可产出名集合」对拍后剩下的（巴伦西亚 / 云达不来梅 / 贝西克塔斯）
#   第二批 5 条：对「模型**实际产出过**的队名」对拍后剩下的
#     （帕德博恩07 / 美因茨05 / 巴黎圣日耳曼 / 热刺 / 赫塔菲）
NEW_ALIASES: dict[str, tuple[str, ...]] = {
    "巴伦西亚": ("瓦伦西亚",),
    "云达不来梅": ("云达不莱梅",),
    "贝西克塔斯": ("皇家贝蒂斯",),
    "巴黎圣日尔曼": ("巴黎圣日耳曼",),
    "帕德博恩": ("帕德博恩07",),
    "托特纳姆热刺": ("热刺",),
    "美因茨": ("美因茨05",),
    "赫塔费": ("赫塔菲",),
}


# ── 镜像 ────────────────────────────────────────────────────────────────

def test_engine_and_web_alias_tables_are_identical():
    """两侧别名表**逐条相同**（一边改了另一边没改 ⇒ join 行为分叉）。"""
    web = _load_web_team_match()
    assert alias_table() == dict(web._ALIASES), (
        "core/team_alias.py 与 web/services/team_match.py 的别名表不一致："
        f"仅引擎侧={set(alias_table()) - set(web._ALIASES)} "
        f"仅 web 侧={set(web._ALIASES) - set(alias_table())}"
    )


def test_engine_and_web_norm_and_canon_agree_on_new_aliases():
    """两侧对新增 3 条的 canon 展开必须一致（否则 web 层仍 join miss）。"""
    web = _load_web_team_match()
    for jc_name, pred_names in NEW_ALIASES.items():
        assert canon_names(jc_name) == web._canon(jc_name), jc_name
        for pred in pred_names:
            assert pred in canon_names(jc_name)
            assert pred in web._canon(jc_name)


# ── 新增 3 条的行为契约 ─────────────────────────────────────────────────

def test_new_aliases_present_in_both_tables():
    web = _load_web_team_match()
    for jc_name, pred_names in NEW_ALIASES.items():
        assert alias_table().get(jc_name) == pred_names
        assert web._ALIASES.get(jc_name) == pred_names


def test_new_aliases_make_teams_match_succeed():
    """竞彩名 ↔ 模型名 现在能判为同一场（这是这 3 条存在的全部理由）。"""
    for jc_name, pred_names in NEW_ALIASES.items():
        pred = pred_names[0]
        assert teams_match(jc_name, "埃弗顿", pred, "埃弗顿"), jc_name
        # 主客反序也算同一场（引擎契约）
        assert teams_match("埃弗顿", jc_name, "埃弗顿", pred), jc_name


def test_new_aliases_do_not_create_false_matches():
    """加了别名**不许**把不同队判成同一场（主客两侧都要对得上）。"""
    # 巴伦西亚 vs 瓦伦西亚 成立，但**另一侧**不同 ⇒ 不成立
    assert not teams_match("巴伦西亚", "曼彻斯特城", "瓦伦西亚", "埃弗顿")
    # 贝西克塔斯（Betis 西甲名）不该跟「皇家贝蒂斯」以外的队混
    assert not teams_match("贝西克塔斯", "马德里竞技", "皇家贝蒂斯", "巴伦西亚")
    # 云达不来梅 vs 云达不莱梅 成立，但客队不同则不成立
    assert not teams_match("云达不来梅", "帕德博恩", "云达不莱梅", "霍芬海姆")


def test_new_aliases_do_not_merge_two_different_teams():
    """同名的两个队不该互相匹配：巴伦西亚 与 瓦伦西亚 是同一队（成立），
    但「贝西克塔斯」与「毕尔巴鄂竞技」是**两支不同的队**（必须不成立）。"""
    assert not teams_match("贝西克塔斯", "水晶宫", "毕尔巴鄂竞技", "水晶宫")
    assert not teams_match("云达不来梅", "奥格斯堡", "帕德博恩", "奥格斯堡")


def test_second_batch_aliases_do_not_create_false_matches():
    """第二批 5 条同样不许把不同队混成同一场（主客两侧都要对得上）。"""
    # 帕德博恩 ↔ 帕德博恩07 成立，但另一侧不同 ⇒ 不成立
    assert not teams_match("帕德博恩", "云达不莱梅", "帕德博恩07", "霍芬海姆")
    # 美因茨 ↔ 美因茨05 成立，但另一侧不同 ⇒ 不成立
    assert not teams_match("美因茨", "多特蒙德", "美因茨05", "弗赖堡")
    # 托特纳姆热刺 ↔ 热刺 成立，但另一侧不同 ⇒ 不成立
    assert not teams_match("托特纳姆热刺", "阿森纳", "热刺", "切尔西")
    # 赫塔费 ↔ 赫塔菲 成立，但另一侧不同 ⇒ 不成立
    assert not teams_match("赫塔费", "塞维利亚", "赫塔菲", "比利亚雷亚尔")
    # 巴黎圣日尔曼 ↔ 巴黎圣日耳曼 成立，但另一侧不同 ⇒ 不成立
    assert not teams_match("巴黎圣日尔曼", "马赛", "巴黎圣日耳曼", "里昂")


# ── 官方队名表不许被「改对」 ────────────────────────────────────────────

def test_official_name_table_keeps_real_betis_chinese_name():
    """官方队名表里 Real Betis 的值必须仍是「皇家贝蒂斯」。

    同一俱乐部在竞彩有两个 id + 两个中文名（505 皇家贝蒂斯 / 279 贝西克塔斯）；
    模型侧走官方队名表 ⇒ 永远是「皇家贝蒂斯」⇒ 另一侧靠别名表桥接。
    **把本表的值改成「贝西克塔斯」会让 6 场欧战场次 join miss。**
    """
    import json
    data = json.loads((REPO_ROOT / "scripts/references/team_cn_official.json").read_text(encoding="utf-8"))
    entries = {k: v for k, v in data.items() if not k.startswith("_")}
    assert entries.get("Real Betis") == "皇家贝蒂斯"
    assert entries.get("Real Betis Balompié") == "皇家贝蒂斯"
    # 「贝西克塔斯」是竞彩侧用词，**不属于**本表的值（它靠别名表桥接）
    assert "贝西克塔斯" not in set(entries.values())
