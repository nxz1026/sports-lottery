"""飞书（Feishu / Lark）自定义机器人推送 —— 建议 5 的告警出口。

⚠️ **凭证纪律**：webhook URL 是**凭证**（谁拿到就能往那个群发消息）⇒
- **只从环境变量 `FEISHU_WEBHOOK_URL` 读**，绝不写进代码、文档、commit message 或日志；
- **日志/异常里绝不回显 URL**（只报 HTTP 状态与错误类型）；
- **未配置 ⇒ 直接返回 ``{"ok": False, "reason": "not_configured"}``**，
  调用方据此把 `pushed` 标成 false —— **绝不假装「已推送」**
  （建议 3/4「静默失败」教训的同款防线：失败必须可见、且不可谎报）。

用法：
    from market.notify import push_feishu, feishu_configured
    push_feishu("hello")
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

# 环境变量名（**只放 env，不入库**）
ENV_WEBHOOK = "FEISHU_WEBHOOK_URL"
# 推送超时（秒）—— 宁可超时也不许卡住采集链路
DEFAULT_TIMEOUT = 10


def feishu_configured() -> bool:
    """是否配置了 webhook（**不返回 URL 本身**）。"""
    return bool((os.environ.get(ENV_WEBHOOK) or "").strip())


def push_feishu(text: str, *, timeout: int = DEFAULT_TIMEOUT,
                title: str | None = None) -> dict[str, Any]:
    """发一条飞书文本消息。

    返回 ``{"ok": bool, ...}``：**永不抛异常**（推送失败不能阻断采集/落库），
    失败时带 ``reason``（不含 URL）。
    """
    url = (os.environ.get(ENV_WEBHOOK) or "").strip()
    if not url:
        return {"ok": False, "reason": "not_configured"}
    if not text or not text.strip():
        return {"ok": False, "reason": "empty_text"}

    payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
    if title:
        payload["content"]["text"] = f"**{title}**\n{text}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            code = resp.getcode()
    except urllib.error.HTTPError as exc:
        # ⚠️ 不回显 URL / 响应体里的敏感信息，只报状态码
        return {"ok": False, "reason": f"http_{exc.code}"}
    except Exception as exc:  # noqa: BLE001 - 网络异常一律降级
        return {"ok": False, "reason": f"{type(exc).__name__}"}

    # 飞书返回 {"code":0,"msg":"success"} ⇒ code=0 才算成功
    ok = False
    detail: Any = None
    try:
        parsed = json.loads(body)
        detail = parsed.get("msg") if isinstance(parsed, dict) else None
        ok = code == 200 and isinstance(parsed, dict) and parsed.get("code") == 0
    except Exception:  # noqa: BLE001 - 响应不是 JSON ⇒ 视作失败
        ok = False
    if not ok:
        return {"ok": False, "reason": f"http_{code}", "msg": detail}
    return {"ok": True, "http": code, "msg": detail}
