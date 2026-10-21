"""web.services.notify —— 推送通道的 web 封装（建议 5 的告警出口）。

**顶层不 import scripts**（守门项：web 层顶层不得 import `scripts.*`）⇒ 函数内延迟 import。

⚠️ **凭证纪律**：webhook URL 是凭证 ⇒ 只从环境变量读，**本模块任何返回值都不含 URL**。
未配置 ⇒ 返回 ``{"ok": False, "reason": "not_configured"}`` ⇒ 调用方如实标 `pushed=false`。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("web.notify")

# T3 启动链不含 scripts/：自举一次。
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.append(_SCRIPTS_DIR)


def configured() -> bool:
    """是否配置了推送通道（**只回布尔值**）。"""
    try:
        from market.notify import feishu_configured

        return feishu_configured()
    except Exception:  # noqa: BLE001
        return False


def push_text(text: str, *, title: str | None = None) -> dict[str, Any]:
    """推一条文本。**永不抛异常**；未配置 ⇒ ``ok=False, reason='not_configured'``。"""
    try:
        from market.notify import push_feishu

        return push_feishu(text, title=title)
    except Exception as exc:  # noqa: BLE001 - 推送失败不阻断主流程
        logger.warning("web.notify 推送失败（降级）：%s", type(exc).__name__)
        return {"ok": False, "reason": f"{type(exc).__name__}"}
