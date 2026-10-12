# ops/ — 运营脚本与 systemd 模板

## league-web.service（常驻 FastAPI 服务）

`league-web.service` 是 dashboard + 全部 `/api/v1/*` 的常驻单元。
部署步骤（Oracle）：

```bash
# 1. 拷贝 unit 到 systemd 目录
sudo cp ops/league-web.service /etc/systemd/system/

# 2. 重载 + 启用自启
sudo systemctl daemon-reload
sudo systemctl enable league-web.service

# 3. 启动 + 校验
sudo systemctl restart league-web.service
sleep 2
curl -s http://127.0.0.1:8077/health | jq .ok      # 应为 true
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8077/health  # 200

# 4. 装到 Nginx（参考 docs/acceptance-2026-09-17.md）
#   location /dashboard/jc/api/v1/ { proxy_pass http://127.0.0.1:8077/api/v1/; }
```

### 关键设计

| 字段 | 用意 |
|------|------|
| `Type=simple` | 前台 uvicorn 进程；MainPID 由 systemd 跟踪 |
| `Restart=on-failure` + `RestartSec=5` | P1-4：`/health` 真依赖探活 → DB 短暂抖动自愈 |
| `StartLimitIntervalSec=300` + `Burst=10` | 5 分钟内崩溃 ≤10 次才重启，超出进入 backoff（避免雪崩刷日志） |
| `EnvironmentFile=/home/ubuntu/DSH/sports-lottery/.env` | 凭据统一从 `.env` 读，与 `sync-oracle.ps1` 一致 |
| `--proxy-headers --forwarded-allow-ips="127.0.0.1"` | P1-1：仅信任 127.0.0.1（Nginx 反代）的 X-Forwarded-For；与 `web/config.py::TRUSTED_PROXY_IPS` 同源 |
| `--workers 2` | 2 worker 并发；P2-8 配 `_guard_quota` 跨 worker 文件锁后即可安全并发 |
| `OOMPolicy=kill` + `OOMScoreAdjust=-100` | OOM 时优先杀 web 而非拖累整机；en kill 降低被 OOM-killer 选中概率 |
| `LimitNOFILE=65536` | 默认 1024 不够；预测管线频繁打开预测文件 + session.db |
| `KillMode=mixed` + `TimeoutStopSec=15` | 平滑停服，15s 强 kill；与 uvicorn `--graceful-timeout` 默认 30s 配合 |
| `StandardOutput=journal` + `SyslogIdentifier=league-web` | `journalctl -u league-web.service` 即可排障

### 排障

| 现象 | 命令 |
|------|------|
| 服务反复 Restart | `journalctl -u league-web.service -n 200` 看 crash 栈 |
| 凭据错 | `systemctl show league-web.service -p EnvironmentFiles` 确认加载 `.env` |
| OOM 触发 | `dmesg -T \| grep -i killed` 看是否被 OOM-killer 选中 |
| 端口冲突 | `ss -tlnp 'sport = :8077'` 查占用 PID |

## league-daily-predict.{service,timer,sh}

每日 09:30 BJT 触发一次预测管线（oneshot 单元）。timer 用 systemd 内的
`OnCalendar` 触发，不是 cron。

- `league-daily-predict.service`：oneshot，跑完即退出
- `league-daily-predict.timer`：每日触发，可独立 `systemctl start ...timer`
- `league-daily-predict.sh`：实际拉起 `predict.py` + `predict_bball.py` 的脚本

P2-10 待办：webhook 缺失时 `exit 1` 让 systemd `OnFailure=` 接住。