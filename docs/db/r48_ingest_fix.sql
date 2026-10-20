-- =============================================================================
-- R48 迁移：体彩装载器 124（timeout）根因修复所需的库端变更
--
-- 背景（2026-10-20 Oracle 实测）：
--   cron `*/10` 跑 jc-ingest-run.sh → `timeout 420 python scripts/ingest/jc_load.py`
--   ⇒ 装载器非零退出 124。根因三条叠加：
--     ① fact.jc_issue_prize 撞唯一键 ⇒ IntegrityError 上抛 ⇒ load_batch 整批 rollback
--        （实测 2026-10-01T14:30Z 起 2580 批里 1805 批（70%）回滚，最新批单独试跑也撞）
--     ② 装载器无增量水位 ⇒ 每轮全量重扫 2601 个 .done ⇒ 420s 装不完 ⇒ 队尾永远补不上
--        （实测 fact.jc_offer 最新 snap_ts 落后 17h）
--     ③ orphan 阶段每轮重装 15,816 个老文件（98% 冗余 IO）
--   代码侧修复见：scripts/ingest/jc_issue_write.py、jc_topic.py、jc_load.py。
--
-- 本脚本做两件事（均可重复执行）：
--   1) fact.jc_issue_prize 的 PK 放宽到含 src_hash —— 让 DDL 注释里 v1.2 的
--      「is_current 翻版」在结构上成立（同 tier 可并存多版本，历史行保留）。
--   2) 新增 ops.ingest_marker（marker 处理水位台账）—— 装载器增量水位。
--
-- 执行：sudo -u postgres psql -d league -f docs/db/r48_ingest_fix.sql
-- 校验：见文件末尾第 3 节。
-- =============================================================================

BEGIN;

-- ---------- 1) fact.jc_issue_prize：PK 放宽（含 src_hash） ----------
-- 前提：同 (game_num, issue_no, tier_no, src_hash) 不得有重复行，否则 ADD PK 会失败。
-- 当前实测 167 行 / 167 个不同 tier ⇒ 无重复；但脚本仍做一次防御性清理
-- （每个同键组只留一行：优先留 is_current=true 的，同为 true 则留 ctid 最新的那条；
--   fact 层 league_ing 无 DELETE 权限，这里由 postgres 执行；
--   重复行只会来自历史误重装，删掉不影响有效数据）。
DELETE FROM fact.jc_issue_prize
 WHERE ctid IN (
   SELECT ctid FROM (
     SELECT ctid,
            row_number() OVER (PARTITION BY game_num, issue_no, tier_no, src_hash
                               ORDER BY is_current DESC, ctid DESC) AS rn
       FROM fact.jc_issue_prize
   ) d
   WHERE d.rn > 1
 );

ALTER TABLE fact.jc_issue_prize DROP CONSTRAINT IF EXISTS jc_issue_prize_pkey;
ALTER TABLE fact.jc_issue_prize
  ADD CONSTRAINT jc_issue_prize_pkey PRIMARY KEY (game_num, issue_no, tier_no, src_hash);

COMMENT ON TABLE fact.jc_issue_prize IS
  '传统足彩奖级（源：jc_issue_result.*Detail.prizeLevelList）。R48：PK 含 src_hash ⇒ 同 tier 可并存多版本；'
  'is_current=true 为当前版本（旧版本置 false 保留），src_hash 是 payload 内容哈希（不含 snap_ts）。';

-- ---------- 2) ops.ingest_marker：装载器水位台账 ----------
CREATE TABLE IF NOT EXISTS ops.ingest_marker (
  marker text PRIMARY KEY,
  processed_at timestamptz NOT NULL DEFAULT now(),
  ups int NOT NULL DEFAULT 0,
  ok boolean NOT NULL DEFAULT true
);
COMMENT ON TABLE ops.ingest_marker IS
  '装载器批级水位：.done marker 已处理台账。ok=false 的批次下轮重试；装载器只处理不在此表的新批次。';

-- 授权：ing 角色需要 SELECT/INSERT/UPDATE（DEFAULT PRIVILEGES 已覆盖新建表，
-- 这里再显式兜一层，与本文件其它 GRANT 段同风格）
GRANT SELECT, INSERT, UPDATE ON ops.ingest_marker TO league_ing;
GRANT SELECT ON ops.ingest_marker TO league_app, league_ro;

COMMIT;

-- ---------- 3) 校验（预期输出见注释） ----------
-- 3.1 PK 已是四列
-- SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
--  WHERE conrelid = 'fact.jc_issue_prize'::regclass AND contype = 'p';
--   预期：jc_issue_prize_pkey | PRIMARY KEY (game_num, issue_no, tier_no, src_hash)
-- 3.2 新表已建、授权已给
-- SELECT relname FROM pg_class WHERE relname = 'ingest_marker';
--   预期：ingest_marker
-- SELECT has_table_privilege('league_ing', 'ops.ingest_marker', 'INSERT');
--   预期：t
-- 3.3 装载器可读水位（ing 角色视角）
-- SET ROLE league_ing; SELECT count(*) FROM ops.ingest_marker; RESET ROLE;
--   预期：0（首次运行前为空）
