-- ops 保留策略清理（2026-10-23）
--
-- 为什么需要：ops.ingest_log 累计 1208 万行 / 1740 MB 且还在长 ——
--   实测最近 24h 写 10201 行，**其中没有一个新文件** ⇒ 全是同一批老文件被
--   **每 10 分钟重装一遍**（每小时 402 行 / 55 个文件）。根因：ops.ingest_marker
--   里有 10 个 ok=false 的 marker（水位只认 ok=true ⇒ 每轮都当待处理重跑）。
--   那 10 个批失败的原因 = 盘上的历史坏文件 JSONDecodeError（已隔离）。
--   ⇒ 治本是清坏文件 + 修 marker 判定；**本文件是兜底**：给 ops 三张表加窗口。
--
-- ⚠️⚠️ **最重要的一条：台账窗口必须 > 落港窗口（30 天）** ——
--   2026-10-23 实测踩过的坑（不是假想，是真跑出来的）：
--   本文件第一版把 ``ops.file_arrival`` 也按 keep_days=7 删 ⇒ 而落港目录保留 **30 天**
--   ⇒ **台账比文件先消失** ⇒ 装载器 ``_orphan_files`` 判「盘上有、但没装载记录」
--   ⇒ 把这批文件当**孤儿**（未装载）⇒ **每轮全部重装**！
--   实测：keep_days=7 跑完的下一轮 cron，**4295 个文件被重装**，
--   ingest_log 当场多了 4295 行、每轮多做一遍全量 IO。
--   （侥幸没造成数据损坏：重装命中「同 hash 幂等跳过」，实测
--     fact.jc_issue_prize 191 行仍全部 is_current=true、业务表行数不变；
--     但这是运气好，不是设计对。）
--   ⇒ **口径拆成两个窗口**：
--       · keep_days       = **7**  → ops.ingest_log（纯日志：只留每文件最后一条结论）
--       · ledger_keep_days = **37** → ops.file_arrival + ops.ingest_marker
--         （37 > 落港的 30 ⇒ 台账永远比它记录的文件活得久 ⇒ 不会变成"孤儿"）
--   ⚠️ 这个不等式是**硬约束**：ledger_keep_days <= 落港保留天数 ⇒ 孤儿重装必然复发。
--     tools/check_docs_claims.py 会**解析本文件与 docs/infra/jc-staging-retention.sh
--     的两个默认值来断言它**（不许靠注释写一句就算数）。
--
-- ⚠️ 口径（**窗口 + 去重**，不是无脑删）：
--   ① 最近 keep_days 天（默认 **7**）**全部行**保留 —— 排障要看当天每一条；
--   ② 更早的行：**每个 (topic, src_file) 只留最后一条** ——
--      「这个文件最后装成什么样」不能丢（它就是该文件的最终结论）；
--   ③ file_arrival / ingest_marker：按 ledger_keep_days（**37**）过期即可删
--      （必须 > 落港窗口，见上）；
--   ④ ingest_marker：**只删 ok=true 且过期**的 ——
--      ok=false 的**必须留着**（它们是「下轮要重试」的批次，
--      删了 = 把失败批次当已处理 ⇒ 数据静默缺失）。
--
-- ⚠️ **为什么 ingest_log 默认 7 天**（2026-10-23 实测）：
--   治本前（10-15~10-19）每天写 **75~80 万行**，全是「每 10 分钟全量重装」的重复噪音
--   （每天重复 5000+ 个文件）⇒ 14 天窗口里光这 5 天就占 **390 万行**。
--   治本后（marker 水位全 ok）实测每天只写 **~550 行** ⇒ 7 天窗口足够覆盖一周排障。
--   ⇒ 7 天 = 噪音清干净 + 排障够用。
--   （10-16~10-19 的历史噪音会随窗口自然滑出：实测每天少 ~76 万行，
--     到 10-26 这批彻底干净，表规模稳定在 ~2 万行量级。）
--
-- ⚠️ **只删 ops 台账，绝不碰业务表**（fact.* / analysis.* 一律不动）。
-- ⚠️ **必须由 postgres 执行**：league_ing 只有 INSERT/SELECT/UPDATE，**没有 DELETE**。
-- ⚠️ DELETE 不回收磁盘 ⇒ 删完要 **VACUUM**（脚本已自动带上）。
--
-- 用法：
--   psql -v keep_days=7 -f docs/db/ops_retention.sql              # 报告（不删）
--   psql -v keep_days=7 -v apply=1 -f docs/db/ops_retention.sql   # 真删
--   # 低峰期想把空间还给 OS（VACUUM FULL 会阻塞装载）：
--   psql -v keep_days=7 -v apply=1 -v vacuum_full=1 -f docs/db/ops_retention.sql

\if :{?apply}
\else
\set apply 0
\endif

\if :{?vacuum_full}
\else
\set vacuum_full 0
\endif

\if :{?keep_days}
\else
\set keep_days 7
\endif

-- ⚠️ 台账窗口（file_arrival / ingest_marker）—— **必须 > 落港保留天数(30)**，
--    否则台账比文件先消失 ⇒ 老文件被当孤儿重装（2026-10-23 实测：4295 个文件）。
\if :{?ledger_keep_days}
\else
\set ledger_keep_days 37
\endif

-- ① 报告（永远先打，让操作者看见「要删多少」）
SELECT 'ops.ingest_log' AS tbl,
       count(*) AS total,
       count(*) FILTER (
         WHERE at < now() - (:keep_days || ' days')::interval
           AND id NOT IN (SELECT max(l2.id) FROM ops.ingest_log l2
                          GROUP BY l2.topic, l2.src_file)
       ) AS will_delete
FROM ops.ingest_log
UNION ALL
SELECT 'ops.file_arrival',
       count(*),
       count(*) FILTER (WHERE arrived_at < now() - (:ledger_keep_days || ' days')::interval)
FROM ops.file_arrival
UNION ALL
SELECT 'ops.ingest_marker',
       count(*),
       count(*) FILTER (WHERE ok AND processed_at < now() - (:ledger_keep_days || ' days')::interval)
FROM ops.ingest_marker;

\if :apply
BEGIN;

-- ② ingest_log：窗口内全留 + 更早的每文件留最后一条
--    ⚠️ 必须用 id 判定「是否最后一条」（id 单调 ⇒ max(id) = 最后写入的那条）。
DELETE FROM ops.ingest_log l
 WHERE l.at < now() - (:keep_days || ' days')::interval
   AND l.id NOT IN (SELECT max(l2.id) FROM ops.ingest_log l2
                     GROUP BY l2.topic, l2.src_file);

-- ③ file_arrival：每个文件唯一一行 ⇒ 过期即可删
--    ⚠️ 窗口是 ledger_keep_days（37），**不是 keep_days（7）** ——
--       它是「已装载」台账，必须比它记录的文件（落港保留 30 天）活得久。
DELETE FROM ops.file_arrival
 WHERE arrived_at < now() - (:ledger_keep_days || ' days')::interval;

-- ④ ingest_marker：**只删 ok=true**（ok=false 是「要重试的批次」，删了就丢数据）
--    ⚠️ 同样用 ledger_keep_days：水位必须比 .done 文件（落港保留 30 天）活得久，
--    否则该批会被当成新批次重装 ⇒ 又是每 10 分钟重复写 ingest_log。
DELETE FROM ops.ingest_marker
 WHERE ok AND processed_at < now() - (:ledger_keep_days || ' days')::interval;

COMMIT;

-- ⚠️ **DELETE 不回收磁盘空间** ⇒ 必须 VACUUM，否则表尺寸原地不动
--    （实测：删掉 447 万行后 pg_total_relation_size 仍是 1740 MB）
--    ANALYZE 更新统计信息（否则 planner 可能用错行数）。
--
--    默认 **普通 VACUUM**：只把空间标记为可重用（文件不变大），
--    不拿 ACCESS EXCLUSIVE 锁 ⇒ **可在生产随时跑**。
--    ``-v vacuum_full=1`` 改用 **VACUUM FULL**（真正把空间还给 OS，
--    实测 1740 MB → 418 MB），但它要 **ACCESS EXCLUSIVE 锁**
--    ⇒ 会**阻塞每 10 分钟一次的装载** ⇒ 只在低峰期/手动跑。
\if :vacuum_full
\echo '--- VACUUM FULL：会拿 ACCESS EXCLUSIVE 锁，阻塞装载，仅限低峰期 ---'
VACUUM (FULL, ANALYZE) ops.ingest_log;
VACUUM (FULL, ANALYZE) ops.file_arrival;
VACUUM (FULL, ANALYZE) ops.ingest_marker;
\else
VACUUM (ANALYZE) ops.ingest_log;
VACUUM (ANALYZE) ops.file_arrival;
VACUUM (ANALYZE) ops.ingest_marker;
\endif

\echo '--- 清理后规模（含磁盘）---'
SELECT 'ops.ingest_log' AS tbl, count(*) AS total,
       pg_size_pretty(pg_total_relation_size('ops.ingest_log')) AS size
FROM ops.ingest_log
UNION ALL SELECT 'ops.file_arrival', count(*),
       pg_size_pretty(pg_total_relation_size('ops.file_arrival')) FROM ops.file_arrival
UNION ALL SELECT 'ops.ingest_marker', count(*),
       pg_size_pretty(pg_total_relation_size('ops.ingest_marker')) FROM ops.ingest_marker;
\endif
