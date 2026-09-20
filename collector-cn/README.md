# collector-cn（国内采集机）

league-predict 系统国内采集机：取中国体育彩票官方 JSON → 落 JSONL → 推远端 oracle。
实施文档：`F:\国内采集机实施文档-v1.md`（§5 契约为准，不得自创字段名）。

## 环境

- Python ≥ 3.9，仅 stdlib（urllib/json/hashlib/pathlib/subprocess/tarfile）
- 出站 ssh 两条通道（`~/.ssh/config`）：
  - `oracle-league` = `league@140.83.62.161`，专用 keypair `~/.ssh/collector-league`（`IdentitiesOnly`），推送唯一通道（受限 shell，仅放行 `rsync --server` 与 `tar -C <incoming> -xzf -`）
  - `oracle` = `ubuntu@140.83.62.161`，仅运维通道，推送不再使用
- 时钟 Asia/Shanghai；输出时间戳一律 UTC ISO8601 带 Z

## 用法

```bash
python collector.py --probe            # 探针模式（复探用）
python collector.py --collect <topic>  # 采集指定 topic 落 JSONL
python collector.py --collect-all      # 采集全部 8 topic
python collector.py --push-batch <topic>...   # 定时任务主路径：B1 采集 + B2 原子推 + G(A) .done 清单（league key-only tar 流式）
```

## 计划任务（5 个）
| 任务 | 周期 | VBS 参数 | topic |
|---|---|---|---|
| `collector_offer_10m` | 每 1 小时（2026-09-17 降频，原 10 分钟） | `offer` | 7 快 topic |
| `collector_0930/1530/2130/2330` | 每日 4 档 | `night` | 8 topic（含 `jc_odds_history`） |

全部走 `wscript scripts/collector_silent.vbs <mode>`（S4U 无窗口）；单例锁
（`.collector.lock` + `OpenProcess` 探活）保证并发 no-op。

批量脚本：`scripts/collect_batch.bat offer|night`；计划任务走 `wscript+scripts/collector_silent.vbs [offer|night]`（无窗口）。
- `offer`（默认）= 7 快 topic（jczq_offer jclq_offer jczq_result jclq_result jc_issue jc_issue_result lottery_draw），`collector_offer_10m` 专用
- `night` = 8 topic（offer 7 + jc_odds_history），4 个 daily 档（09:30/15:30/21:30/23:30）专用
- `jc_odds_history` 单批 ~60-75s（27 场在售 QPS=1.0），只在 daily 档跑，防网络挂起时快批堆积僵尸 python（2026-09-16 死机复盘）
- 计划任务均为"开机即跑/登录即跑"（BootTrigger+LogonTrigger）+ `StartWhenAvailable` 漏档补跑（2026-09-21）

## 契约 v1.2（远端 2026-09-15 升级：新增 fetched_at 键 + 7 topic 完整推送 + .done 清单）

- 通用行外壳：`{"kind","topic","snap_ts","fetched_at","endpoint","http_status","collector_host","payload","src_hash"}`（v1.2 新增 `fetched_at` = 响应解析完成时刻，UTC 带 Z）
- `fetched_at` 不进入 `src_hash`、不做身份键，不与 `snap_ts` 混用
- `snap_ts` = 请求发出时刻（UTC 带 Z），同批同值；官方更新时间留 payload 原样
- `kind="error"`：`errorCode != "0"` 或 `success != true` 时必须产行（失败响应无 value 键）
- payload 官方键名原样，不许改名/清洗/判奖（如 `lotterySaleEndtime` 少 a、`stakeAmount` 千分位、`result:"3＋,1"` 全角加号、`sectionsNo999:"取消"`）
- `src_hash` = `sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",",":")))`
- 身份键：offer/result = `matchId`；issue/lottery = `(lotteryGameNum, lotteryDrawNum)`
- `jczq_offer`/`jclq_offer`：一行 = 一场 × 一个玩法（had/hhad/crs/ttg/hafu），`options` 整块 + `oddsHistory` 原样
- `jclq_offer`/`jclq_result` 已冻结（v1.2 解除 "unverified-shape" 标记）：篮球 = NBA/CBA（老板 06:35 决定），`jclq_result` 窗口 = 近 7 日（`{today_minus_7}`~`{today}`）；`jclq_offer` 行粒度 = 一场 × 一个玩法（同 §5.1 足球），玩法 `mnl/hdc/hilo/wnm`
- 采集节奏（v1.3）：
  - **offer 快批档**：`collector_offer_10m`，7 topic 齐全（B1，**不含** `jc_odds_history`）；节奏 10 分钟 → 每小时（2026-09-17）→ **每 2 小时**（2026-09-21，数据密度需求降低）
  - **4 daily 档**：09:30/15:30/21:30/23:30 = 全 8 topic（含 `jc_odds_history`）；对应 `collector_silent.vbs night`
  - 每批 = **该档 topic 全部齐全**（.jsonl 或 .empty，缺一个 = 批次缺陷，B1）
- **B2 原子推送**（league key-only，2026-09-20 起）：本地 `tar -czf -`（仅本批文件，arcname=`topic/<file>`）→ ssh `oracle-league` 受限 shell `tar -C <incoming>/cn-collector -xzf -` 直接解到最终位置；`.done` 以最终时间戳名随第二路流推（B2：数据先落位、清单后到）。无 `/tmp`、无 sudo、无 staging/mv（受限 shell 只有 tar 一条命令，无法远端改名 → `.done` 本地先定名）。半程断流 = 本批无 `.done`，海外侧按无主文件丢弃，重推幂等（`__NNN` 序号递增不覆盖）
- **G(A) `.done` 清单**：`.done` 文件内含 manifest，每行 `<topic>/<file>\t<rowcount>\t<sha256-of-row-bytes>`；**相对路径必须带 `topic/` 前缀**（2026-09-16 修复：前缀缺失导致远端 312 个文件"无主"、每批 440 条回退告警）；`.empty` 行数为 0、聚合列空。远端以清单为准（不再用文件名窗口匹配）
- **`__002` 分片**：同分钟重跑/补跑可能追加 `__002`/`__003`，清单是唯一正确映射（旧文件名窗口匹配已死）

## 探针结论（2026-09-15，13 端点全 200）

| topic | 真实接口 | 备注 |
|---|---|---|
| jczq_offer | `uniform/football/getMatchCalculatorV1.qry?channel=c` | 文档的 getMatchListV1.qry 返 HTTP 567 反爬 |
| jczq_result | `uniform/football/getUniformMatchResultV1.qry?...&matchPage=1&pcOrWap=1` | 文档的 getMatchResultV1.qry 恒空 |
| jc_issue | `lottery/getFootBallConcernV1.qry?param=<gameKey>,0` | gameKey=90/900129/98/94 |
| jc_issue_result | `lottery/getFootBallDrawInfoV2.qry?isVerify=1&param=94,0;90,0;98,0` | 含奖级/销量/滚存 |
| jclq_offer | `uniform/basketball/getMatchCalculatorV1.qry?channel=c` | 彩种已停售，value 提示停止销售 |
| jclq_result | `uniform/basketball/getUniformMatchResultV2.qry` | V2，历史开奖可查 |
| lottery_draw | `lottery/getHistoryPageListV1.qry?gameNo=<no>&provinceId=0&isVerify=1&termLimits=30` | gameNo=85/35/350133/04（文档 3501/3502 无数据） |
| jc_odds_history | `uniform/football/getOddsHistoryV1.qry?channel=c&matchId=<id>` | 新（v1.3）：一行 = 一场在售足球 × 整条赔率走势（6 块 + 元键原样），matchId 取自 getMatchCalculatorV1 的 Selling 场次；定时 8-topic 批 |

## 推送通道（league key-only，2026-09-20 切换）

- 远端 `incoming/cn-collector/` 属 `league:league`；推送专用 keypair `~/.ssh/collector-league`（ed25519，comment `ND-PC-WIN league cn-collector`），ssh config host `oracle-league`（`IdentitiesOnly`）。
- oracle `league` 账号受限 shell `/usr/local/bin/league-rsync-shell`：白名单 = `rsync --server` + `tar -C <incoming 子目录> -xzf -`（2026-09-20 扩白，海外侧已确认保留；备份 `/tmp/league-rsync-shell.bak-202609210623`）。`authorized_keys` 公钥行带 `command=…restrict,no-*` 前缀（已锁定最终形态）。
- 数据流：`tar -czf - | ssh oracle-league "tar -C … -xzf -"`；`.done` 同一通道第二路流。全程无 ubuntu/sudo。

## 运维

- 计划任务 XML 导出/改/`Register-ScheduledTask -Force` 覆盖（2026-09-21 节奏调整即此法；改前备份原 XML）。
- 诊断日志：`logs/collector_<mode>.log`（`--mode-log` tee，不入 git）。
- 交接文档：`F:\给国内机-交接-league-pubkey-20260920.md`（公钥 + 白名单 + 海外侧确认记录）。

## 红线

- QPS ≤ 1，重试 3 次指数退避（5/15/45s）
- 只 GET 公开 JSON；不登录、不存 cookie、不解析 HTML
- 单批 0 行也产 `.empty` 标记；文件追加写禁止覆盖历史批
- 行内 `src_hash` = 原始 JSON sha256（幂等键）