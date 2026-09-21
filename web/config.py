"""web.config — 全应用唯一环境变量读取点。

约定（见 .env.example，清单唯一出处）：
- 所有 env 读取集中在此模块；其他模块一律 `from web import config`。
- 默认值一律安全：默认仅本机可访问、https 开关默认关（cookie secure 随之关，
  避免本地开发被 secure cookie 卡死）。
- 时间存储一律 UTC epoch 秒（float/int）；展示层才换算 BJT。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("web.config")

# 仓库根目录（web/config.py 的父目录的父目录）。
BASE_DIR = Path(__file__).resolve().parent.parent

# .env 存在即加载；不存在（如全新 venv 裸启动）时走默认值。
load_dotenv(BASE_DIR / ".env")


def _env_int(name: str, default: int) -> int:
    """读整数型 env；缺失或非法值记 warning 并回退 default，绝不因坏 env 崩溃。"""
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        logger.warning("env %s 非整数，回退默认值 %s", name, default)
        return default


# --- 服务 ---------------------------------------------------------------
HOST: str = os.getenv("WEB_HOST", "127.0.0.1")  # 默认仅本机可访
PORT: int = _env_int("WEB_PORT", 8000)

# --- 会话认证 -----------------------------------------------------------
# 单账号模型：账号信息全部来自 env，仓库内不落任何真实值。
# 占位口令：生产模式（非本机 HOST）未设置 AUTH_PASSWORD 时启动即报错（见文件底部守卫）。
_DEFAULT_PW = "unit-test-password-placeholder"
AUTH_USERNAME: str = os.getenv("AUTH_USERNAME", "admin")
AUTH_PASSWORD: str = os.getenv("AUTH_PASSWORD", _DEFAULT_PW)

# 会话 TTL（秒），默认 12 小时。
SESSION_TTL_SECONDS: int = _env_int("SESSION_TTL_SECONDS", 43200)
# 会话过期后立即删除；同时惰性清理过期行。
SESSION_CLEANUP_ON_ACCESS: bool = os.getenv("SESSION_CLEANUP_ON_ACCESS", "1") in ("1", "true", "True")

# --- 登录限速：连续失败 N 次 → 锁定窗口 W 秒内一律 429 ------------------
LOGIN_MAX_FAILURES: int = _env_int("LOGIN_MAX_FAILURES", 5)
LOGIN_LOCKOUT_SECONDS: int = _env_int("LOGIN_LOCKOUT_SECONDS", 600)

# --- Cookie / CORS ------------------------------------------------------
# 部署在 https 后面时置 true：cookie 加 Secure，CORS 只放行 https 来源。
USE_HTTPS: bool = os.getenv("USE_HTTPS", "0") in ("1", "true", "True")
CORS_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "").split(",")
    if o.strip()
]  # 空列表 = 不启用 CORS 中间件（默认同源，最安全）。

# --- 会话存储 -----------------------------------------------------------
# 默认落在 web/.data/（gitignore 已排除），绝不落入引擎数据目录。
SESSION_DB_PATH: str = os.getenv("SESSION_DB_PATH", str(BASE_DIR / "web" / ".data" / "sessions.db"))

# --- 任务触发（M3：jobs.py / 配额守卫）----------------------------------
# 引擎子进程超时（秒），到点必杀。
PREDICT_TIMEOUT_SECONDS: int = _env_int("PREDICT_TIMEOUT_SECONDS", 600)
# 每日预测触发上限（BJT 日口径；默认 80，给手动操作留余量）。
DAILY_TRIGGER_LIMIT: int = _env_int("PREDICT_DAILY_LIMIT", 80)
# 同一天惰性自动刷新至多一次（predictions/today 缺数据时兜底）。
AUTO_REFRESH_DAILY: bool = os.getenv("AUTO_REFRESH_DAILY", "1") in ("1", "true", "True")

# 定时预测不在进程内调度，改由 systemd 统一接管：
#   ops/league-daily-predict.timer  → 每日 09:00 Asia/Shanghai（显式时区）
# 旧的 ENABLE_CRON / CRON_HOUR（apscheduler 进程内 cron）已移除：它不传 timezone，
# 在 UTC 宿主上实际 09:00 UTC（=17:00 BJT）触发，且服务重启后要重算，实测 0 次成功触发。

# --- M3 数据目录（web/.data/ 下，gitignore 已排除）-----------------------
DATA_DIR: Path = BASE_DIR / "web" / ".data"
JOBS_DIR: Path = Path(os.getenv("JOBS_DIR", str(DATA_DIR / "jobs")))
JOBS_LOCK_FILE: Path = Path(os.getenv("JOBS_LOCK_FILE", str(DATA_DIR / "jobs.lock")))
QUOTA_FILE: Path = Path(os.getenv("QUOTA_FILE", str(DATA_DIR / "quota.json")))

# --- API-Football（默认关闭；只读、缓存且受配额保护）----------------------
# Enrichment is explicitly opt-in; a missing key or this flag keeps all calls off.
API_FOOTBALL_ENRICH_ENABLED: bool = os.getenv("API_FOOTBALL_ENRICH_ENABLED", "false") in ("1", "true", "True")
# Backwards-compatible low-level switch; enrichment still requires ENRICH_ENABLED.
API_FOOTBALL_ENABLED: bool = os.getenv("API_FOOTBALL_ENABLED", "0") in ("1", "true", "True")
API_FOOTBALL_KEY: str = os.getenv("API_FOOTBALL_KEY", "")
API_FOOTBALL_BASE_URL: str = os.getenv("API_FOOTBALL_BASE_URL", "https://v3.football.api-sports.io")
API_FOOTBALL_CACHE_DIR: Path = Path(os.getenv("API_FOOTBALL_CACHE_DIR", str(DATA_DIR / "api-football-cache")))
API_FOOTBALL_CACHE_TTL_SECONDS: int = _env_int("API_FOOTBALL_CACHE_TTL_SECONDS", 900)
API_FOOTBALL_DAILY_LIMIT: int = _env_int("API_FOOTBALL_DAILY_LIMIT", 100)
API_FOOTBALL_MINUTE_LIMIT: int = _env_int("API_FOOTBALL_MINUTE_LIMIT", 10)
API_FOOTBALL_TIMEOUT_SECONDS: int = _env_int("API_FOOTBALL_TIMEOUT_SECONDS", 10)


# --- 临时赛事白名单（五大联赛之外的赛事，CSV；用于亚运/欧冠/世界杯/奥运等）----
# 命名采用 fact.jc_match.league_cn 的官方全名（与 jczq_offer 入港的 leagueName 一致）。
# 重启服务生效；赛事过完把这一行注释掉、再次重启即恢复"五大联赛主面板"。
# 例：LEAGUE_TEMP_LEAGUES="亚运会男足,亚运会女足,亚洲冠军精英联赛,欧罗巴联赛"
LEAGUE_TEMP_LEAGUES_RAW: str = os.getenv("LEAGUE_TEMP_LEAGUES", "").strip()


def parse_league_temp(raw: str) -> tuple[str, ...]:
    """解析 LEAGUE_TEMP_LEAGUES：去空白/去重/过滤空项，保持原顺序。"""
    seen, out = set(), []
    for tok in raw.split(","):
        t = tok.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return tuple(out)


LEAGUE_TEMP_LEAGUES: tuple[str, ...] = parse_league_temp(LEAGUE_TEMP_LEAGUES_RAW)


def env_summary() -> dict:
    """暴露给 /health 的无敏感摘要（不含账号/密码）。"""
    return {
        "host": HOST,
        "port": PORT,
        "session_ttl_seconds": SESSION_TTL_SECONDS,
        "login_max_failures": LOGIN_MAX_FAILURES,
        "login_lockout_seconds": LOGIN_LOCKOUT_SECONDS,
        "use_https": USE_HTTPS,
        "cors_origins": CORS_ORIGINS,
        "predict_timeout_seconds": PREDICT_TIMEOUT_SECONDS,
        "daily_trigger_limit": DAILY_TRIGGER_LIMIT,
        "league_temp_leagues": list(LEAGUE_TEMP_LEAGUES),
    }


# --- 生产口令守卫：非本机 HOST 时禁止占位口令 ---------------------------
if HOST not in ("127.0.0.1", "localhost", "::1") and AUTH_PASSWORD == _DEFAULT_PW:
    raise RuntimeError("生产模式（非本机 HOST）禁止使用默认口令，请设置 AUTH_PASSWORD")
