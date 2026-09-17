# 伤停、阵容与交锋数据源边界

## 当前状态

当前 league-v2 预测链路已接入 football-data.org、API-Football 和 ESPN 的既有预测/赛果读取，但尚未接入可靠的赛前伤停、确认首发/阵容或专用 H2H 服务。因此 Dashboard 继续显示“不可用”，不从缺失字段推断球员健康、首发或交锋结论。

## 候选数据源

| 数据源 | 伤停 | 首发/阵容 | H2H | 适用性 |
|---|---|---|---|---|
| API-Football / API-Sports | 有专用接口 | 有比赛 lineups | 有 head-to-head | MVP 低门槛；免费额度有限，需核对联赛覆盖与授权 |
| Sportmonks | injuries/suspensions | lineups、predicted lineups | H2H | 联赛范围固定且重视阵容时优先评估，按套餐收费 |
| Sportradar Soccer v4 | missing/injured feeds | lineup feeds | competitor-vs-competitor | 生产级质量和实时能力，商务报价，需确认中超覆盖 |
| football-data.org | 无稳定专用伤停接口 | 字段覆盖随联赛变化 | 部分比赛含 H2H 信息 | 适合低成本赛程/赛果基线，不作为伤停主源 |
| CFA/CFL 官方页面 | 公告/比赛报告 | 可能有官方信息 | 可人工核验 | 权威但未发现稳定公开 API，不直接抓取 |

参考：

- [API-Football 文档](https://www.api-football.com/documentation-v3)
- [API-Football 价格](https://www.api-football.com/pricing)
- [Sportmonks Football 文档](https://docs.sportmonks.com/football)
- [Sportradar Soccer Overview](https://developer.sportradar.com/soccer/reference/soccer-overview)
- [Sportradar 阵容与伤停说明](https://developer.sportradar.com/soccer/docs/soccer-ig-rosters-lineups-transfers)
- [football-data.org 文档](https://www.football-data.org/documentation/api)
- [中国足协](https://www.thecfa.cn/)
- [中国足球职业联赛](https://www.cfl-china.cn/)

## 接入前硬条件

1. 验证目标联赛覆盖、provider fixture/team/player ID 映射和字段完整率；
2. 保存 `provider`、原始 ID、`observed_at`、来源和数据状态，区分 confirmed、probable、unknown；
3. 首发仅在 provider 明确标记 confirmed 后展示；没有数据不能展示为“全员健康”；
4. 伤停按 injury/suspension 原因和更新时间展示，超时数据标记 stale，不继续伪装为最新；
5. H2H 按两队 canonical ID 缓存，展示样本窗口、比赛日期和来源；
6. 付费或配额型源默认关闭，配置和授权确认前不发请求、不写入 raw/fact；
7. 中国公开网页只作为人工核验入口，未经授权不做爬虫或再分发。

## 推荐路线

先用 API-Football 做小范围、只读、缓存验证；若目标是固定联赛且阵容/伤停是核心，再评估 Sportmonks；若需要生产 SLA、实时推送和更高可靠性，再进入 Sportradar 商务评估。任何 provider 接入必须单独设计契约、限速、缓存、回退和测试，不直接把外部字段塞进当前预测摘要。
