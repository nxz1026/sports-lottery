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

## 计划任务（5 个）—— **2026-10-22 起本机已停用，改手动**
| 任务 | 周期 | VBS 参数 | topic |
|---|---|---|---|
| `collector_offer_10m` | 每 2 小时（2026-09-17 由 10 分钟降 1 小时，2026-09-21 再降 2 小时） | `offer` | 7 快 topic |
| `collector_0930/1530/2130/2330` | 每日 4 档 | `night` | 8 topic（含 `jc_odds_history`） |

全部走 `wscript scripts/collector_silent.vbs <mode>`（S4U 无窗口）；单例锁
（`.collector.lock` + `OpenProcess` 探活）保证并发 no-op。

批量脚本：`scripts/collect_batch.bat offer|night`；计划任务走 `wscript+scripts/collector_silent.vbs [offer|night]`（无窗口）。
- `offer`（默认）= 7 快 topic（jczq_offer jclq_offer jczq_result jclq_result jc_issue jc_issue_result lottery_draw），`collector_offer_10m` 专用
- `night` = 8 topic（offer 7 + jc_odds_history），4 个 daily 档（09:30/15:30/21:30/23:30）专用
- `jc_odds_history` 单批 ~60-75s（27 场在售 QPS=1.0），只在 daily 档跑，防网络挂起时快批堆积僵尸 python（2026-09-16 死机复盘）
- 计划任务均为"开机即跑/登录即跑"（BootTrigger+LogonTrigger）+ `StartWhenAvailable` 漏档补跑（2026-09-21）
- ⚠️ **原子性（2026-10-23 生产实测修）**：写批次文件与拿单例锁**都必须是 O_EXCL 独占创建**
  （`open(path, "x")`）。原来的「`exists()` → `open(path,"w")`」两步不是原子的 ⇒
  Boot+Logon 把多个任务在同一刻拉起时（实测 21:49 一次拉起 4 个），
  多个批次会**同时拿到锁**、**给同一 topic 选到同一个文件名** ⇒ 两个进程各写一半 ⇒
  **文件内容交织、变成非法 JSON**（远端装载报 `JSONDecodeError`，且坏文件留在盘上反复报错）。
  回归测试见 `tests/test_collector_atomicity.py`（并发写不撞名 + 并发抢锁只有一个赢家）。
- ⚠️ **存活判定必须跨平台（2026-10-23 订正）**：拿锁后要判断「旧 pid 是否还活着」，
  **不许直接写 `ctypes.windll`**（Windows 专有）。采集端跑在 Windows 工作机，但
  `collector.py` 同时是**仓库里的生产代码**，其回归测试会在 **Linux（Oracle）** 上跟着全量
  pytest 一起跑 ⇒ 只认 windll 会让 Oracle 上直接 `AttributeError` 崩掉、测试变红。
  现在走 `_pid_alive()`：**Windows 走 `OpenProcess`**；**POSIX 走 `os.kill(pid, 0)`**
  （只做存在性+权限探测，不发真信号）。
  ⚠️ **Windows 上不能用 `os.kill(pid, 0)`** —— 那边 `os.kill` 对非 `CTRL_*` 信号会调
  `TerminateProcess` ⇒ **会把目标进程杀掉**。

## 手动模式（2026-10-22 起，本机生效）
**采集机是工作电脑，不是 24h 开机** ⇒ 5 个计划任务在本机**已停用**（`Enabled=False`，任务定义保留未删）。
**机制本身完整保留**（任务定义 XML + `collector_silent.vbs` + config 一字未改），随时可恢复。

**为什么值得改**：近 7 天实测（`jczq_offer`，117 次相邻批比较）——
**54% 的批次完全空转**（采回来和上一批一模一样）；07–09 时共 36 批只有 **1 批**有产出（2.8%）；
新增开售（111 场）**全部**落在 11–17 时且一天只有一次「开售潮」；赔率变动集中在赛前 17–24 时。
⇒ 全天 16 批里有一半是纯空转，而用户「不是随时在看数据」。

### 怎么用：双击 `scripts\refresh_now.bat`
一键跑完整条链（**这是主路径**）：

| 步 | 动作 | 落点 |
|---|---|---|
| 1 | `collector.py --push-batch`（8 topic）采集 + 原子推远端 | 落港 `/srv/league-staging/incoming/cn-collector` |
| 2 | `ssh oracle 'bash ~/bin/jc-ingest-run.sh'` | `fact.jc_*` |
| 3 | `ssh oracle '… scripts/decision/recommend.py'` | `analysis.ev_report`（今日推荐单） |
| 4 | `ssh oracle '… scripts/decision/settle.py'` | `analysis.rec_settle`（战绩） |

参数：`refresh_now.bat [fast|full] [nosettle]`
- `fast` = 跳过 `jc_odds_history`（省 ~60s；但「赔率异动」视图不会多一个历史点）
- `nosettle` = 跳过第 4 步
- 日志：`logs\refresh_now.log`（双击后停在 `pause`，不会闪窗）

### 怎么恢复自动：管理员 PowerShell
```powershell
powershell -ExecutionPolicy Bypass -File E:\2026Workplace\Code\collector-cn\scripts\tasks_backup\restore_scheduled_tasks.ps1
```
（用 `20261022-manual-mode\*.xml` 逐字恢复任务定义并启用；恢复前先跑一次 `scripts\collect_batch.bat night` 验证采集链路。）

### 手动模式必须配的新鲜度守门（否则就是新的静默失败）
采集端改手动后，「人忘了先采集就直接出荐单」成了新的静默失败路径：**库里最新 SP 可能还是几天前的，
照样算得出 EV，页面看上去完全正常**。⇒ `scripts/decision/recommend.py` 加了两层闸：
- **全局闸**：全库 `had` 最新快照超 6 小时 ⇒ **拒绝出单**（退出码 3），并打印修复命令；`--allow-stale` 可显式放行（note 里留痕）。
- **逐场闸**：某一场自己的快照超龄 ⇒ 跳过该场（不用旧价算 EV）。
- 页面同步显示「定价赔率快照 X 小时前」，过期标红（`web/services/recommend.py::offer_freshness`，唯一真源在 scripts 层）。

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
| jclq_result | `uniform/basketball/getUniformMatchResultV2.qry` | V2，历史开奖可查。**pageSize 必须 ≥ 100**（2026-10-22 实测：pageSize=30 时接口 `total=42 / pages=2` ⇒ 只取第 1 页 30 场、**漏 12 场真实比赛**；改 100 后一次拿全 42 场） |
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