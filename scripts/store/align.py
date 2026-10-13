"""跨源对齐（纯函数，零 DB）：AF 的 fixture_id 是权威锚；FD 行按 (league_key, 开球时刻) 精确等值取候选列表，再用
队名 token 子集 / 同义表在候选里挑出**恰好一个**对阵命中的场次。四条"不猜"的硬规矩由真报文实测逼出（前提、命中率
与残余分类见 .omp-logs/P0-FACT3.report.txt）：开球时刻必须等值（两源对同一场有分钟级分歧）⇒ 差 1 秒即 no_time；
同刻多场是常态（周六 15:00 全开）⇒ 键唯一性作废，唯一性落到"配对"上；队名相等只会误杀（短名 vs 长名）⇒ 改判
token 子集（两侧非空、主客不互换、不许差实词）；两源不同专名只许查 TEAM_SYNONYMS 常量表，别用相似度蒙。
队名只用 AF 侧原名（FD 块里没有 AF 的 team id）⇒ af_names。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from core.log import logger
from core.team_norm import strip_accents_and_lowercase

# 俱乐部类型/法人词（真报文形态：FC/CF/AS/RC/OGC/US/SS/SSC/VfL/SV…）：只剥类型词，绝不剥队名实词
STOP_WORDS = frozenset(("fc afc cf sc ac club of the de if kc as rc ogc us ss ssc acf cfc bsc osc hsc"
                        " spvgg tsv tsg fsv sv vfl vfb calcio").split())
# 两源**专名**不同的唯一出口（键=归一后 FD 名 → 值=归一后 AF 名，一对一）；每条都由真实对阵暴露，见 report §6
TEAM_SYNONYMS: dict[str, str] = {
    "internazionale milano": "inter", "wanderers wolverhampton": "wolves", "lyonnais olympique": "lyon",
    "1901 rennais stade": "rennes", "sheffield united": "sheffield utd", "bayern munchen": "bayern munich",
}
TOKEN_HIT, SYNONYM_HIT, OK, OK_SYNONYM = "tokens", "synonym", "ok", "ok_synonym"
NO_TIME, AMBIGUOUS, NAME_MISMATCH, SWAPPED = "no_time", "ambiguous_pair", "name_mismatch", "home_away_swapped"
OK_REASONS = frozenset({OK, OK_SYNONYM})  # 判"是否命中"用这个集合，别按 == OK 单值比（同义表命中即 ok_synonym）


def tokens(name: str | None) -> frozenset[str]:
    """小写 + 去变音符 + 按非字母数字切词 + 剥类型词：'Newcastle United FC' → {newcastle, united}。

    NFKD + Mn 去除 + lowercase 已收口到 ``core.team_norm.strip_accents_and_lowercase``
    （P5-3）。STOP 词过滤仍属本模块专有（27 词含 vfl/afc/spvgg/...）。
    """
    return frozenset(w for w in re.split(r"[^a-z0-9]+", strip_accents_and_lowercase(name))
                     if w and w not in STOP_WORDS)


def normalize_team(name: str | None) -> str:
    """token 集排序拼串：同义表的键/值口径，两侧同口径才谈得上"同一专名"。"""
    return " ".join(sorted(tokens(name)))


def kickoff_at(value: object) -> datetime | None:
    """统一成 UTC 时刻：AF 给 `+00:00`、FD 给 `Z`，字符串直接比会永远不等。"""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def af_index(rows: list[dict]) -> dict[tuple[str, datetime], list[int]]:
    """AF 行 → {(league_key, 开球时刻): [fixture_id, …]}：同刻多场收成候选列表，由队名定夺而非整键置空。"""
    index: dict[tuple[str, datetime], list[int]] = {}
    for row in rows:
        moment = kickoff_at(row.get("kickoff_at"))
        if row.get("league_key") and moment:
            index.setdefault((row["league_key"], moment), []).append(row["fixture_id"])
    return index


def af_names(rows: list[dict]) -> dict[int, tuple[str | None, str | None]]:
    """{fixture_id: (主队原名, 客队原名)}：队名核对只能回 AF 行（FD 块里没有 AF 的 team id）。"""
    return {r["fixture_id"]: (r.get("home_name"), r.get("away_name")) for r in rows if r.get("fixture_id") is not None}


def _side_hit(af_name: str | None, fd_name: str | None) -> str | None:
    """单侧口径：先 token 子集（两侧都必须非空），再查同义表；都不中返回 None。"""
    af_tokens, fd_tokens = tokens(af_name), tokens(fd_name)
    if af_tokens and fd_tokens and (af_tokens <= fd_tokens or fd_tokens <= af_tokens):
        return TOKEN_HIT
    af_norm, fd_norm = normalize_team(af_name), normalize_team(fd_name)
    if af_norm and fd_norm and (TEAM_SYNONYMS.get(af_norm) == fd_norm or TEAM_SYNONYMS.get(fd_norm) == af_norm):
        return SYNONYM_HIT
    return None


def _pair_hit(af_home: str | None, af_away: str | None, fd_row: dict, swapped: bool = False) -> str | None:
    """候选场两队 vs FD 行两队：任一侧不中即不命中；任一侧走同义表 ⇒ 整场记 synonym 命中。"""
    home, away = fd_row.get("home_name"), fd_row.get("away_name")
    fd_home, fd_away = (away, home) if swapped else (home, away)  # 反序只用于识别，不算对齐成功
    hits = [_side_hit(af_home, fd_home), _side_hit(af_away, fd_away)]
    if None in hits:
        return None
    return SYNONYM_HIT if SYNONYM_HIT in hits else TOKEN_HIT


def match_fd(fd_row: dict, index: dict[tuple[str, datetime], list[int]], name_pairs: dict[int, tuple]):
    """FD 行 → (锚定的 fixture_id, reason)；reason 不在 OK_REASONS 时为空串 —— 宁可不插，不可插错。"""
    key = (fd_row.get("league_key"), kickoff_at(fd_row.get("kickoff_at")))
    candidates = index.get(key) or []
    if not candidates:
        return "", NO_TIME
    hits = [(fid, hit) for fid in candidates if (hit := _pair_hit(*name_pairs.get(fid, (None, None)), fd_row))]
    if len(hits) > 1:
        logger.warning(f"[align] FD {fd_row.get('fixture_id')} 在 {key} 命中 {len(hits)} 个候选 ⇒ {AMBIGUOUS}")
        return "", AMBIGUOUS
    if hits:
        return str(hits[0][0]), OK_SYNONYM if hits[0][1] == SYNONYM_HIT else OK
    swapped = [fid for fid in candidates if _pair_hit(*name_pairs.get(fid, (None, None)), fd_row, True)]
    return ("", SWAPPED) if swapped else ("", NAME_MISMATCH)
