"""认队口径（P0-TEAMKEY1）：ref.team 的身份是**源 id**（生成列 af_id/fd_id），name_cn 只是展示名。

起因：老唯一键 UNIQUE(sport, name_cn)，而 name_cn = to_cn(源队名串)；AF 跨季改拼写（Vfl→VfL Bochum、
Bayern Munich→Bayern München）+ to_cn 按串查词典 ⇒ 同一家俱乐部（af_id 完全相同）被插成两行，拟合时"上一季没
这支队"，德甲 2024 季 306 场只写出 132 场预测。故规则三条：
  ① 先按源 id 查（AF 查 af_id）：命中**只并 aliases**，绝不回改 name_cn（展示名要跨季稳定）；
  ② 未命中才 INSERT（name_cn = to_cn(原名)），撞 name_cn 再回落 `aliases || EXCLUDED.aliases`；
  ③ FD 队的 fd_id 只并进**对齐命中那一场**的两队（锚点 = fact.fixture 的 team_id）：不按队名猜、不建新行。
本模块出 SQL + 两个写入口。认队体检读法已迁 ops 端（store.selfcheck 双源覆盖接口），
``team_identity_audit``/``AUDIT_SQL`` 旧接口保留为兼容性 grep 钩子都不再启用。
"""

from __future__ import annotations

from core.i18n import to_cn
from psycopg.types.json import Jsonb

AF_KEY, FD_KEY = "api_football", "football_data"  # aliases 两源键；af_id/fd_id 生成列由它们派生
TEAM_SQL = ("INSERT INTO ref.team (sport, name_cn, aliases) VALUES (%s, %s, %s) ON CONFLICT (sport, name_cn)"
            " DO UPDATE SET aliases = ref.team.aliases || EXCLUDED.aliases RETURNING team_id")
TEAM_AF_SQL = "SELECT team_id FROM ref.team WHERE sport = %s AND af_id = %s"  # 认队第一凭据
TEAM_ALIAS_SQL = "UPDATE ref.team SET aliases = ref.team.aliases || %s WHERE team_id = %s"  # 只并，不动 name_cn
FIXTURE_TEAMS_SQL = "SELECT home_team_id, away_team_id FROM fact.fixture WHERE fixture_id = %s"
# FD 队 id 只并"本行还没有、且全库没人占用"的键：绝不覆盖既有身份、绝不撞 partial unique（占用者多半是同一家俱乐部的
# 另一个行 = 跨键重复，属 DDL 侧去重）；行已持有同一个 fd_id 时按 no-op 计入"已就位"（rowcount=1，重跑不报冲突）。
FD_TEAM_SQL = ("UPDATE ref.team t SET aliases = t.aliases || jsonb_build_object(%s::text, %s::bigint)"
               " WHERE t.team_id = %s AND (t.fd_id = %s OR (t.fd_id IS NULL AND NOT EXISTS"
               " (SELECT 1 FROM ref.team o WHERE o.sport = t.sport AND o.fd_id = %s)))")


def resolve_team(cur, name: str | None, af_id: int | None) -> int | None:
    """AF 侧认队：af_id 命中即只并 aliases（name_cn 保持首见值）；未命中才 INSERT，撞 name_cn 回落并 aliases。
    缺 id 或名字 ⇒ None（不建队伍、不猜中文名）。
    """
    if not name or af_id is None:
        return None
    found = cur.execute(TEAM_AF_SQL, ("football", af_id)).fetchone()
    if found:
        cur.execute(TEAM_ALIAS_SQL, (Jsonb({AF_KEY: af_id}), found[0]))
        return found[0]
    cur.execute(TEAM_SQL, ("football", to_cn(name), Jsonb({AF_KEY: af_id})))
    return cur.fetchone()[0]


def merge_fd_teams(cur, fixture_id: int, fd_teams: tuple | None) -> int:
    """把 FD 两队 id 并进对齐命中那场的两队 aliases（football_data 键），返回**没能并入**的队数。

    锚点只认 fact.fixture 的两队 team_id：FD 块里没有 AF 队 id、按队名找队正是本次要废掉的旧规则；align 只放行
    home↔home 未反序的命中（SWAPPED 不算命中），所以键位天然对齐。没并成的两种情形都返回 >0：目标行已持有别的
    fd_id，或该 fd_id 已被**别的行**占着（真库现状：38 家俱乐部跨键重复）——代码不猜、不覆盖，交 DDL 去重。
    """
    known = cur.execute(FIXTURE_TEAMS_SQL, (fixture_id,)).fetchone() or (None, None)
    blocked = 0
    for team_id, fd_id in zip(known, fd_teams or (None, None)):
        if team_id is not None and fd_id is not None:
            cur.execute(FD_TEAM_SQL, (FD_KEY, fd_id, team_id, fd_id, fd_id))
            blocked += 1 - cur.rowcount
    return blocked
