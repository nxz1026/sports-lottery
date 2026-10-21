-- 竞彩官方 id → ref.team 的**批量建行 + 灌映射**（路线图 §6.12.3 A）
--
-- 背景（2026-10-21 生产实测）：
--   fact.jc_match 里出现过的官方 id 共 **397** 个，其中 **153** 个已映射
--   （ref.team_sporttery + ref.team.aliases->>'sporttery'），
--   剩下 **244** 个**没有对应的 ref.team 行**。
--   ⇒ 这不是 schema 问题、也不是「一队多 id」问题，而是**那一行根本不存在**（§6.10.1 已查实）。
--   实测这 244 个竞彩中文名**两两不同**、且与现有 ref.team.name_cn **零冲突**
--   ⇒ 1:1 建行无歧义，覆盖 **23 个联赛**（欧洲国家联赛 42 / 美国职业大联盟 20 / 巴西甲 19 /
--   国际赛 16 / 日本职业联赛 14 / 亚洲冠军精英联赛 14 / 亚运会男足 13 …）。
--
-- ⚠️ 诚实结论：对**用户可见产品零收益** —— 产品层按队名 join（展示名取
--    fact.jc_match.home_cn/away_cn），ref.team 全仓只被 scripts/store/jc_ops_view.py 读。
--    ⇒ 做的理由是**把数据补齐**，不是产品收益。
--
-- 三条硬纪律（缺一条就可能把历史 join 键改写）：
--   ① **只增不改**：既有 ref.team 行一个字节都不动（只 INSERT 新行 + ON CONFLICT DO NOTHING）；
--   ② **不猜**：队名只取 fact.jc_match 的**竞彩官方中文名**（唯一真源），不做任何名称推断；
--   ③ **只补 NULL**：fact.jc_match 的 team_id 只填 NULL，已有映射绝不被改写。
--
-- ⚠️ 本文件是**与 scripts/ingest/jc_write.py::ensure_jc_team_mappings 逐句等价的手工版**
--    （单事务版，方便 DBA 直接跑；采集侧走 Python 入口，逻辑是同一套）。
--    两边改一处必须同步改另一处（守门 tools/check_docs_claims.py 有「落表 SQL 与代码对账」断言）。
--
-- 用法（生产）：
--    psql -d league -f docs/db/suggestion12_team_backfill.sql
--    预演：把最后的 COMMIT 换成 ROLLBACK（或整段包在 BEGIN ... ROLLBACK 里）。
--    采集侧自动补齐：每轮 jc_load 会调 ensure_jc_team_mappings（建完即 0 行改动）。

BEGIN;

-- ① 建行：为「官方 id 出现过、但 ref.team 里没有行」的队建行
--    （DISTINCT ON + ORDER BY ⇒ 一支队多个 id 时**取最小的 id 当主 id**，保证可重复、可复算）
INSERT INTO ref.team (sport, name_cn, aliases)
SELECT DISTINCT ON (u.cn) 'football', u.cn, jsonb_build_object('sporttery', u.jc_id::text)
FROM (
    SELECT home_sporttery_id AS jc_id, home_cn AS cn FROM fact.jc_match
    UNION ALL
    SELECT away_sporttery_id AS jc_id, away_cn AS cn FROM fact.jc_match
) u
WHERE u.jc_id IS NOT NULL AND u.cn IS NOT NULL AND u.cn <> ''
  AND NOT EXISTS (SELECT 1 FROM ref.team_sporttery ts WHERE ts.sport = 'football' AND ts.jc_id = u.jc_id)
  AND NOT EXISTS (SELECT 1 FROM ref.team t2 WHERE t2.aliases->>'sporttery' = u.jc_id::text)
  AND NOT EXISTS (SELECT 1 FROM ref.team t3 WHERE t3.sport = 'football' AND t3.name_cn = u.cn)
ORDER BY u.cn, u.jc_id
ON CONFLICT (sport, name_cn) DO NOTHING;

-- ② 灌映射：把官方 id 指向对应队行（映射表 PK 是 (sport, jc_id) ⇒ 一个 id 只属一支队）
INSERT INTO ref.team_sporttery (sport, jc_id, team_id)
SELECT DISTINCT ON (u.jc_id) 'football', u.jc_id, t.team_id
FROM (
    SELECT home_sporttery_id AS jc_id, home_cn AS cn FROM fact.jc_match
    UNION ALL
    SELECT away_sporttery_id AS jc_id, away_cn AS cn FROM fact.jc_match
) u
JOIN ref.team t ON t.sport = 'football' AND t.name_cn = u.cn
WHERE u.jc_id IS NOT NULL AND u.cn IS NOT NULL AND u.cn <> ''
  AND NOT EXISTS (SELECT 1 FROM ref.team_sporttery ts WHERE ts.sport = 'football' AND ts.jc_id = u.jc_id)
ORDER BY u.jc_id, t.team_id
ON CONFLICT (sport, jc_id) DO NOTHING;

-- ③ 回填存量场次：**只补 NULL**（已有映射绝不被改写）
UPDATE fact.jc_match m SET home_team_id = ts.team_id
FROM ref.team_sporttery ts
WHERE ts.sport = 'football' AND ts.jc_id = m.home_sporttery_id AND m.home_team_id IS NULL;

UPDATE fact.jc_match m SET away_team_id = ts.team_id
FROM ref.team_sporttery ts
WHERE ts.sport = 'football' AND ts.jc_id = m.away_sporttery_id AND m.away_team_id IS NULL;

-- ④ 对账输出（幂等与否全看它）
SELECT (SELECT count(*) FROM ref.team)                                        AS ref_team_rows,
       (SELECT count(*) FROM ref.team_sporttery)                              AS map_rows,
       (SELECT count(DISTINCT ts.jc_id) FROM ref.team_sporttery ts
         WHERE ts.sport = 'football')                                         AS mapped_ids,
       (SELECT count(*) FROM fact.jc_match
         WHERE home_team_id IS NOT NULL AND away_team_id IS NOT NULL)         AS matches_fully_mapped,
       (SELECT count(*) FROM fact.jc_match)                                    AS jc_match_rows,
       (SELECT count(DISTINCT u.jc_id) FROM (
            SELECT home_sporttery_id AS jc_id FROM fact.jc_match UNION ALL
            SELECT away_sporttery_id FROM fact.jc_match) u
         WHERE NOT EXISTS (SELECT 1 FROM ref.team_sporttery ts2
                           WHERE ts2.sport = 'football' AND ts2.jc_id = u.jc_id)) AS unmapped_ids,
       (SELECT count(*) FROM (SELECT jc_id FROM (
            SELECT home_sporttery_id AS jc_id, home_cn AS cn FROM fact.jc_match UNION ALL
            SELECT away_sporttery_id, away_cn FROM fact.jc_match) x
          GROUP BY jc_id HAVING count(DISTINCT cn) > 1) y)                    AS multi_name_ids;

COMMIT;
