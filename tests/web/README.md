# `tests/web/` 索引

> web 侧（FastAPI + 接口层 + 应用层）的回归测试与契约守护。
> 测试文件按"里程碑标签 (M-label) + 功能名"双轴命名：
> - **M-label**（M1 / M2 / M3 / M4 / M6）= 当时立项时的开发里程碑编号，
>   沿袭历史 ticket 编号（`tickets/P0-*.txt` / `docs/web/WO-M*.md`），
>   不再变更（避免 ticket 链接失效）。
> - **功能名**（无 M 前缀）= 后期按功能点添加的专项测试，独立于里程碑。

## M-label → 测试覆盖映射

| M-label | 文件 | 覆盖 | ticket / WO |
|---|---|---|---|
| M1 | `test_m1_auth.py` | 会话认证闭环、login 限速锁、prod 守卫 | `docs/web/WO-M1.md` |
| M2 | `test_m2_api.py` | 业务端点 `require_auth` + 数据结构 + 降级语义 | `docs/web/WO-M2.md` |
| M2 | `test_m2_store.py` | `PredictionStore` 数据结构（验收点 C）+ 降级（验收点 D） | 同上 |
| M3 | `test_m3_jobs.py` | 任务触发 + 配额共享 + 并发/锁 + AI enrich job | `docs/web/WO-M3.md` |
| M4 | `test_m4_config.py` | env 健壮性 + 生产模式默认口令守卫 | `docs/web/WO-M4.md` |
| M6 | `test_m6_ai_enrich.py` | AI 摘要富化（ai-enrich job）+ LLM 兜底 | `docs/web/WO-M6.md` |

> 注意：M5（job 拆分 / APScheduler）是中途撤销的尝试（已改为 systemd timer），
> 故 `tests/web/` 没有 `test_m5_*.py`。

## 功能专项测试（无 M 前缀）

| 文件 | 覆盖 |
|---|---|
| `test_ai_report.py` | `/api/v1/ai/daily` 报告聚合 |
| `test_apifootball.py` | `web/services/apifootball.py` 客户端包装 |
| `test_jc_backtest_api.py` | `/api/jc/backtest` 端点契约 |
| `test_jc_daily_image.py` | `/api/jc/daily-image` 端点契约（含 NBA 表行） |
| `test_jc_lottery_api.py` | `/api/jc/lottery` 端点契约 |
| `test_jc_view_timezone.py` | 时区约定：naive timestamp = BJT 墙钟；带时区标识才做 +8h |
| `test_job_trigger_source.py` | jobs.trigger_* 的 trigger 来源字段（白名单） |
| `test_prediction_traceability.py` | 预测行 ↔ 实际赛果 ↔ AI 富化的稳定主键链路 |
| `test_temp_league_whitelist.py` | `LEAGUE_TEMP_LEAGUES` 环境变量接入 |
| **`test_no_scripts_import.py`** | **契约 §5 静态守护**：web 层顶层不 import `scripts.*` / `ai.*` |

## 跑测试

```bash
# 全部
python -m pytest tests/web -q

# 单独某个里程碑
python -m pytest tests/web/test_m3_jobs.py -q

# 仅契约守护
python -m pytest tests/web/test_no_scripts_import.py -v
```

## 加新测试的约定

- **新端点 / 新行为** → 按"功能专项"命名（`test_<module>.py`），不要
  强行套 M-label（M 编号不再递增）。
- **新里程碑 / 新分支工作**（如 v3.x）→ 新建 `tests/web/test_v3_*.py`
  或子目录 `tests/web/v3/`；M-label 历史包袱不延续。
- **契约守护**（顶部 import / 数据 schema / 时区口径）→ 在
  `test_no_scripts_import.py` 增 case，或新建 `test_<contract>.py`。