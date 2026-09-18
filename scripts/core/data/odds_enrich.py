"""ESPN 赔率富化（核查 P0-2 修复）：football-data 源不带赔率 → 全部预测
market.status=missing，今日推荐 KPI 与串关组合赔率恒为「—」。

ESPN scoreboard（site.api.espn.com，无 key 无配额）对五大联赛提供完整
1x2 收盘赔率（moneyline.home/draw/away.close）+ 让球 + 大小球。本模块
按「归一化队名 + 开球日」把 ESPN 赔率回填进 football-data 预测行：

- 显示/信号字段：odds_data_available、home/draw/away_true_prob（三向去水）、
  spread_home_line/close、total_over/under_close、ml_home_close、draw_ml；
- 结构化 market 字典（web 层 _market_projection 的引擎侧产物）：
  source/captured_at/market_type/odds_format/selections.decimal_odds。

ESPN 抓取失败非致命：原样返回 future（保持 football-data 基本面预测），
只记 warning，绝不中断预测流水线。
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from core.data.parse import remove_vig
from core.log import logger


def american_to_decimal(raw) -> float | None:
    """美式赔率串 → 十进制（+160→2.60，-145→1.69）；非法 → None。"""
    s = str(raw or "").strip().lstrip("+")
    if not s or s in {"-", "+"}:
        return None
    try:
        v = int(s)
    except ValueError:
        return None
    if v == 0:
        return None
    if v > 0:
        return round(1 + v / 100, 2)
    return round(1 + 100 / abs(v), 2)


# 五大联赛（英/西/德/意/法）队名惯用前后缀：归一化时去掉，
# 让 "Brentford FC"（football-data）≈ "Brentford"（ESPN displayName）。
# 含西语连接词 de/la（Atlético de Madrid、Deportivo de La Coruña）
# 与西语名词尾 -futbol（Real Sociedad de Fútbol），两侧同步剥离后才可比。
_TEAM_NOISE_TOKENS = {
    "fc", "cf", "afc", "cfc", "sc", "ssc", "ac", "as", "ss", "cp",
    "calc", "calcio", "club", "sport", "sportivo", "vfb", "tsg",
    "de", "la", "futbol", "fussball", "fut",
}


def normalize_team(name: str) -> str:
    """小写 + 去重音 + 去非字母数字 + 去队名噪音词 + 别名归一 → 可比较的归一化名。

    NFKD 分解（Atlético→atle◌́+tico）后按 Unicode Mn 类别直接删掉
    组合重音（不能当普通分隔符替换成空格，否则产生 'atl tico' 词内空格），
    再做小写/非字母数字清洗。最后查别名表，把同一队的跨语言/跨源
    惯用变体（Köln=Cologne、München=Munich 等）归到同一键。
    """
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    tokens = [t for t in s.split() if t not in _TEAM_NOISE_TOKENS]
    # 逐 token 别名替换（hamburger→hamburg、koln→cologne…），
    # 再对整体做别名兜底（lyonnais→lyon 等完整名映射）。
    tokens = [_TEAM_ALIASES.get(t, t) for t in tokens]
    s = " ".join(tokens)
    return _TEAM_ALIASES.get(s, s)


# 同一队的跨源/跨语言惯用变体 → 规范键。
# 只收录「归一化后仍无法互推、但确定是同一支球队」的别名（城市名
# 德/英/法拼写差异、俱乐部新旧名）。宁缺勿滥：不确定的变体不加，
# 靠开球时点唯一性兜底，绝不用别名制造错配。
_TEAM_ALIASES: dict[str, str] = {
    # 德国：Köln/Cologne、München/Munich、Hamburger/Hamburg
    "koln": "cologne",
    "munchen": "munich",
    "hamburger": "hamburg",
    # 法国：Lyon（俱乐部官方名 Olympique Lyonnais）
    "lyonnais": "lyon",
}


def _names_equivalent(a: str, b: str) -> bool:
    """归一化相等，或一方是另一方的前缀/后缀（队名惯用「短名 vs 长名」，
    如 La Coruña=coruna ⊂ Deportivo de La Coruña=deportivo coruna；
    长度差容忍 1 个词，防 'milton' 匹配到 'milton keynes'）。"""
    na, nb = normalize_team(a), normalize_team(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) < 4:
        return False
    # 短名须是长名的完整词边界前缀/后缀，且长度差 ≤ 1 个词
    if long_.startswith(short + " ") and len(long_.split()) - len(short.split()) == 1:
        return True
    if long_.endswith(" " + short) and len(long_.split()) - len(short.split()) == 1:
        return True
    return False


def _espn_1x2_decimal(odds: dict) -> dict[str, float | None]:
    """ESPN odds[0].moneyline → home/draw/away 十进制收盘赔率。"""
    ml = (odds.get("moneyline") or {})
    out: dict[str, float | None] = {}
    for side in ("home", "draw", "away"):
        entry = ml.get(side) or {}
        close = entry.get("close") or entry
        out[side] = american_to_decimal(close.get("odds"))
    return out


def _espn_market_dict(dec: dict[str, float | None], captured_at: str) -> dict | None:
    """构造 web 层 _market_projection 认得的 market 字典。

    缺失的一侧不写进 selections（web 侧据此降级 status=partial，
    不会误标 complete）。三侧全缺 → None。
    """
    selections = {k: {"decimal_odds": v} for k, v in dec.items() if v is not None}
    if not selections:
        return None
    return {
        "source": "espn",
        "captured_at": captured_at,
        "market_type": "1x2",
        "odds_format": "decimal",
        "selections": selections,
    }


def enrich_soccer_odds(future: list[dict], espn_events: list[dict],
                       now_utc: datetime) -> int:
    """把 ESPN 赔率回填进 football-data 预测行。就地修改 future，返回富化场次。

    匹配三层（可靠性递增，前一层命中即停）：
    1. 归一化主客队名相等 + 开球同日 → 直接命中；
    2. 队名前/后缀容器匹配 + 同日 → 命中；
    3. 队名对不上，但开球 UTC 时刻精确到分钟相同 + 同日，
       且该时点候选唯一 → 命中（同一联赛同日赛程两侧同源，时点一致）；
    任一时点候选 ≥2 且无法唯一 → 跳过该场（记 warning），绝不错配。
    """
    if not future or not espn_events:
        return 0

    # ESPN 侧索引：
    #   by_name[(nh, na, date)] → entry          队名精确/容器匹配
    #   by_kickoff[(date, "HH:MM")] → [entry...] 时点回退匹配
    index: dict[tuple[str, str, str], dict] = {}
    by_kickoff: dict[tuple[str, str], list] = {}
    captured_at = now_utc.isoformat()
    for ev in espn_events:
        try:
            comp = (ev.get("competitions") or [{}])[0]
            competitors = comp.get("competitors") or []
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            home_en = home.get("team", {}).get("displayName", "")
            away_en = away.get("team", {}).get("displayName", "")
            date = str(ev.get("date", ""))[:10]
            kickoff = str(ev.get("date", ""))[11:16]  # "HH:MM"（UTC）
            odds_list = comp.get("odds") or []
            if not odds_list:
                continue
            dec = _espn_1x2_decimal(odds_list[0])
            ps = odds_list[0].get("pointSpread") or {}
            tot = odds_list[0].get("total") or {}
            entry = {
                "decimal": dec,
                "spread_home_line": (ps.get("home") or {}).get("close", {}).get("line", "")
                             or (ps.get("home") or {}).get("open", {}).get("line", ""),
                "spread_home_close_odds": (ps.get("home") or {}).get("close", {}).get("odds", ""),
                "total_over_close": (tot.get("over") or {}).get("close", {}).get("line", ""),
                "total_under_close": (tot.get("under") or {}).get("close", {}).get("line", ""),
                "draw_ml": (odds_list[0].get("drawOdds") or {}).get("moneyLine", ""),
                "market": _espn_market_dict(dec, captured_at),
                "_home_n": normalize_team(home_en),
                "_away_n": normalize_team(away_en),
            }
            if not entry["decimal"] and not entry["market"]:
                continue
            index[(normalize_team(home_en), normalize_team(away_en), date)] = entry
            if date and kickoff:
                by_kickoff.setdefault((date, kickoff), []).append(entry)
        except (AttributeError, IndexError, TypeError):
            continue  # 单个坏事件不拖垮整批

    enriched = 0
    for match in future:
        if match.get("odds_data_available"):
            continue  # 数据源本身带赔率（espn/api-football 直连时）→ 不覆盖
        h = str(match.get("home_en") or match.get("home") or "")
        a = str(match.get("away_en") or match.get("away") or "")
        mdate = str(match.get("kickoff_utc") or "")[:10]
        mkickoff = str(match.get("kickoff_utc") or "")[11:16]  # "HH:MM"
        nh, na = normalize_team(h), normalize_team(a)

        entry = index.get((nh, na, mdate))
        if entry is None:
            # 第 2 层：容器匹配（队名前/后缀），限同开球日
            for (kh, ka, kd), cand in index.items():
                if kd != mdate:
                    continue
                if _names_equivalent(nh, kh) and _names_equivalent(na, ka):
                    entry = cand
                    break
        if entry is None:
            # 第 3 层：开球时点（UTC 精确到分钟）。
            # 单候选直接命中；多候选（同时刻多场）用队名相似度打分消歧：
            # 主/客队名容器匹配各 +1 分，取唯一最高分；最高分并列 → 跳过。
            cands = by_kickoff.get((mdate, mkickoff), [])
            if len(cands) == 1:
                entry = cands[0]
            elif len(cands) > 1:
                scores = []
                for c in cands:
                    c_h = c.get("_home_n", "")
                    c_a = c.get("_away_n", "")
                    s = 0
                    if _names_equivalent(nh, c_h):
                        s += 1
                    if _names_equivalent(na, c_a):
                        s += 1
                    scores.append((s, c))
                scores.sort(key=lambda x: -x[0])
                top = scores[0][0]
                if top > 0 and sum(1 for s, _ in scores if s == top) == 1:
                    entry = scores[0][1]
                else:
                    logger.warning("odds_enrich: %s 时点 %s:%s 有 %d 场候选无法唯一化，跳过 %s",
                                   mdate, mkickoff[:2], mkickoff[3:], len(cands), match.get("name"))
        if entry is None and not mkickoff:
            # 第 4 层：预测行缺开球时刻 → 退化为纯队名匹配（跨全部日期），
            # 仍要求唯一候选，避免错配。
            cands = [cand for (kh, ka, _d), cand in index.items()
                     if _names_equivalent(nh, kh) and _names_equivalent(na, ka)]
            if len(cands) == 1:
                entry = cands[0]
            elif len(cands) > 1:
                logger.warning("odds_enrich: 无开球时刻且队名匹配到 %d 场候选无法唯一化，跳过 %s",
                               len(cands), match.get("name"))
        if entry is None:
            continue
        _apply_enrichment(match, entry)
        enriched += 1
    if enriched:
        logger.info(f"odds_enrich: ESPN 赔率回填 {enriched}/{len(future)} 场")
    return enriched


def _apply_enrichment(match: dict, entry: dict) -> None:
    """把 ESPN 赔率写入 football-data 预测行（就地）。"""
    dec = entry.get("decimal") or {}
    home_impl = 1 / dec["home"] if dec.get("home") else None
    draw_impl = 1 / dec.get("draw") if dec.get("draw") else None
    away_impl = 1 / dec.get("away") if dec.get("away") else None
    home_t, draw_t, away_t = remove_vig(home_impl, draw_impl, away_impl)

    match["odds_data_available"] = True
    if dec.get("home"):
        match["home_ml_implied"] = round(home_impl, 4)
    if dec.get("draw"):
        match["draw_implied"] = round(draw_impl, 4)
    if dec.get("home"):
        match["home_true_prob"] = round(home_t, 4)
    if dec.get("draw"):
        match["draw_true_prob"] = round(draw_t, 4)
    if dec.get("away"):
        match["away_true_prob"] = round(away_t, 4)
    # 显示/信号字段（页面与 predictor 直读）
    if entry.get("spread_home_line"):
        match["spread_home_line"] = entry["spread_home_line"]
    if entry.get("spread_home_close_odds"):
        match["spread_home_close_odds"] = entry["spread_home_close_odds"]
    if entry.get("total_over_close"):
        match["total_over_close"] = entry["total_over_close"]
    if entry.get("total_under_close"):
        match["total_under_close"] = entry["total_under_close"]
    if entry.get("draw_ml"):
        match["draw_ml"] = entry["draw_ml"]
    if entry.get("market"):
        match["market"] = entry["market"]
