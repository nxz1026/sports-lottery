# Dashboard 部署说明

## 访问地址

```text
原有门户：`https://140.83.62.161/dashboard/`
体彩 Dashboard：`https://140.83.62.161/jc/`

公网入口由本机 Nginx 提供 HTTPS。`/jc/` 路径**未启用 Nginx Basic Auth**（`auth_basic off`，2026-09-18 起从全局 Basic Auth 剥离），其鉴权由应用层 session（`lp_session` cookie）负责；Nginx Basic Auth 只保护 `/` `/dashboard/` `/cpt/` `/emotion/` `/trade/` 等其它业务。页面自身使用 league-v2 的 session 登录。

> ⚠️ **R47 生产实测（2026-10-19）**：独立登录页 `static/dashboard-login.html` 已于 2026-10-18（`093dafa`）下线，nginx 的 `location = /jc/login` 仍指向它 → 现返回 **404**；前端 401 改为内联提示，不再跳登录页。因此**公网体彩看板当前缺少可用的登录入口**（详见 README「认证与隔离」段与 R47 记录）。

## 服务

- systemd：`league-web.service`（**不是** `league-dashboard.service`——后者已不存在，实测 2026-10-19）
- FastAPI：`127.0.0.1:8077`
- 静态入口：`/static/dashboard.html`
- Dashboard API 代理：公网 `/jc/api/` → FastAPI `/api/`
- Dashboard 登录页：公网 `/jc/login` → `/static/dashboard-login.html`
- 原有门户 `/dashboard/` 保持由原 NDORACLE 服务提供

## 当前功能与数据边界

- `/jc/` 是独立体彩 Dashboard，原门户 `/dashboard/` 不变；
- 应用登录后调用 `/api/v1/predictions/*`、`/api/v1/accuracy/*`、`/api/v1/calibration`、`/api/v1/backtest` 等只读接口；
- 串关赔率、市场概率、Edge、EV 只有在完整结构化 1X2 市场数据存在时才展示，否则明确显示不可用；
- 本机投注记录保存在浏览器 `localStorage`，键名为 `jc_dashboard_ledger_v1`，仅用于手工记录和已结算 ROI，不上传服务器；
- 伤停、首发、历史交锋和预测概率校准分桶当前没有可靠数据源，页面不虚构这些信息；
- 预测历史接口不提供实际赛果关联，历史表不能被当作投注结算结果。

## 应用路由与鉴权

FastAPI 原生路由不包含 `/jc` 前缀：

- `GET /health`：免应用鉴权；
- 未登录访问 `GET /`：重定向 `/login`，已登录则重定向 `/static/index.html`；
- `GET /login`：重定向 `/static/login.html`；
- `POST/GET /api/v1/login|me`、`POST /api/v1/logout`：应用 session，cookie 名为 `lp_session`；
- 预测、历史、校准、回测、数据源和任务 API 均需要应用 session。

公网 Nginx 负责把 `/jc/` 映射到体彩静态入口，把 `/jc/login` 映射到登录页，并将 `/jc/api/` 去前缀代理到本机 FastAPI `/api/`。Nginx/systemd 配置属于部署机外部 artifact，本仓库不包含配置文件；变更时必须同步保存 location、ExecStart、`PYTHONPATH` 和端口配置。

当前应用关键配置由环境变量提供，不入库：`WEB_HOST`、`WEB_PORT`、`AUTH_USERNAME`、`AUTH_PASSWORD`、`USE_HTTPS`、`SESSION_DB_PATH`、`LP_OUTPUT_DIR`、`PREDICT_DAILY_LIMIT`、`PREDICT_TIMEOUT_SECONDS`、`AUTO_REFRESH_DAILY`、`ENABLE_CRON`。Web 依赖使用 `requirements-web.txt` 安装。预测文件从 `LP_OUTPUT_DIR/predictions` 读取，坏 JSON 或缺失文件按降级语义跳过；自动刷新与 AI 任务共享配额并做同日去重。

## 验证

```bash
sudo systemctl status league-web.service
sudo nginx -t
curl -kI https://140.83.62.161/dashboard/         # 401（Nginx Basic Auth 保护）
curl -kI https://140.83.62.161/jc/     # 200（该路径 auth_basic off）
curl -kI https://140.83.62.161/jc/login  # 404（登录页已下线，R47 实测）
curl -s http://127.0.0.1:8077/health | jq .ok     # true（健康检查真实路径，不带 /api/v1）
```

`/jc/` 未启用 Nginx Basic Auth，因此**不带凭据即 200**；其 API 未登录返回的 `401` 来自**应用层 session**（不是 Basic Auth）。要验证应用层登录，用 `.env` 的 `AUTH_USERNAME`/`AUTH_PASSWORD` 走 `POST /api/v1/login` 拿 `lp_session` cookie，再验 `/jc/api/v1/me`、登录后的 `/jc/api/v1/predictions/today`。Hobby/scale-to-zero 或进程重启环境可能丢失生成文件与 session，除非 `LP_OUTPUT_DIR`、`SESSION_DB_PATH` 使用外部持久化。
