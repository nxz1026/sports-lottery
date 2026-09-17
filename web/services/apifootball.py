"""Read-only, opt-in API-Football provider with disk cache and quota guards.

The provider never runs at import time.  Callers must explicitly enable it and
provide ``API_FOOTBALL_KEY``; all requests use GET and urllib so transport is
straightforward to mock in tests.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from web import config

EPL_LEAGUE_ID = 39


class ApiFootballError(RuntimeError):
    """Provider is disabled, over quota, or returned an invalid response."""


class ApiFootballQuotaError(ApiFootballError):
    """A daily or rolling-minute request budget is exhausted."""


class ApiFootballService:
    """Small read-only API-Football client.

    ``transport`` receives a urllib Request and timeout and returns response
    bytes (or a response object exposing ``read``); injecting it avoids real
    network calls in tests.
    """

    def __init__(self, *, transport: Callable | None = None, now: Callable[[], float] | None = None,
                 cache_dir: Path | None = None):
        self.transport = transport or self._urlopen
        self.now = now or time.time
        self.cache_dir = Path(cache_dir or config.API_FOOTBALL_CACHE_DIR)
        self._lock = threading.Lock()

    @staticmethod
    def _urlopen(request: Request, timeout: int):
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured HTTPS endpoint
            return response.read()

    def _enabled(self) -> None:
        if not getattr(config, "API_FOOTBALL_ENRICH_ENABLED", False):
            raise ApiFootballError("API-Football provider is disabled")
        if not config.API_FOOTBALL_KEY:
            raise ApiFootballError("API-Football key is not configured")

    def _cache_path(self, endpoint: str, params: dict[str, object]) -> Path:
        key = endpoint.strip("/") + "?" + urlencode(sorted(params.items()))
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def _read_cache(self, path: Path) -> dict | None:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if self.now() - float(item["cached_at"]) <= config.API_FOOTBALL_CACHE_TTL_SECONDS:
                return item["data"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return None

    def _quota_path(self) -> Path:
        return self.cache_dir / "quota.json"

    def _guard_quota(self) -> None:
        now = self.now()
        today = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        try:
            state = json.loads(self._quota_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("day") != today:
            state = {"day": today, "daily": 0, "requests": []}
        requests = [float(v) for v in state.get("requests", []) if now - float(v) < 60]
        if int(state.get("daily", 0)) >= config.API_FOOTBALL_DAILY_LIMIT:
            raise ApiFootballQuotaError("API-Football daily quota exhausted")
        if len(requests) >= config.API_FOOTBALL_MINUTE_LIMIT:
            raise ApiFootballQuotaError("API-Football minute quota exhausted")
        state["daily"] = int(state.get("daily", 0)) + 1
        state["requests"] = requests + [now]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._quota_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self._quota_path())

    def _get(self, endpoint: str, params: dict[str, object]) -> dict:
        self._enabled()
        path = self._cache_path(endpoint, params)
        cached = self._read_cache(path)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._read_cache(path)
            if cached is not None:
                return cached
            self._guard_quota()
            query = urlencode({k: str(v) for k, v in params.items()})
            url = config.API_FOOTBALL_BASE_URL.rstrip("/") + "/" + endpoint.lstrip("/") + "?" + query
            req = Request(url, headers={"x-apisports-key": config.API_FOOTBALL_KEY}, method="GET")
            try:
                raw = self.transport(req, config.API_FOOTBALL_TIMEOUT_SECONDS)
                if hasattr(raw, "read"):
                    raw = raw.read()
                payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            except Exception as exc:  # transport and decoding failures are safe provider errors
                raise ApiFootballError("API-Football request failed") from exc
            if not isinstance(payload, dict) or payload.get("errors"):
                detail = payload.get("errors") if isinstance(payload, dict) else "invalid response"
                raise ApiFootballError(f"API-Football response error: {detail}")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"cached_at": self.now(), "data": payload}), encoding="utf-8")
                tmp.replace(path)
            except OSError as exc:
                raise ApiFootballError("API-Football cache write failed") from exc
            return payload

    @staticmethod
    def _rows(payload: dict) -> list[dict]:
        rows = payload.get("response", [])
        return rows if isinstance(rows, list) else []

    def injuries(self, fixture_id: int) -> list[dict]:
        rows = self._rows(self._get("injuries", {"fixture": int(fixture_id)}))
        out: list[dict] = []
        for r in rows:
            player = r.get("player") or {}
            # API-Football 把伤停类型/原因嵌在 ``player`` 下（行级只有
            # fixture/league/team/player 四键）。旧代码读行级字段，导致每条记录
            # 的 type/reason 恒为 None —— 数据被静默丢弃。保留行级回退以兼容
            # provider 未来把字段提到行级的情况。
            out.append({
                "player": player,
                "team": r.get("team") or {},
                "type": player.get("type") or r.get("type"),
                "reason": player.get("reason") or r.get("reason"),
            })
        return out

    def lineups(self, fixture_id: int) -> list[dict]:
        rows = self._rows(self._get("fixtures/lineups", {"fixture": int(fixture_id)}))
        return [{"team": r.get("team") or {}, "formation": r.get("formation"), "startXI": r.get("startXI") or [], "substitutes": r.get("substitutes") or [], "coach": r.get("coach") or {}} for r in rows]

    @staticmethod
    def _fixture_date(row: dict) -> datetime:
        raw = (row.get("fixture") or {}).get("date")
        fallback = datetime.min.replace(tzinfo=timezone.utc)
        if not isinstance(raw, str):
            return fallback
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return fallback
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def h2h(self, home_team_id: int, away_team_id: int, *, last: int | None = None) -> list[dict]:
        """Head-to-head fixtures, most recent first.

        ``last`` is applied client-side and is deliberately never sent to the
        provider: free API-Football plans reject the parameter outright with
        ``Free plans do not have access to the Last parameter.`` and answer with
        zero rows, so sending it would silently empty the result.  The provider
        also does not order H2H rows globally by date (it groups by home-team
        identity), which is why the limit is taken from an explicit date sort
        rather than from the raw response order.
        """
        params: dict[str, object] = {"h2h": f"{int(home_team_id)}-{int(away_team_id)}"}
        rows = self._rows(self._get("fixtures/headtohead", params))
        out = [{"fixture": r.get("fixture") or {}, "league": r.get("league") or {}, "teams": r.get("teams") or {}, "goals": r.get("goals") or {}, "score": r.get("score") or {}} for r in rows]
        out.sort(key=self._fixture_date, reverse=True)
        if last is not None:
            out = out[: max(0, int(last))]
        return out


APIFootballService = ApiFootballService
