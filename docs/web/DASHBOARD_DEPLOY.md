# Dashboard 部署说明

## 访问地址

```text
https://140.83.62.161/dashboard/
```

公网入口由本机 Nginx 提供 HTTPS 和 Basic Auth；页面自身继续使用 league-v2 的 session 登录。

## 服务

- systemd：`league-dashboard.service`
- FastAPI：`127.0.0.1:8077`
- 静态入口：`/static/dashboard.html`
- Dashboard API 代理：公网 `/dashboard/api/` → FastAPI `/api/`
- Dashboard 登录页：公网 `/dashboard/login` → `/static/dashboard-login.html`

## 验证

```bash
sudo systemctl status league-dashboard.service
sudo nginx -t
curl -kI https://140.83.62.161/dashboard/
```

无 Nginx Basic Auth 凭据时返回 `401` 是预期行为。通过 Basic Auth 后，Dashboard 页面再使用应用自己的登录 session 访问预测 API。
