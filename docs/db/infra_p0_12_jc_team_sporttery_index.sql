-- P0-JCTEAM2 工单 4：jc_write.resolve_jc_teams 的热路径表达式索引（2026-09-17 核验缺失）
-- 核验命令：
--   select indexname, indexdef from pg_indexes
--    where schemaname='ref' and tablename='team' and indexdef ilike '%sporttery%';
-- 结果：空（无任何含 sporttery 的表达式索引）
-- 执行者：队长（已执行；工单约定：DDL 由 DDL owner 执行，coder 不直接跑库）
-- 回执：2026-09-17，CREATE INDEX 成功，idx_team_sporttery 已存在。
-- 说明：ref.team 现 128 行，量小不痛；cron 每 10 分钟、每 topic 都走该表达式查询，迟早需要。
CREATE INDEX IF NOT EXISTS idx_team_sporttery ON ref.team ((aliases->>'sporttery'));
