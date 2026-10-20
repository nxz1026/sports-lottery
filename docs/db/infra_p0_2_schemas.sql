-- ============================================================
-- P0-infra ②：ref / stg / ops 三 schema（对 league 库执行，postgres 超级用户跑）
-- 定稿 2026-09-15（队长）。契约冻结前只到此为止：fact/model/analysis 与 v.* 视图不建。
-- 与 schema_v2_draft.sql 的差异：
--   E stg 七表草案里是 `(line jsonb, …)` 占位 → 统一补全为 4 列并加 src_hash 主键
--     （幂等装载的落点：store 验收要求"同一文件装两遍 count(*) 不变"，靠 ON CONFLICT DO NOTHING）
--   F 新增 ops.file_arrival：支撑《实施计划》P0-infra 的"topic 超 24h 无 .done 报警"新鲜度监控
--   G ref.jc_match_num.fixture_id 不建外键（fact.fixture 本阶段不存在），列类型保持 bigint
--   H 不建任何视图：v.market_full 在草案里就是 `…` 伪代码，等 fact/model 落地一起出
--   I 【实测教训】授权必须**先于**建表执行：ALTER DEFAULT PRIVILEGES 只影响语句之后创建的对象，
--     本文件第一版把授权写在末尾 → 已建表一条权限都没拿到。现三段式：schema → 默认权限 → 建表 → 存量补授。
-- 本文件不 seed ref.league：联赛↔各源 id 映射属 P0-store/backfill 的职责（要用真实报文核）
-- 可重复执行（IF NOT EXISTS + GRANT 幂等）。
-- ============================================================
\set ON_ERROR_STOP on

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS ref;
CREATE SCHEMA IF NOT EXISTS stg;
CREATE SCHEMA IF NOT EXISTS ops;
GRANT USAGE ON SCHEMA ref, stg, ops TO league_ing, league_app, league_ro;

-- ---------- 默认权限（必须在 CREATE TABLE 之前，见差异 I）----------
ALTER DEFAULT PRIVILEGES IN SCHEMA ref            GRANT SELECT, INSERT, UPDATE ON TABLES TO league_ing;
ALTER DEFAULT PRIVILEGES IN SCHEMA stg            GRANT SELECT, INSERT, TRUNCATE ON TABLES TO league_ing;
-- ops 必须含 UPDATE：quota_ledger 的设计写法是 INSERT … ON CONFLICT DO UPDATE SET used=used+1，
--   只授 INSERT 会 permission denied（2026-09-15 实测踩到，权限矩阵跑出来的）。
ALTER DEFAULT PRIVILEGES IN SCHEMA ops            GRANT SELECT, INSERT, UPDATE ON TABLES TO league_ing;
ALTER DEFAULT PRIVILEGES IN SCHEMA ref, stg, ops  GRANT SELECT ON TABLES TO league_app, league_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA ops            GRANT USAGE, SELECT ON SEQUENCES TO league_ing;

-- ---------- ref 参考层 ----------
CREATE TABLE IF NOT EXISTS ref.league (
  league_key   text PRIMARY KEY,                 -- epl/laliga/seriea/bundesliga/ligue1/csl/cl/wc/nba/cba
  cn_name      text NOT NULL,
  sport        text NOT NULL CHECK (sport IN ('football','basketball')),
  scope        text NOT NULL CHECK (scope IN ('regular','irregular')),
  window_from  date, window_to date,
  ids          jsonb NOT NULL DEFAULT '{}',      -- {api_football:39, football_data:'PL', espn:'…', sporttery:'…'}
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ref.team (
  team_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sport     text NOT NULL,
  name_cn   text NOT NULL,
  aliases   jsonb NOT NULL DEFAULT '{}',         -- {api_football:"Man City", sporttery:"曼城", …}
  UNIQUE (sport, name_cn)
);
CREATE TABLE IF NOT EXISTS ref.jc_match_num (
  jc_match_id  text PRIMARY KEY,                 -- '20260915-w3-001' 规范化
  display_num  text NOT NULL,                    -- '周三001'
  fixture_id   bigint,                           -- fact.fixture 落地后再补外键；对齐失败置 NULL 待人工
  sell_date    date NOT NULL,
  play_types   text[] NOT NULL                   -- 当日开售玩法 ['had','hhad','crs','ttg','haf']
);
CREATE TABLE IF NOT EXISTS ref.jc_issue (
  issue_no   text PRIMARY KEY,
  game       text CHECK (game IN ('14c','rq','bqc','jqc')),
  matches    jsonb NOT NULL,
  sales_from timestamptz, sales_to timestamptz, draw_at timestamptz,
  status     text NOT NULL DEFAULT 'onsale'      -- onsale/drawn/void
);

-- ---------- stg 国内机装载区（JSONL 原样落位，UNLOGGED 可重放，主键 src_hash 保幂等）----------
CREATE UNLOGGED TABLE IF NOT EXISTS stg.jczq_offer     (line jsonb NOT NULL, src_hash text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now(), src_file text NOT NULL, PRIMARY KEY (src_hash)) TABLESPACE ts_stg;
CREATE UNLOGGED TABLE IF NOT EXISTS stg.jczq_result    (line jsonb NOT NULL, src_hash text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now(), src_file text NOT NULL, PRIMARY KEY (src_hash)) TABLESPACE ts_stg;
CREATE UNLOGGED TABLE IF NOT EXISTS stg.jc_issue       (line jsonb NOT NULL, src_hash text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now(), src_file text NOT NULL, PRIMARY KEY (src_hash)) TABLESPACE ts_stg;
CREATE UNLOGGED TABLE IF NOT EXISTS stg.jc_issue_result(line jsonb NOT NULL, src_hash text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now(), src_file text NOT NULL, PRIMARY KEY (src_hash)) TABLESPACE ts_stg;
CREATE UNLOGGED TABLE IF NOT EXISTS stg.jclq_offer     (line jsonb NOT NULL, src_hash text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now(), src_file text NOT NULL, PRIMARY KEY (src_hash)) TABLESPACE ts_stg;
CREATE UNLOGGED TABLE IF NOT EXISTS stg.jclq_result    (line jsonb NOT NULL, src_hash text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now(), src_file text NOT NULL, PRIMARY KEY (src_hash)) TABLESPACE ts_stg;
CREATE UNLOGGED TABLE IF NOT EXISTS stg.lottery_draw   (line jsonb NOT NULL, src_hash text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now(), src_file text NOT NULL, PRIMARY KEY (src_hash)) TABLESPACE ts_stg;

-- ---------- ops 运行留痕 ----------
CREATE TABLE IF NOT EXISTS ops.ingest_log (
  id bigserial PRIMARY KEY, topic text, src_file text, rows_in int, rows_ups int,
  rejected jsonb, ok boolean, at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ops.quota_ledger (
  day date NOT NULL, source text NOT NULL, used int NOT NULL DEFAULT 0, cap int NOT NULL,
  PRIMARY KEY (day, source)
);
CREATE TABLE IF NOT EXISTS ops.file_arrival (
  topic text NOT NULL, src_file text NOT NULL, arrived_at timestamptz NOT NULL DEFAULT now(),
  bytes bigint, rows int, done_marker boolean NOT NULL DEFAULT false, PRIMARY KEY (topic, src_file)
);
-- R48：marker 处理水位台账（装载器增量水位）。
-- 为什么需要：ingest_log / file_arrival 记的是「文件级」留痕，但**批级**是否已处理没有台账
--   ⇒ 装载器每轮只能全量重扫全部 .done（实测 2601 个，越滚越大），单轮耗时超 timeout 420
--   ⇒ 每轮被砍在队尾前，新批次永远补不上（实测 fact.jc_offer 新鲜度落后 17h）。
-- 口径：marker 名（.done 文件名）为主键；ok=false 的批次不记 ⇒ 下轮会重试。
CREATE TABLE IF NOT EXISTS ops.ingest_marker (
  marker text PRIMARY KEY,
  processed_at timestamptz NOT NULL DEFAULT now(),
  ups int NOT NULL DEFAULT 0,
  ok boolean NOT NULL DEFAULT true
);

-- ---------- 存量补授（首次执行时上面默认权限已覆盖；重跑/历史对象靠这段）----------
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA ref TO league_ing;
GRANT SELECT, INSERT, TRUNCATE ON ALL TABLES IN SCHEMA stg TO league_ing;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA ops TO league_ing;
GRANT SELECT ON ALL TABLES IN SCHEMA ref, stg, ops TO league_app, league_ro;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops TO league_ing;

-- ---------- 横向隔离（见同目录 pg_hba_league_snippet.txt）----------
-- 【实测教训】`REVOKE CONNECT ON DATABASE 其它库 FROM league_ing…` **无效**：
--   PG 的权限是"角色直授 ∪ 所属角色 ∪ PUBLIC"的并集，从单个角色 REVOKE 掉来自 PUBLIC 的隐式 CONNECT
--   并不会生效（文档明说这类 revoke 不改来自 PUBLIC 的授权）。
--   正确做法＝pg_hba 加一条 `host league league_ing,league_app,league_ro 127.0.0.1/32 scram-sha-256`
--   放在 catch-all 之前（首条匹配即生效），且**不去动其它库的 PUBLIC 授权**——
--   longkonglong 是在跑的另一项目，REVOKE … FROM PUBLIC 会把它一起打死。
