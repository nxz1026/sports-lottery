-- ============================================================================
-- 建议 2 遗留 A：竞彩官方 team id → ref.team 的**多对一**映射表
-- 依据：路线图 §6.10.1（2026-10-21 生产实测）。
--
-- 为什么不是「把 aliases->>'sporttery' 从单值改成数组」（原计划）：
--   ① `->>` 作用在 jsonb **数组**上返回的是**数组的 JSON 文本**（["506","279"]）而不是 NULL
--      ⇒ jc_write.resolve_jc_teams 的 `(aliases->>'sporttery') = ANY(%s)` 会一条都匹配不上
--      ⇒ **152 支队的映射静默全断**（而这个映射率本身是被观测的指标）；
--   ② ref.team.jc_id 是 STORED 生成列（表达式要改写），team_jc_key partial unique 只能守**主 id**
--      ⇒ 次 id 失去防撞，比现状更弱；
--   ③ 152 行存量要从字符串迁成单元素数组（全量重写 jsonb）。
--   实测收益对照：245 个未映射 id 里 0 个能靠改 schema 救回（ref.team 里压根没有那一行），
--   数组化真正能救的只有「一支队两个 id」这 1 例 3 个 side —— 而本表同样能救，且**零破坏**。
--
-- 本表的取舍：
--   · PRIMARY KEY (sport, jc_id) ⇒ **一个官方 id 只能属一支队**，
--     对**所有** id 生效（比数组方案的 partial unique 更强：次 id 也受保护）；
--   · `aliases->>'sporttery'` **语义一字不动**（仍是「主 id」）⇒ 所有现有读者零影响；
--     ref.team.jc_id 生成列**不改** ⇒ 零 DDL 风险；
--   · 纯增量：只新建表 + 灌存量，**不改 ref.team 任何一行**（name_cn / aliases 全部零改动）。
--
-- 跑法（**全部写入在同一个事务里**，先预演再真执行）：
--   # ① 预演：把 COMMIT 换成 ROLLBACK ⇒ 建表/迁移/回填全部撤销，一个字节都不留
--   sed 's/^COMMIT;/ROLLBACK;/' suggestion10_team_sporttery_map.sql \
--     | sudo -u postgres psql -d league -v ON_ERROR_STOP=1 -f -
--   # ② 真执行
--   sudo -u postgres psql -d league -v ON_ERROR_STOP=1 -f /tmp/suggestion10_team_sporttery_map.sql
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------- 1) 映射表
CREATE TABLE IF NOT EXISTS ref.team_sporttery (
    sport      text    NOT NULL,                 -- 'football'（与 ref.team.sport 同口径；竞彩篮球另起）
    jc_id      bigint  NOT NULL,                 -- 竞彩官方 teamId（webapi.sporttery.cn 的 homeTeamId/awayTeamId）
    team_id    integer NOT NULL REFERENCES ref.team(team_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sport, jc_id)                   -- **一个官方 id 只能属一支队**（防撞靠数据库，不靠代码）
);
COMMENT ON TABLE ref.team_sporttery IS '竞彩官方 team id → ref.team 队行的多对一映射（§6.10.1）；'
                                       'aliases->>''sporttery'' 仍只是「主 id」，本表才是多 id 的真源';
COMMENT ON COLUMN ref.team_sporttery.jc_id IS '竞彩官方 teamId；同一家俱乐部在竞彩可能有多个 id（例：毕尔巴鄂竞技 506 / 贝西克塔斯 279）';
CREATE INDEX IF NOT EXISTS idx_team_sporttery_team ON ref.team_sporttery (team_id);

-- ---------------------------------------------------------------- 2) 存量迁移（幂等）
--    来源 = ref.team 里带 sporttery 键且 jc_id 非空的行（152 行）⇒ 一行不多、一行不少。
INSERT INTO ref.team_sporttery (sport, jc_id, team_id)
SELECT t.sport, t.jc_id, t.team_id
FROM ref.team t
WHERE t.jc_id IS NOT NULL AND t.aliases ? 'sporttery'
ON CONFLICT (sport, jc_id) DO NOTHING;

-- ---------------------------------------------------------------- 3) 已知的一队两 id（幂等）
--    贝西克塔斯 sid 279 = 毕尔巴鄂竞技 sid 506（#2351）：
--    依据是 279 的 2 场对手是马赛、霍芬海姆 ⇒ 欧联 ⇒ 西甲劲旅毕尔巴鄂竞技。
INSERT INTO ref.team_sporttery (sport, jc_id, team_id)
SELECT 'football', 279, 2351
WHERE EXISTS (SELECT 1 FROM ref.team t WHERE t.team_id = 2351 AND t.sport = 'football')
ON CONFLICT (sport, jc_id) DO NOTHING;

-- ---------------------------------------------------------------- 4) 授权（与 ref.team 完全同口径）
--    ref.team：league_ing 有 INSERT/SELECT/UPDATE（**无 DELETE**），app/ro 只读。
--    本表同样不给 league_ing DELETE —— 映射错了要改只能 UPDATE（留痕在 created_at 与 git 里）。
GRANT USAGE ON SCHEMA ref TO league_ing, league_app, league_ro;
GRANT SELECT, INSERT, UPDATE ON ref.team_sporttery TO league_ing;
GRANT SELECT ON ref.team_sporttery TO league_app, league_ro;

-- ---------------------------------------------------------------- 5) 回填 fact.jc_match 的队伍映射（幂等、只补 NULL）
--    ⚠️ 只补 NULL：**已有非空 team_id 一律不动**。若某 side 已有映射而与本表冲突，
--    说明是数据打架 —— 留给人工判，不许脚本悄悄改写。
UPDATE fact.jc_match m
SET home_team_id = ts.team_id
FROM ref.team_sporttery ts
WHERE ts.sport = 'football' AND ts.jc_id = m.home_sporttery_id
  AND m.home_team_id IS NULL;

UPDATE fact.jc_match m
SET away_team_id = ts.team_id
FROM ref.team_sporttery ts
WHERE ts.sport = 'football' AND ts.jc_id = m.away_sporttery_id
  AND m.away_team_id IS NULL;

COMMIT;

-- ---------------------------------------------------------------- 6) 验证（跑完逐条核对）
-- SELECT count(*) AS map_rows FROM ref.team_sporttery;                      -- 预期 153（152 存量 + 279）
-- SELECT count(*) AS teams_still_single FROM ref.team
--   WHERE aliases ? 'sporttery' AND jc_id IS NOT NULL;                       -- 预期 152（ref.team 一行未改）
-- SELECT count(*) AS dup_ids FROM (                                         -- **必须 0**：一个 id 指向两支行
--   SELECT jc_id FROM ref.team_sporttery GROUP BY sport, jc_id HAVING count(*) > 1) d;
-- SELECT count(*) AS teams_with_multi_ids FROM (                            -- 预期 1（#2351：506 + 279）
--   SELECT team_id FROM ref.team_sporttery GROUP BY team_id HAVING count(*) > 1) m;
-- SELECT count(*) AS jc_matches_both_mapped FROM fact.jc_match
--   WHERE home_team_id IS NOT NULL AND away_team_id IS NOT NULL;             -- 预期 227（原 226）
-- SELECT count(*) AS ref_team_rows FROM ref.team;                           -- 预期 267（**一行未增未改**）
