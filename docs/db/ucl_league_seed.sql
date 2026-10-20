-- R49 · 建议 1「模型补欧冠」—— ref.league 增补 ucl 行
-- 生成：2026-10-20 · 依据：docs/project/产品路线图-竞彩闭环-20261020.md §建议 1
--
-- 为什么必须先插这一行（**不是可选项**）：
--   scripts/store/upsert_fixtures.py::league_maps 是 raw 落库的唯一联赛真相源 ——
--   它从 ref.league.ids 取 api_football id → league_key 映射，
--   `af_anchors()` 里 `known = set(af_map.values()) | set(fd_map.values())`，
--   **不在 ref.league 的 league_key 会被直接丢掉**（`dropped`）。
--   ⇒ 不插这行，AF 回填回来的欧冠数据在 raw→fact.fixture 这一步就被丢弃，
--     训练集永远是空的。
--
-- ids 字段：只写 api_football（=2，UEFA Champions League）。
--   **故意不写 football_data** —— football-data.org 没有欧冠，
--   写一个假 code 会让回填器拼出无效 URL（并 403）。
--   scripts/store/parse_api.py::FD_LEAGUES 派生自 LEAGUE_CONFIG 的 league_id 字段，
--   欧冠配置里也没有该字段 ⇒ FD 源自动跳过，两处一致。
--
-- scope='regular'：与既有 5 个联赛一致（schema 的 CHECK 只允许 regular/irregular；
--   window_from/window_to 是「开关制激活窗口」占位列，既有 5 行也都是 NULL ⇒ 同样留空）。
--
-- 执行方式（生产）：
--   sudo -n -u postgres psql -d league -v ON_ERROR_STOP=1 -f docs/db/ucl_league_seed.sql
-- 回滚：
--   DELETE FROM ref.league WHERE league_key = 'ucl';   -- 需先无 fact.fixture 引用

\set ON_ERROR_STOP on

BEGIN;

-- 幂等：已存在则只补全缺失字段，绝不覆盖已有 ids（避免把别人的 football_data 键抹掉）
INSERT INTO ref.league (league_key, cn_name, sport, scope, ids)
VALUES ('ucl', '欧冠', 'football', 'regular', '{"api_football": 2}'::jsonb)
ON CONFLICT (league_key) DO UPDATE
   SET cn_name = EXCLUDED.cn_name,
       sport   = EXCLUDED.sport,
       scope   = EXCLUDED.scope,
       ids     = ref.league.ids || EXCLUDED.ids,
       updated_at = now();

COMMIT;

-- 核验（应返回 1 行，ids = {"api_football": 2}）
-- SELECT league_key, cn_name, sport, scope, ids FROM ref.league WHERE league_key = 'ucl';
