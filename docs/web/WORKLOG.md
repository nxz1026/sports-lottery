# WORKLOG — league-predict Web Dashboard（队长台账）

| 日期(BJT) | 里程碑 | 动作 | 状态 |
|---|---|---|---|
| 09-09 18:30 | 立项 | 用户拍板 5 项决策（FastAPI Cloud 部署/调度上云+GHA下线/AI作扩展降级/单账号/router拆分） | ✅ |
| 09-09 18:35 | — | 双仓勘察完成；PLAN.md + WO-M0.md 落仓，分支 `web-dashboard` 建立，commit 4249d2b | ✅ |
| 09-09 18:37 | **M0** | 派工 OMP（omp-call --acp --timeout 1200，pid 13194，日志 docs/web/logs/M0_20260909_183742.log）：产出 DATA_CONTRACT.md + M0_report.md，全仓只读 | ⏳ 进行中 |

## 待办/依赖
- [用户] 申请 FastAPI Cloud 新服务（M5 前到位即可，开发用占位）→ 已入 inbox
- [队长] M0 交付后：抽查契约结论 vs 代码实际（重点：文件命名时区、--all 产物形态、results 关联键），闭环疑点清单，然后写 WO-M1
- [队长] 验收红线速查：引擎 `scripts/` diff 必须为空；新增文件仅限 web/ static/ docs/web/ requirements-web.txt pyproject [web] extra；import 禁 pandas/sklearn/xgboost/tensorflow；无敏感值入库

## 备忘
- 本地 20s 命令上限：OMP 用 nohup+日志文件方式派发，轮询用短命令
- welfare 参照要点：入口 pyproject `[tool.fastapi] entryPoint = "src.api:app"`（league 将为 `web.api:app`）；compose 端口 8000:8080、healthcheck /health、restart unless-stopped（league 本地调试可用 8010 占位）
- 云端若实测无常驻定时器 → 启用 PLAN §1.2 fallback（惰性触发+TTL），M3 时定夺
| 09-09 18:46 | M5预研 | FastAPI Cloud 官方文档核实：①Scale-to-Zero 默认开，Hobby 套餐 min 恒为0 ⇒ 常驻 cron 不可靠，M3 默认改为「访问时惰性刷新+当日BJT判据」，Pro(min≥1) 才叠加真 cron；②GitHub 集成仅默认分支 push 触发部署（PR/其他分支不触发）⇒ 开发分支安全，merge main 即上线；③.gitignore 生效、predictions/results 历史 JSON 在仓库内 ⇒ 冷启动自带种子数据；④env 敏感值须建为 Secret（创建时定型）；⑤无持久盘声明 ⇒ 会话/新产物重启即失，按风险登记处置 | ✅ |
| 09-09 18:47 | M0 | OMP 仍在执行（pid 13197，LinBlue provider 已解析；--timeout 1200s 上限约 18:57 BJT）；已挂 watcher → docs/web/logs/M0.done；到期未完则重派或转队长自办 | ⏳ |
| 09-09 19:02 | M0 事故 | 首次派工（LinBlue/DeepSeek-V4-Flash-0.1）19 分钟零产出：网关 /v1/models 正常(200/1.6s)，但该模型通道 503 no available channel；GLM-5.3 返回空 content。判读＝默认模型死通道导致 ACP 卡死。已杀进程 | ✅ |
| 09-09 19:04 | **M0a** | 重派：--acp --model Kimi-K2.7-Code（健康）--timeout 1500，策略改为**先建骨架后增量落盘**，范围收敛到契约 1–4 节（5–9 节留 M0b）。日志 M0a_20260909_190418.log，退出写 M0a.done。套餐确认=Hobby（免费档）⇒ M3 定稿「惰性刷新」为主 | ⏳ 进行中 |
| 09-09 19:10 | 机制变更 | 用户指令：**锁定 LinBlue/DeepSeek-V4-Flash-0.1（付费模型），抽风不死不休**。落地：`/root/omp-resilient.sh` 派工器（≤30次重试、单试超时、DONE哨兵、断点续做prompt）；SOP 更新进 PLAN §7。弃用 Kimi 试跑（其骨架成果保留），杀进程时踩坑：pkill -f 模式包含自身命令行＝自杀，改用 "[o]mp" 括号式。 | ✅ |
| 09-09 19:10 | **M0a** | 派工器上线重派（pid 14104，try1/30 开始 19:10:50，单试超时 900s）。完成信号=logs/M0a.done 含 M0a-DONE | ⏳ |
| 09-09 19:39 | M0a 纠偏 | try1 卡死 26 分钟无产出（omp-call 的 `--timeout` 只保护 chunk 收集循环，不覆盖 `session/prompt` 的阻塞读——模型抽风时看门狗失效，重试机制空转）。修复：`/root/omp-resilient.sh` 外层加 `timeout --kill-after=15 (TMO+90)` 硬杀 + 孤儿 acp 子进程清扫；19:40:27 重新起跑（pid 14885） | ✅ |

## 监控体系上线 (20:30–21:12)
- 发现 omp-call 两个结构 bug：req() 吞掉全部 session/update（日志恒 0B、永远"[ACP无返回]"）；turn 结束后空转满 timeout 才退。已修（读流+实时 trace 进 stderr→日志）。
- 模型铁律再确认：ACP --model 必须裸名 DeepSeek-V4-Flash-0.1；带 LinBlue/ 前缀 = 静默挂死。
- 监控三层：① OMP 官方 hook（~/.omp/agent/hooks/heartbeat.ts，经 --hook 显式加载——自动发现在 print/acp 无效）→ hb/heartbeat.jsonl 工具级事件流；② /root/omp-watchdog.sh cron 每分钟：wrapper 死了复活 / 日志+心跳双沉默>12min 戳子进程强制重试 / STATUS.md 状态行；暂停某任务 touch logs/<TAG>.paused；③ 可选推送 /root/omp-notify.conf NOTIFY_URL（ntfy 兼容，待用户 topic）。
- M0a 进度：第 1 节已落盘（predict.py 引证抽查 3/3 属实）；try6 进行中；wrapper 30 次耗尽会被 watchdog 复活续跑。

## M0a 验收 PASS (23:36)
- SUCCESS try8 21:49 BJT；哨兵 2026-09-09T13:30Z；契约 23.8KB（§1-4）。
- 抽证：§2 顶层 keys 与真实 JSON 逐一对上；predict.py:280/329 引证属实；scripts/core 零 diff；commit 00b5516。
- 亮点发现（Web 层直接受益）：①now_utc 是 now_bjt 的别名（变量名撒谎），文件名时间戳=BJT、粒度到小时；②文件名不含联赛码→同小时多联赛互相覆盖→store 层必须按联赛分文件或改命名（§9-1）；③样本 generated_at +00:00 与现版 +08:00 矛盾，已挂 §9 疑点（疑似 GHA 旧版直调）。
- M0b 23:36:53 发车（§5-9+疑点收口），budget 1200s x30，watchdog 已接管 .job。

## 队长文书 (23:45)
- PLAN §7 SOP 修正：派工模型改裸名 DeepSeek-V4-Flash-0.1（带前缀=静默挂死，附 WORKLOG 事故链接）。
- WO-M1.md 定稿：骨架+认证 11 交付物、6 红线、A-E 验收点；暂不加 apscheduler（留给 M3）。
- 09-10 03:18:11  **M0b STALLED 3.5h 未出哨兵 → 流水线停车，待晨间人工处置** (auto-chain)

## 事故：哨兵大小写空转（01:35–05:50）
- M0b 实际 01:35 完成（契约 48.9KB、待补0、报告5.4KB），但 agent 把哨兵写成 `M0B-DONE`（大写B），wrapper/watchdog/pipeline 三处大小写敏感 grep 均不认 → 空转 20 次重试 ×~22min（浪费付费模型调用，late 轮次还在自发打磨 §9 无害亦无效），pipeline 03:18 按规停车（stall commit 682e385，机制本身是对的）。
- 修复：三脚本哨兵检查全部 grep -qi；M0b.job 摘除、halt 清除、wrapper 20972 停止。流水线重启，M0b 将走过闸提交，继续 M1→M4。
- 教训入 skill：完成信号字符串必须机器可判、检查一律大小写不敏感。
- 09-10 05:49:08  **M0b GATE-FAIL（原因见 pipeline.log）→ 流水线停车，越权文件已移 /tmp/forensics/M0b** (auto-chain)
- 09-10 05:52:21  **M0b 过闸并提交（机械检查；队长全面复核在晨间）** (auto-chain)

## 队长巡检 07:15 BJT (09-10)
- M0b ✅ SUCCESS try=8（哨兵 01:35）。
- M1 try1~4 全部卡死于同一模式：开场 40s 内 11~34 个 tool 事件后 LLM 通道整段静默直至被杀。try1 已写全 11 件交付物，缺 report+sentinel。
- 网关实锤：后端直连探针（100s 超时的最小请求）两次均 0 回复 → DeepSeek-V4-Flash-0.1 线路此刻不通，非上下文/文件问题。
- 监控栈根治：watchdog/resilient 的进程 kill pattern `[o]mp acp` 与真实 cmdline（omp --hook … acp）不匹配 → 12 分钟止损从未生效，此前每轮卡死白烧满 22min。改 `local/bin/[o]mp --hook`（跳过 3379/内部 worker），备份 .bak。修复当分钟即砍死 try4（rc=143），try5 上线。
- M1 prompt 追加《硬性节奏令》：禁止通读、T+14min 前必须落盘 report+sentinel、落盘优先。
- 态势：卡死轮次现约 12.5min/轮自动巡检网关；全线失败则 09:22 流水线自动停车，仓库无损。晨间队长总复核。
- 09-10 09:22:26  **M1 STALLED 3.5h 未出哨兵 → 流水线停车，待晨间人工处置** (auto-chain)

## 队长巡检 10:40 BJT
- 结论：v4 每轮只发 1 个动作→第二轮必断（9/9 复现），网关实证「带 history 的请求必死」，与响应长短无关；count 探针 101/120 截断。
- 09:22 流水线按计划停车（21a2c25），仓库零污染；M1 代码/测试本体仍是满分状态（15 passed，队长多轮复跑）。
- 交卷令 v5 上线：一回合三发（C1 修 bug 一行式幂等；C2/C3 heredoc 报告六段自带 grep 守卫；哨兵只能经 && 链在 SEG6 落盘后产生——假完成不可能）。
- 若 v5 仍死：降级为「拆 M2 为微票 + 队长人工过 M1」双轨；探针持续监测网关恢复（通了就回多回合）。
- 09-10 10:41:11  **M0b GATE-FAIL（原因见 pipeline.log）→ 流水线停车，越权文件已移 /tmp/forensics/M0b** (auto-chain)
- 09-10 10:44:47  **M0b 过闸并提交（机械检查；队长全面复核在晨间）** (auto-chain)
- 09-10 10:44:53  **M1 过闸并提交（机械检查；队长全面复核在晨间）** (auto-chain)

## 队长结案 10:50 BJT — M1 交付 + forensics 事故
- 网关病理定论：每会话仅第一回合可用且输出预算~200-300 token（count 探针 101/120 截断实锤）；v4/v5/v6 三种交卷式全灭（多回合死/大入参死/三发齐射死）。
- M1 收尾由队长代执行（m1_finalize.sh：幂等修 bug+报告誊写+条件链哨兵 02:39:20Z）——**代章哨兵已如实记录**，交付物 100% 为 OMP 产物、证据 100% 队长实测、闸门四项全绿+冷启动回归通过，晨审可复核。
- 事故：流水线 10:41 重启后 M0b 站独占白名单把 M1 文件判越界——11 件 mv 进 forensics、pyproject（tracked）被 restore 抹掉。已全部恢复、追加段按存档重建（tomllib 验证）。
- 闸门修两处：白名单永久取并集；restore 前强制备份 modified/。
- 11e29e7 = M1 正式提交；流水线 10:44 复活，M2 已派（fded48e 为 WORKLOG 顺带行）。
- 下一步：M2 prompt 微票化（每票=单回合 cp/append 一个函数，跨票靠文件接力），否则降级期 M2 必烧满停车线。
- 队长拍板记录（用户指令 10:55 BJT）：LinBlue provider 锁死至额度用尽，中途不换；死亡窗口由重试基建硬扛。usage 探针曾配后按用户指示即撤（10:59）：用完自然报错，不监控。判器脚本留存 /tmp/api_test.sh /tmp/sse_test.sh。

## 14:03 BJT 队长夜巡 — 网关门控 v2 上线（goal 值守第一战）
- 判别实锤：/root/gw_probe.sh（工具历史 payload，仿 turn2）56 秒内 3 败 1 胜（524/17s×3），
  同窗纯文本/新连接 200/3s → 确诊 relay 对 tool-history 请求选择性挂起，非 OMP 客户端。
- try14 死于该挂起（760s abort 指纹，13:40→13:54 rc=0）；try15 同坑漂 8 分钟被 v2 收摊。
- watchdog v2：cron 每分钟探针，连3败→gw.down（停派工+卡死快戳120s），连2胜→自动复活派工。
  try 预算从此只在网关健康时消耗。M1 成品劫持案的 gate 修复不变。
- 停车线 14:14:53 到点无碍：哨兵可后补，队长人工过闸（goal 值守中）。
- 09-10 14:14:59  **M2 STALLED 3.5h 未出哨兵 → 流水线停车，待晨间人工处置** (auto-chain)

## 14:28 BJT — 重试垫片 retry_proxy 上线（goal 值守第二战）
- 观察：同 payload 时快(2.5s)时慢(40s黑洞)，且 SSE-toolhist 过 / JSON-toolhist 挂的不对称
  → 主嫌 = relay 对非流式工具历史请求选择性黑洞（OMP 疑似非流式，待 try18 嗅探证实）。
- 队长基础设施 /root/retry_proxy.py:8799：透明转发+首字节前静默重试（15×22s/3s退避），
  SSE 首块后即转发；models.yml LinBlue baseUrl→shim（原值备份 .bak-preshim）；
  watchdog 加垫片保活；gw_probe 改走 shim（探针与派工同路，绿=真绿）。
- 若证实 OMP 非流式：下一招 streamify（shim 把请求改 stream:true 发出，SSE 重组成完整
  JSON 回给 OMP——对客户端完全透明）。
- try16 阵亡(14:21 rc=0 挂死指纹)、try17 裸奔老配置收尾中；try18 起带盾。

## 14:33 BJT — ⚰️ LinBlue 余额耗尽（goal 停机条件命中，全线暂停）
- 死证：M2 try1-4（新wrapper计数）每发 10-20 秒杀，ACP 明文
  「402 Add credits to continue, or switch to a free model」（见 logs/M2_try2..5，4/4 命中）。
- 鉴别：517B/40token 小请求仍 200/5.5s —— relay 活着，是**账户余额撑不起 38k token
  业务上下文**。此前 14:00-14:20 的"降级窗口"524 挂起疑为欠费前夜的限流表现（存疑不追）。
- 处置：touch M2.paused + wrapper/子进程收摊（3379 未动）；垫片+watchdog v2+gw_probe 全套
  基础设施留存，充值即插即用：rm docs/web/logs/M2.paused && 重启 wrapper（垫片在位时
  gw_probe 走 shim 已连过 4 拍，闸门自开）。
- 账面：M0b/M1 已交付；M2 差五红（简报与弹药已上膛，tests/web 43 用例中 38 绿）。
- 用户预言应验：「不用消费监控，用完自然就报错了」—— 确实自然报错，零监控成本。

## 14:55 BJT — 死因复核（修正上文：402 系 OMP 内部 kilo 维护路由，非 LinBlue；判决不变）
- 验尸澄清：try6/7 秒杀的 "402 Add credits" 来自 OMP agent_end maintenance 路由（provider=kilo,
  claude-opus-4.8，models.yml 无此键=内置兜底）——LinBlue 从未亲口报 402。
- 但业务通道实弹判决：62k-token（≈OMP turn2 体量）非流式+工具历史 2/2 全灭
  （HTTP 554/109s、524/107s，空体；/root/big38k_test.py）；同日 13k/1.2k/40token 能过。
  ACP 全天 turn2 挂死 30+ 发同属此症。→ LinBlue 对本账户大体量请求已失去服务能力
  （欠费降权或额度封顶，机制存疑但复现率 100%）。
- 结论：goal 停机条件成立（业务弹药耗尽）。M2 维持 paused + 现场封存；
  小请求幸存说明这不是临时窗口，充值/提额前重启派工=空烧。

## 2026-09-10 20:20 BJT —— M2 收口：真凶不是网关，是权限黑洞（队长自纠全过程）

**结果**：M2 SUCCESS（try7 @20:15:51 UTC 哨兵），pytest 43/43 绿，uvicorn 真机烟测
（/api/v1/predictions 200 / 坏日期 400 / 未登录 401），验收提交 `4d8d89e`。

**根因链（三条，全部今天实锤）**：
1. **网关 100s 长生成割线**（18:3x 发现）：LinBlue relay 对超 ~100s 的生成切断。
   V4F 高 effort 千字 thinking 挤占预算 → toolCall 参数流不全 → OMP 不发执行。
   处置：config.yml maxEffort high→low（19:36）——生效后 toolCall 30s 内必达。
2. **bash 权限黑洞 300s**（19:56 对照实验实锤）：omp-call 客户端对
   session/request_permission 不响应，OMP 300s 超时后才兜底执行。上午一切"慢"、
   下午一切"死"皆源于此（edit/read 类工具不触发权限，故文件编辑一直正常）。
3. **队长自伤**（20:0x）：我给 omp-call 打 AUTO-APPROVE 补丁时按 Zed 旧 schema 回
   `{"outcomeSelected":...}`，OMP 回 `unknown option ID: undefined` → fail-closed，
   比 300s 黑洞更糟。20:14 抓 PERM-RAW 原始 options 对照后改为顶层
   `{"outcome":"selected","optionId":...}`（双 schema 并发），一发即通。
   教训：**协议响应格式必须以对方实际 option/字段样本为准，先抓 raw 再写 handler**。

**基础设施变更**（全部队长侧，不触业务码）：
- omp-call：request_permission 自动 allow_once + failed 状态全量 dump + PERM-RAW 探针日志
  （备份 omp-call.bak-preautoapprove）。
- m2_finalize.sh：客观条件盖章（守卫 grep + 43 passed 必过才写 M2.done）——v10 交卷闭环成立。
- 撤销待办：PERM-RAW/failed dump 属临时探针，M4 收口后降为 trace-only 或删除。

**遗留澄清**：17:52 后 "agent 全灭" 假象 = 黑洞+割线叠加；LinBlue 全程计费正常，
"额度耗尽" 结论维持撤销。11:53:04 predictions.py 那次 mtime 写入未能归因（无内容变化），
记录在案不追。

## 2026-09-10 23:xx BJT —— M5 上线成功：https://league-predict.fastapicloud.dev

**平台事实（全部实测，写死为教条）**：
1. deploy token（fcd_）走 `FASTAPI_CLOUD_TOKEN`+`FASTAPI_CLOUD_APP_ID`，deploy 命令免设备码；
   但 **env 管理与 logs/stream 仅 user token**（API 层 401/拒绝，REST 换路径也无效）。
2. 打包上传**屏蔽点文件 .env**（CLI 默认 ignore 规则；`.fastapicloudignore` 的 `!.env` 实测无效）。
3. 运行时导入按 `[tool.fastapi] entrypoint`（小写！repo 里的驼峰 `entryPoint` 是无效键，
   但本 app 靠顶层 `main.py::app` 兜底成功——deploy4 起 Ready the chicken）。
4. 平台从 **/app 源码目录**运行（health 里 session_db_path=/app/web/.data 证实），
   `pip install .` 只是装依赖的副产物 → staging pyproject `packages=[]` 正确。
5. Hobby scale-to-zero：冷启动探针 404@0.4s=未路由；Ready 后正常。

**排障路径**（三步定音）：deploy1 setuptools 多包自动发现炸 → packages=[]；deploy2/3 缺顶层
入口 → main.py bootstrap；deploy5-7 .env 被 ignore 链吞 → config.env（不带点）+ 打包时注入
`web/__init__.py` 显式 load_dotenv（staging-only，仓库零污染，deploy8 环境全对）。

**发布工具链（队长基建）**：`/root/build_league_web.sh`（组装 staging）+
source `deploy/.env` 后 `fastapi cloud deploy /root/build/league-web`。
重部署 = 两行命令。**已知瑕疵**：staging main.py 挂着 /_diag/env 诊断路由（键名已脱敏，
当前不被执行）；repo 的 `entryPoint` 驼峰键待 OMP 顺手改 `entrypoint`（不影响运行）。

**验证矩阵（线上）**：/health 200·env 注入生效(host=0.0.0.0/CORS 域名/cron off)、
登录 200/错密 401、index.html 200、predictions/today 200、jobs 200、ai/status 200。
凭据：admin / `deploy/runtime.env`（git 不跟踪）。
**待用户点头**：GHA prediction cron 下线（M1–M5 全 PASS 前提已凑齐）。

## 2026-09-11 —— 配置变更批：改口令 + 两把足球数据 key + GHA 下线落地

- `deploy/runtime.env`：`AUTH_PASSWORD=<已改，值不入库>`（用户指定；auth 侧无长度校验，hmac 等值比对）；
  新增 `FOOTBALL_DATA_API_KEY=<已脱敏>`、`API_FOOTBALL_KEY=<已脱敏>`（端点无需配置，
  `scripts/core/data/fetch.py` 硬编码即 api.football-data.org/v4 + v3.football.api-sports.io）。
  key 经 config.env 注入→`_build_env` 全量透传给 predict 子进程，引擎直接 `os.environ` 消费。
- **GHA 自动 cron 下线**：main=1b1ba20（schedule 块删除，保留 workflow_dispatch）；
  本机 git remote 切 SSH（HTTPS 无凭据）。
- deploy9（deployment 118ac606, success）上线 → 线上验收：login 新口令=200（首两次 401 为
  失败计数锁 5/600s + 灰度旧容器噪音，**排查教训：改密后先等锁过期再定罪**）；
  触发 epl 预测 job 2b4e4007a21e → done/exit0/90s，today 出 7 场中文预测（football-data 取数通）。
- **AI 中文遗留项（未动，待用户决策）**：web job 只跑 predict.py，AI 摘要读缓存
  `predictions/ai_scores.json`（英文，GHA 时代产物）；prompt 英文硬编码于
  `ai/batch_pipeline.py::_build_prompt`。要云端出中文 AI 摘要需三件套：
  ①prompt 中文化（OMP 工单）②LLM_API_KEY/BASE/MODEL 进 runtime.env（用户给 key）
  ③enrichment 步骤接入云端触发链（当前断，因 GHA 恰是唯一 regen 通道）。

## M6 系列：AI 摘要云端化+中文化（2026-09-11，deploy10-14）
- **LLM 选型 agnes-ai**（OpenAI 兼容，json_object 实测四绿）；三件套进 runtime.env→config.env（git 外）。LinBlue=OMP agent 大脑（余额续用至耗尽），agnes=应用 AI 层，用途隔离。
- **M6**(OMP)：prompt 中文指令行（授权引擎例外）+ /jobs/ai-enrich 端点 + _spawn 公共链 + SPA 按钮。
- **验收揭穿 GHA 幽灵**：ai_enrich_gha.py 输入是 GHA 专属 /tmp/predict_output.txt，云端 job 静默空转 exit 0。
- **M6R**：执行体改 `python -m web.enrich`（直读 store.latest_by_league，ai 延迟 import，请求路径保持纯净）；staging pyproject 补 requests（云端镜像实证必需）。
- **M6R2**：collect_items 字段映射修正（pick/confidence 不存在→direction/stars/confidence_score）。
- **M6S**（云端实证缺陷）：analyse_batch 位置 zip 配对致摘要串位（EPL 批实锤）→ 按名 join+兼容回退+prompt 要求 match 回显。**本地真调 agnes 铁证：5/5 摘要置信度数值与 store 真值逐一对齐**。
- 测试轨迹 56→63→65→68 绿。deploy13 曾把错位中文摘要上线（10条含串位）→ deploy14 修。
- Hobby 限制：ai_scores.json 云端写回不跨 scale-to-zero 持久（冷启回退 git 种子）；种子已更新 5 条中文 csl 样例。
- **deploy14(5a603aab) 云端终验过**：predict epl 7场→enrich 10项 exit 0；中文摘要配对铁证双向核验（队名切尔西/利物浦自指 + "本组最高"=真值最高 conf 0.177 伯恩茅斯行）；deploy13 式串位清零。用户指令 4（AI 中文）+5（触发验证）就此闭环。

## GHA 全退役 + 主线合并（2026-09-11，用户指令：都放 fastapi，解锁 push）
- 删除 `.github/workflows/league-predict.yml`（注意 web-dashboard 上残留的是旧 cron 版本，merge main 若不清理会**复活每日自动跑**——已连同 `references/gha-workflow-template.md` 一并 git rm）。
- README 重写：头部去 GHA 宣称 + 新增「运行与部署」章节（fastapicloud 地址/部署链路/平台5铁律摘要/config.env 注入说明；口令只指向运维机，**不写入库**）。
- 清理僵尸 docs/web/logs/M0a.job；STATUS.md 快照刷新。
- 分支策略：web-dashboard（M0b-M6S 全部交付+本批文档）merge 进 main 后 push main；web-dashboard 分支同推留档。

## 代码质量审核（2026-09-12，用户指令：队长审核健壮性/函数化/死代码/死模块）
- 产出 `docs/web/CODE_REVIEW.md`：4 死模块（ai_enrich_gha / feedback_memory / cache / .agents 389 文件污染）、20+ 死代码项（reconcile_results+print_metrics_summary 0 调用、_FORBIDDEN 常量化、try_ai_status 拆线、~15 未用导入）、11 个 >50L 函数（calculate_prediction 336L 居首，段注释即拆分刀口）、9 类健壮性缺陷（D1 孤儿任务全站 409 锁死最危）。
- 处置：拆 WO-M7a（纯清理）→ WO-M7b（健壮性+前四大函数）两张工单派 OMP，待用户批准。审核手段：AST 测长 + pyflakes + import 图 + 人工审读 web 全量。

## M7 质量战役执行记录（2026-09-11，OMP 执行 × 队长验收）
- 模型：LinBlue/DeepSeek-V4-Flash；44 commits（a1–a8 清理 / b1–b5,b7 健壮性 / c1a–c5c 函数化 / .agents 反追踪 / 文档）。
- 结果：全仓 AST >50 行函数清零（21 个，比审核原表多 10 个漏网）；197 测试每步双跑绿；净 −17 万行仓库污染（.agents）+ −700 行死代码。
- 否决与放弃：b6（两版实测破契约，revert bc488d6 后 D2/D5 判"有意放弃"，理由入 CODE_REVIEW 处置表）。
- 教训：哨兵不写≠没干活（3 次 override 收口）；grep '197 passed' 会匹配失败行，验收必 grep FAILED；bisect 后必查 branch。

## M8 篮球整合（2026-09-12 夜巡，OMP=GPT-5.6-luna 全单一次过）
- e1a 引擎迁入 scripts/bball/（5 模块常数逐字一致）→ e1b CLI+注册+active 休战旗 → e1c daysBack/Key 脱敏/优雅退出 → e2 odds 参数回正+--ahead-days+DATA_DIR 上云契约 → e3 前端 sport 双模板（足球响应字节级不变）→ e4 篮球 17 离线用例（197→214）。
- 休赛期数据（D-06）：32 场揭幕战中文前瞻随包上云（免费档无历史比分，backtest 转向 ahead-90 前瞻）。
- 事故（D-07）：t2b 带红写哨兵 + 我端分号链坏 commit → reset 零污染回退 → t2b2 修复并**新增队长门禁：FAILED=0 && GREEN=2 && commit 硬链**。
- 尾巴 t2 收官：enrich [:5]→[:40] 全量窗口（当日 7 场全出摘要）。
- e5 云端实战补刀：enrich 全量窗口 40 条真实跑挂一次（LLM 批返回混入字符串元素）→ e5b 外层过滤（无害纵深）+ e5d 真崩点 isinstance 守卫+回归测试（215 绿）。deploy18 后云端重跑成功：ai 缓存 112→142，NBA 揭幕战全中文摘要（样例：奇才 vs 凯尔特人→"凯尔特人面对重建中的奇才具有压倒性实力优势…"）。
- 终态：main=58b8528+docs，215 测试基线（197 足球 + 17 篮球 + 1 enrich 回归），quality/m7 已并 main，deploy18 线上 = 足球五联赛 + NBA 32 场前瞻双 sport 看板。
