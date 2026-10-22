"""竞彩队名 ↔ 模型队名的 **join 可达性审计**（只读；路线图 §6.13）。

问题（2026-10-22 生产实测）：竞彩场次与模型预测是**按中文名** join 的
（``scripts/model/archive.py::_match_jc`` → ``core/team_alias.py::teams_match``）。
若某支队的竞彩叫法**模型永远产不出来**（不在官方队名表 / LLM 缓存 / COUNTRY_CN 里，
别名表里也没有桥接），那一场就会**静默出局**：
  - ``model.pred_archive_match.jc_match_id`` 留空 ⇒ 荐单拿不到模型概率
  - ⇒ 不进 ``analysis.ev_report``、不产 CLV
  - ⇒ **页面上看不出任何异常**（纯静默失败）

本脚本把这条检查做成**人人可跑的复算命令**：
    python scripts/ingest/jc_name_join_audit.py
退出码：0 = 全部可达；1 = 有队名 join 必 miss（**不许静默**，要让门禁看见）。

口径（⚠️ 两个口径，主判据是**实际产出**）：
  - 「模型侧**实际产出过**的队名」= ``model.pred_archive_match`` 里真实出现过的
    home_cn / away_cn（限模型覆盖的联赛）—— **这是硬证据**：模型当时到底叫什么。
  - 「模型侧**理论可产出**名」= ``to_cn()`` 的四级链能产出的中文名：
    ① ``references/team_cn_official.json`` 的值 ② ``references/team_translations.json`` 的值
    ③ ``core.config.COUNTRY_CN`` 的值 ④ 别名表的目标名
    ⇒ 只在**还没有任何实际产出**时兜底（刚上线、还没跑过预测）。
  - 「可达」= 竞彩名的 canon 集（本体 + 别名表桥接）∩ 上述集合 ≠ ∅
  - 只审计**模型覆盖的联赛**（``ref.league`` 的 6 个：英超/西甲/意甲/德甲/法甲/欧冠）——
    模型不覆盖的联赛（日/韩/巴/美…）本来就没有预测，join miss 是正常语义。

⚠️ **为什么主判据必须是「实际产出」而不是「理论集合」**（2026-10-22 生产实测的教训）：
第一版审计用理论集合当主判据 ⇒ 报「零缺口」⇒ **其实漏了 5 条**：
``帕德博恩07`` / ``美因茨05``（ESPN displayName 带数字后缀，翻译后残留）、
``巴黎圣日耳曼``（竞彩「巴黎圣日尔曼」）、``热刺``（竞彩「托特纳姆热刺」）、
``赫塔菲``（竞彩「赫塔费」）。原因：LLM 缓存/COUNTRY_CN 里**有**「帕德博恩」「美因茨」，
理论集合判它「可达」⇒ 审计通过；**但模型实际产出的是「帕德博恩07」** ⇒ 实际 join 不上。
⇒ **「理论能产出」≠「实际会产出」** —— 审计必须对着真实产出说话。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parents[1]          # scripts/
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from core.config import COUNTRY_CN, FOOTBALL_DIR  # noqa: E402
from core.leagues import LEAGUE_CONFIG  # noqa: E402
from core.log import logger  # noqa: E402
from core.team_alias import alias_table, canon_names, norm_name  # noqa: E402

# 模型覆盖的联赛 key（足球；NBA 是独立入口 bball，不在此表）
_MODEL_LEAGUE_KEYS = sorted(LEAGUE_CONFIG.keys())

# 竞彩侧的联赛缩写（fact.jc_match.league_abbr）↔ ref.league.cn_name
_NAMES_SQL = """
SELECT n, count(*) AS n_matches
FROM (
    SELECT m.home_cn AS n, m.match_id FROM fact.jc_match m
     WHERE m.home_cn IS NOT NULL AND m.home_cn <> '' AND m.league_abbr = ANY(%s)
    UNION ALL
    SELECT m.away_cn, m.match_id FROM fact.jc_match m
     WHERE m.away_cn IS NOT NULL AND m.away_cn <> '' AND m.league_abbr = ANY(%s)
) t
GROUP BY 1
ORDER BY 1
"""

# 模型侧**实际产出过**的队名（生产硬证据）：归档里真实出现过的 home_cn / away_cn
_ACTUAL_NAMES_SQL = """
SELECT n, count(*) AS n_rows
FROM (
    SELECT am.home_cn AS n FROM model.pred_archive_match am
      JOIN model.pred_archive ar ON ar.archive_id = am.archive_id
     WHERE ar.league_key = ANY(%s) AND am.home_cn IS NOT NULL AND am.home_cn <> ''
    UNION ALL
    SELECT am.away_cn FROM model.pred_archive_match am
      JOIN model.pred_archive ar ON ar.archive_id = am.archive_id
     WHERE ar.league_key = ANY(%s) AND am.away_cn IS NOT NULL AND am.away_cn <> ''
) t
GROUP BY 1
ORDER BY 1
"""

# 映射自检：一个 ref.team 行挂了多个竞彩 id 时，这些 id 在竞彩源数据里的**队名**
# 必须都属于这支俱乐部（本体名 + 别名表桥接）⇒ 否则说明某个 id 挂错了队
# （2026-10-22 实例：jc_id 279「贝西克塔斯」被挂到了「毕尔巴鄂竞技」行）。
_MAPPING_SQL = """
WITH id_nm AS (
    SELECT home_sporttery_id AS jc_id, home_cn AS nm FROM fact.jc_match
     WHERE home_sporttery_id IS NOT NULL
    UNION ALL
    SELECT away_sporttery_id, away_cn FROM fact.jc_match WHERE away_sporttery_id IS NOT NULL
),
per_id AS (SELECT jc_id, min(nm) AS nm FROM id_nm GROUP BY jc_id),
multi AS (SELECT team_id FROM ref.team_sporttery WHERE sport = 'football'
           GROUP BY team_id HAVING count(*) > 1)
SELECT m.team_id, t.name_cn, s.jc_id, p.nm AS source_name
FROM multi m
JOIN ref.team t ON t.team_id = m.team_id
JOIN ref.team_sporttery s ON s.team_id = m.team_id
JOIN per_id p ON p.jc_id = s.jc_id
ORDER BY m.team_id, s.jc_id
"""


def model_side_names() -> set[str]:
    """``to_cn()`` 四级链能产出的全部中文名。"""
    out: set[str] = set()
    official_path = FOOTBALL_DIR / "references" / "team_cn_official.json"
    if official_path.exists():
        raw = json.loads(official_path.read_text(encoding="utf-8"))
        out |= {v for k, v in raw.items() if not k.startswith("_") and isinstance(v, str)}
    tr_path = FOOTBALL_DIR / "references" / "team_translations.json"
    if tr_path.exists():
        raw = json.loads(tr_path.read_text(encoding="utf-8"))
        out |= {v for v in raw.values() if isinstance(v, str)}
    out |= {v for v in COUNTRY_CN.values() if isinstance(v, str)}
    out |= {t for targets in alias_table().values() for t in targets}
    return out


def unreachable(names: dict[str, int], model_side: set[str],
                 actual: set[str] | None = None) -> list[tuple[str, int]]:
    """返回 (竞彩名, 受影响场次数)：模型产不出来的队名。

    ``actual`` = 模型侧**实际产出过**的队名（生产硬证据）。
    给了它就以它为主判据 —— **「理论能产出」≠「实际会产出」**
    （ESPN displayName 残留数字后缀等坑，见模块 docstring 的实测记录）。
    ``actual`` 为空（还没跑过预测）时才退回理论集合。
    """
    known = {norm_name(m) for m in (actual if actual else model_side)}
    out = []
    for name, n_matches in names.items():
        if not ({norm_name(c) for c in canon_names(name)} & known):
            out.append((name, n_matches))
    return sorted(out, key=lambda kv: (-kv[1], kv[0]))


def close_candidates(name: str, model_side: set[str], limit: int = 3) -> list[str]:
    """给不可达的竞彩名找出「最像的模型侧名字」（提示用，不是判据）。

    判定：去掉数字后缀后相等 / 双向子串 / 共有 ≥2 字连续子串。
    例：竞彩「帕德博恩」→ 候选「帕德博恩07」；竞彩「赫塔费」→ 候选「赫塔菲」。
    """
    def strip_num(s: str) -> str:
        return s.rstrip("0123456789")

    out: list[str] = []
    for cand in sorted(model_side):
        a, b = norm_name(name), norm_name(cand)
        if not a or not b:
            continue
        if strip_num(a) == strip_num(b) or b in a or a in b:
            out.append(cand)
        elif len(a) >= 2 and len(b) >= 2 and any(a[i:i + 2] in b for i in range(len(a) - 1)):
            out.append(cand)
    return out[:limit]


def allowed_source_names(row_name: str) -> set[str]:
    """这个队行**可以拥有**的竞彩源名集合（归一后）。

    = 本体名 + **别名表里指向它的那些键**。
    ⚠️ 别名表的方向是「竞彩侧用词 → 允许匹配到的预测侧用词」，
    所以「贝西克塔斯 → 皇家贝蒂斯」意味着：**皇家贝蒂斯**这个队行也允许
    源名「贝西克塔斯」（反查键，不是查 values）。
    """
    out = {norm_name(row_name)}
    for key, targets in alias_table().items():
        if norm_name(row_name) in {norm_name(t) for t in targets}:
            out.add(norm_name(key))
    return out


def mapping_violations(rows: list[tuple]) -> list[tuple]:
    """一个队行挂了多个 id，但某个 id 的竞彩源名**不属于**这支俱乐部 ⇒ id 挂错了队。

    ``rows`` = (team_id, name_cn, jc_id, source_name) 四元组。
    判定用别名表反查：源名必须落在该队行名 + 「指向它的别名键」范围内
    （例：#2355「皇家贝蒂斯」挂着 505「皇家贝蒂斯」与 279「贝西克塔斯」⇒
    「贝西克塔斯」经别名表桥接后属于它 ⇒ **合法**）。
    """
    out = []
    by_team: dict[int, list[tuple]] = {}
    for team_id, name_cn, jc_id, source_name in rows:
        by_team.setdefault(team_id, []).append((name_cn, jc_id, source_name))
    for team_id, items in by_team.items():
        name_cn = items[0][0]
        allowed = allowed_source_names(name_cn)
        for _cn, jc_id, source_name in items:
            if norm_name(source_name) not in allowed:
                out.append((team_id, name_cn, jc_id, source_name))
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="竞彩队名 ↔ 模型队名 join 可达性审计（只读）")
    ap.add_argument("--conn", default="ro", help="数据库角色（缺省 ro = 只读）")
    args = ap.parse_args(argv)

    from store import pg

    try:
        with pg.read_conn(args.conn) as conn:
            leagues = [r[0] for r in conn.execute(
                "SELECT cn_name FROM ref.league WHERE sport = 'football'").fetchall()]
            rows = conn.execute(_NAMES_SQL, (leagues, leagues)).fetchall()
            actual_rows = conn.execute(
                _ACTUAL_NAMES_SQL, (_MODEL_LEAGUE_KEYS, _MODEL_LEAGUE_KEYS)).fetchall()
            map_rows = conn.execute(_MAPPING_SQL).fetchall()
    except Exception as exc:  # noqa: BLE001 — 连不上就明说，绝不假装「零缺口」
        logger.error("连不上库（%s 角色）：%s: %s", args.conn, type(exc).__name__, exc)
        return 2

    names = {r[0]: int(r[1]) for r in rows}
    actual = {r[0] for r in actual_rows}
    model_side = model_side_names()
    # 主判据 = **实际产出名**（理论集合只在还没跑过预测时兜底）
    bad = unreachable(names, model_side, actual or None)
    bad_map = mapping_violations(map_rows)

    print(f"审计范围：模型覆盖的 {len(leagues)} 个联赛（{', '.join(leagues)}）")
    print(f"竞彩队名 {len(names)} 个；模型侧**实际产出**名 {len(actual)} 个"
          f"（理论可产出名 {len(model_side)} 个）")
    if bad:
        print(f"❌ {len(bad)} 个竞彩队名模型实际产不出来（对应场次会被静默丢出荐单/CLV）：")
        for name, n in bad:
            cands = close_candidates(name, actual or model_side)
            tail = f"　← 最像的模型侧名：{', '.join(cands)}" if cands else ""
            print(f"   {name}（影响 {n} 场）{tail}")
        print("修法：往 core/team_alias.py 与 web/services/team_match.py 的别名表加桥接"
              "（键=竞彩侧用词，值=预测侧用词），两侧必须同时改。")
    else:
        print("✅ 全部竞彩队名都可 join 到模型预测（无静默出局）")

    if bad_map:
        print(f"❌ {len(bad_map)} 个竞彩 id 挂到了不属于它的队行（映射错配）：")
        for team_id, name_cn, jc_id, source_name in bad_map:
            print(f"   jc_id {jc_id}（竞彩名「{source_name}」）→ #{team_id}「{name_cn}」")
    else:
        print(f"✅ 挂多个 id 的队行全部自洽（{len({r[0] for r in map_rows})} 支，源名都在别名桥接范围内）")

    return 1 if (bad or bad_map) else 0


if __name__ == "__main__":
    raise SystemExit(main())
