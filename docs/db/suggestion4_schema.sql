-- R49 · 建议 4「填决策层（今日推荐单 + Kelly + 竞彩实际赔率结算）」—— DDL（幂等）
-- 生成：2026-10-21 · 依据：docs/project/产品路线图-竞彩闭环-20261020.md §6.6
--
-- 设计要点：
--   ① `analysis.ev_report` **既有 8 列语义不动**（report_id/made_at/legs/p_parlay/ev/kelly/corr_adj/note），
--      只**加可空列**：让「每行 = 一条推荐腿」这件事能被 SQL 直接查（结算、统计不必翻 jsonb）。
--      该表此前 **0 行且全仓无人读取** ⇒ 加列零风险（既有行全为 NULL）。
--   ② 结算结果落**新表** `analysis.rec_settle`（建议 4 子项 ③）。
--   ③ 幂等：唯一键 (match_id, option_code, made_at::date) ⇒ 重跑只更新价格与概率，不重复插行。
--
-- 权限：与既有 analysis.* 一致（league_app 读写、league_ro 只读、league_ing 无权）。
--
-- 执行方式（生产）：
--   sudo -n -u postgres psql -d league -v ON_ERROR_STOP=1 -f docs/db/suggestion4_schema.sql
-- 回滚（**只删新增表/新增索引/新增列**，不动既有列与既有数据）：
--   DROP TABLE IF EXISTS analysis.rec_settle;
--   DROP INDEX IF EXISTS analysis.ev_report_pick_uq;
--   ALTER TABLE analysis.ev_report
--     DROP COLUMN IF EXISTS match_id, DROP COLUMN IF EXISTS option_code,
--     DROP COLUMN IF EXISTS sp_decide, DROP COLUMN IF EXISTS p_model,
--     DROP COLUMN IF EXISTS match_date, DROP COLUMN IF EXISTS league_key,
--     DROP COLUMN IF EXISTS decided_at;

\set ON_ERROR_STOP on

BEGIN;

-- ─────────────────────────────────────────────────────────────
-- ① analysis.ev_report 加可空列（推荐单的结构化载体）
--    一行 = 一条推荐腿（legs jsonb 里是同一条腿的完整描述）
-- ─────────────────────────────────────────────────────────────
ALTER TABLE analysis.ev_report ADD COLUMN IF NOT EXISTS match_id      bigint;
ALTER TABLE analysis.ev_report ADD COLUMN IF NOT EXISTS option_code   text;
ALTER TABLE analysis.ev_report ADD COLUMN IF NOT EXISTS sp_decide     numeric;  -- 决策时刻的竞彩官方 SP
ALTER TABLE analysis.ev_report ADD COLUMN IF NOT EXISTS p_model       numeric;  -- 模型概率（模型自己的输出）
ALTER TABLE analysis.ev_report ADD COLUMN IF NOT EXISTS match_date    date;     -- BJT 比赛日
ALTER TABLE analysis.ev_report ADD COLUMN IF NOT EXISTS league_key    text;
ALTER TABLE analysis.ev_report ADD COLUMN IF NOT EXISTS decided_at    timestamptz;

-- 幂等：同场同侧同一天只留一行（重跑更新，不重复插）
-- ⚠️ 表达式索引用的是 made_at::date（推荐单的「业务日」= 决策日）
CREATE UNIQUE INDEX IF NOT EXISTS ev_report_pick_uq
    ON analysis.ev_report (match_id, option_code, (made_at::date));

-- ─────────────────────────────────────────────────────────────
-- ② analysis.rec_settle：推荐单的结算结果（建议 4 子项 ③）
--    结算价 = 竞彩**官方结算 SP**（fact.jc_result.sp_home/sp_draw/sp_away），
--    命中判定 = fact.jc_result.win_flag（H/D/A），缺失时用比分 ft_h/ft_a。
--    ⚠️ 结算 SP ≠ 决策 SP（竞彩每 10 分钟变盘）⇒ 两个都存，可复算。
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS analysis.rec_settle (
    settle_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pick_id    bigint REFERENCES analysis.ev_report(report_id) ON DELETE CASCADE,
    match_id   bigint NOT NULL,
    option_code text   NOT NULL,
    league_key text,
    match_date date,
    sp_decide  numeric,                          -- 决策时刻的竞彩官方 SP
    sp_settle  numeric,                          -- 竞彩官方结算 SP
    p_model    numeric,
    hit        boolean,                          -- 是否命中
    stake      numeric NOT NULL DEFAULT 2,       -- 战绩统计用的固定注额（元/注）
    payout     numeric NOT NULL DEFAULT 0,       -- 命中时 = stake × sp_settle
    pnl        numeric NOT NULL DEFAULT 0,       -- payout − stake
    settle_source text,                          -- win_flag | score
    settled_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT rec_settle_option_check CHECK (option_code IN ('h', 'd', 'a')),
    CONSTRAINT rec_settle_stake_check CHECK (stake >= 0),
    CONSTRAINT rec_settle_unique UNIQUE (match_id, option_code)
);
CREATE INDEX IF NOT EXISTS rec_settle_date_idx ON analysis.rec_settle (match_date);
CREATE INDEX IF NOT EXISTS rec_settle_league_idx ON analysis.rec_settle (league_key, match_date);

COMMIT;

-- ── 授权（与既有 analysis.* 保持一致）───────────────────────────────
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'league_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON analysis.rec_settle TO league_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON analysis.ev_report TO league_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'league_ro') THEN
    GRANT SELECT ON analysis.rec_settle TO league_ro;
    GRANT SELECT ON analysis.ev_report TO league_ro;
  END IF;
END $$;

-- 核验（应返回 2 行 + 7 列）
-- SELECT table_name FROM information_schema.tables
--  WHERE table_schema='analysis' AND table_name IN ('ev_report','rec_settle');
-- SELECT column_name FROM information_schema.columns
--  WHERE table_schema='analysis' AND table_name='ev_report' ORDER BY ordinal_position;
