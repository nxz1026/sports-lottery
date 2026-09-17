# Dashboard 部署说明

## 访问地址

```text
原有门户：`https://140.83.62.161/dashboard/`
体彩 Dashboard：`https://140.83.62.161/dashboard/jc/`

公网入口由本机 Nginx 提供 HTTPS 和 Basic Auth；页面自身继续使用 league-v2 的 session 登录。

## 服务

- systemd：`league-dashboard.service`
- FastAPI：`127.0.0.1:8077`
- 静态入口：`/static/dashboard.html`
- Dashboard API 代理：公网 `/dashboard/jc/api/` → FastAPI `/api/`
- Dashboard 登录页：公网 `/dashboard/jc/login` → `/static/dashboard-login.html`
- 原有门户 `/dashboard/` 保持由原 NDORACLE 服务提供

## 当前功能与数据边界

- `/dashboard/jc/` 是独立体彩 Dashboard，原门户 `/dashboard/` 不变；
- 应用登录后调用 `/api/v1/predictions/*`、`/api/v1/accuracy/*`、`/api/v1/calibration`、`/api/v1/backtest` 等只读接口；
- 串关赔率、市场概率、Edge、EV 只有在完整结构化 1X2 市场数据存在时才展示，否则明确显示不可用；
- 本机投注记录保存在浏览器 `localStorage`，键名为 `jc_dashboard_ledger_v1`，仅用于手工记录和已结算 ROI，不上传服务器；
- 伤停、首发、历史交锋和预测概率校准分桶当前没有可靠数据源，页面不虚构这些信息；
- 预测历史接口不提供实际赛果关联，历史表不能被当作投注结算结果。

## 应用路由与鉴权

FastAPI 原生路由不包含 `/dashboard/jc` 前缀：

- `GET /health`：免应用鉴权；
- 未登录访问 `GET /`：重定向 `/login`，已登录则重定向 `/static/index.html`；
- `GET /login`：重定向 `/static/login.html`；
- `POST/GET /api/v1/login|me`、`POST /api/v1/logout`：应用 session，cookie 名为 `lp_session`；
- 预测、历史、校准、回测、数据源和任务 API 均需要应用 session。

公网 Nginx 负责把 `/dashboard/jc/` 映射到体彩静态入口，把 `/dashboard/jc/login` 映射到登录页，并将 `/dashboard/jc/api/` 去前缀代理到本机 FastAPI `/api/`。Nginx/systemd 配置属于部署机外部 artifact，本仓库不包含配置文件；变更时必须同步保存 location、ExecStart、`PYTHONPATH` 和端口配置。

当前应用关键配置由环境变量提供，不入库：`WEB_HOST`、`WEB_PORT`、`AUTH_USERNAME`、`AUTH_PASSWORD`、`USE_HTTPS`、`SESSION_DB_PATH`、`LP_OUTPUT_DIR`、`PREDICT_DAILY_LIMIT`、`PREDICT_TIMEOUT_SECONDS`、`AUTO_REFRESH_DAILY`、`ENABLE_CRON`。Web 依赖使用 `requirements-web.txt` 安装。预测文件从 `LP_OUTPUT_DIR/predictions` 读取，坏 JSON 或缺失文件按降级语义跳过；自动刷新与 AI 任务共享配额并做同日去重。

## 验证

```bash
sudo systemctl status league-dashboard.service
sudo nginx -t
curl -kI https://140.83.62.161/dashboard/
curl -kI https://140.83.62.161/dashboard/jc/
curl -kI https://140.83.62.161/dashboard/jc/login
```

无 Nginx Basic Auth 凭据时返回 `401` 是预期行为。通过 Basic Auth 后，再使用应用登录 session 验证 `/dashboard/jc/api/v1/me`、登录后的 `/dashboard/jc/api/v1/predictions/today`。Hobby/scale-to-zero 或进程重启环境可能丢失生成文件与 session，除非 `LP_OUTPUT_DIR`、`SESSION_DB_PATH` 使用外部持久化。
