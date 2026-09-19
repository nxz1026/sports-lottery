"""web.services.team_match — 竞彩盘口场次 ↔ 模型预测场次的队名匹配与合并。

纯函数模块（无 IO，便于单测）。每日一图的后端合并逻辑：

- 输入：竞彩盘口在售场次行（had/hhad 玩法，来自 store.jc_view.fixtures_on）+ 各联赛预测
  场次（来自 store.latest_by_league()["<lg>"]["data"]["predictions"]）。
- 输出：为每场在售盘口（had）合成的"每日一图"行：
    odd 层（官方赔率倾向） + 算法层（模型 direction/星级/波胆）。
- 队名匹配策略：去掉空格/大小写归一后，按主客精确匹配；再套中文别名表
  （盘口用词 ↔ 预测用词，如 曼彻斯特联↔曼联、埃尔沃斯堡↔鄂尔士贝格）。

铁律：本条不改任何数据，只做只读合并；匹配不到的场次不拼接、不臆造预测。
"""

from __future__ import annotations

import re

# --- 队名规范化 -----------------------------------------------------------
_WS = re.compile(r"\s+")


def norm_name(s: str) -> str:
    """归一化队名用于比较：去空白、去常见后缀标记、转小写。

    不做过度的英文/中文转换——只消除确无信息量的空白与文字样本差异。
    """
    if not s:
        return ""
    return _WS.sub("", str(s)).strip().lower()


# --- 中文别名表：竞彩盘口用词 → 模型预测常用词 ---------------------------
# 键 = 盘口侧队名，值 = 允许匹配到的预测侧队名（可多个）。
# 仅收录实测 / 高确定性差异；不确定的不猜（宁可该场不出每日图）。
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
}


def _canon(s: str) -> set[str]:
    """返回某队的可匹配名集（本体 + 别名）。"""
    out = {norm_name(s)}
    for alias_key, targets in _ALIASES.items():
        if norm_name(s) == norm_name(alias_key):
            out.update(norm_name(t) for t in targets)
    return out


def teams_match(h1: str, a1: str, h2: str, a2: str) -> bool:
    """判断 (h1,a1) 与 (h2,a2) 是否同一场（主客可互换但同一场）。

    竞彩盘口与预测的主客顺序理论上应一致；放宽容许同场反序以便健壮。
    """
    S1h, S1a = _canon(h1), _canon(a1)
    S2h, S2a = _canon(h2), _canon(a2)
    if S1h & S2h and S1a & S2a:
        return True
    if S1h & S2a and S1a & S2h:
        return True
    return False


# --- odd 层：官方赔率倾向 ------------------------------------------------
# had 玩法 options 键：h=主胜 d=平 a=客胜（字符串赔率，如 "3.05"）。
def odd_suggestion(had_options: dict) -> dict:
    """由官方 had 赔率指向赔率最低项（隐含概率最高）→ odd 层倾向。

    返回 {"pick": "主胜|平|客胜", "odds": "<最低赔率>"}；无有效赔率返回空。
    """
    try:
        vals = {
            "主胜": float(had_options.get("h") or 0),
            "平": float(had_options.get("d") or 0),
            "客胜": float(had_options.get("a") or 0),
        }
    except (TypeError, ValueError):
        return {}
    vals = {k: v for k, v in vals.items() if v > 0}
    if not vals:
        return {}
    pick = min(vals, key=vals.get)
    # 用十进制格式避免二进制浮点四舍五入误差（round(2.03,2)=='2.02' 的问题）
    formatted = f"{vals[pick]:.2f}"
    return {"pick": pick, "odds": formatted}


# --- 算法层：模型预测摘取 ------------------------------------------------
def algo_suggestion(pred: dict) -> dict:
    """从预测条目抽算法层关键信息。

    direction 是"主队 胜(接近)"或"客队 胜"；stars 如 "2-star"。
    提取：主/客倾向 + 星级 + 波胆比分（predicted_score 如 "1-0"）。
    """
    direction = str(pred.get("direction") or "")
    stars = str(pred.get("stars") or "")
    n_star = 0
    m = re.search(r"(\d+)-star", stars)
    if m:
        n_star = int(m.group(1))
    home = str(pred.get("home") or "")
    # direction 形如 "博洛尼亚 胜(接近)" / "热刺 胜" / "卡利亚里 胜"。判断倾向：
    # 含"平"→平；否则看 direction 前缀是否指向主队名。
    if "平" in direction:
        side = "平"
    elif home and home in direction.split("(")[0]:
        side = "主胜"
    else:
        side = "客胜"
    return {
        "pick": side,
        "stars": n_star,
        "score": str(pred.get("predicted_score") or ""),
        "direction": direction,
    }


# --- 主合并 --------------------------------------------------------------
def build_rows(fixture_rows: list[dict], pred_by_league: dict[str, list[dict]]) -> list[dict]:
    """合并竞彩盘口在售场次与预测 → 每日一图行。

    参数：
      fixture_rows: store.jc_view.fixtures_on() 的原始行（含 had/hhad/crs/... 多玩法）。
      pred_by_league: {league: [prediction_entry,...]}（store.latest_by_league data.predictions）。

    返回行（每场只一条，had 为锚，缺 had 用 hhad 兜底）：
      {match_num, league_cn, home_cn, away_cn, kickoff_bj, had, hhad,
       odd:{...}, algo:{...}, matched:bool}
    """
    # 按 match_num 聚合所有玩法
    by_match: dict = {}
    for r in fixture_rows:
        mn = r.get("match_num")
        if mn is None:
            continue
        m = by_match.setdefault(mn, {
            "match_num": mn,
            "league_cn": r.get("league_cn"),
            "home_cn": r.get("home_cn"),
            "away_cn": r.get("away_cn"),
            "kickoff_bj": r.get("kickoff_bj"),
            "plays": {},
        })
        m["plays"][r.get("play_type")] = r.get("options") or {}

    # 扁平建预测索引 (home,away)->entry
    pred_index: list[tuple[dict, dict]] = []  # (entry, league)
    for league, entries in pred_by_league.items():
        for e in entries:
            pred_index.append((e, league))

    rows = []
    for mn, m in by_match.items():
        plays = m["plays"]
        had = plays.get("had") or plays.get("hhad") or {}
        home, away = m["home_cn"], m["away_cn"]
        # 匹配预测
        best = None
        for e, lg in pred_index:
            if teams_match(home or "", away or "", e.get("home") or "", e.get("away") or ""):
                best = e
                break
        row = {
            "match_num": mn,
            "league_cn": m["league_cn"],
            "home_cn": home,
            "away_cn": away,
            "kickoff_bj": m.get("kickoff_bj"),
            "had": had,
            "hhad": plays.get("hhad") or {},
            "matched": best is not None,
        }
        if best is not None:
            row["algo"] = algo_suggestion(best)
        row["odd"] = odd_suggestion(had)
        rows.append(row)
    return rows