"""R59: web/services/apifootball.py 4 个 0 直测 helper 5 测 + D-120 lock。

R58 末已锁 D-117~D-119. 自动化扫描找出剩余 last 4 helper:

- _cache_path    (L58-60): endpoint + sorted params -> sha256 hex.json 路径
- _read_cache    (L62-69): 读 cache + TTL 校验 (依赖 self.now())
- _quota_path    (L71-72): cache_dir/quota.json 简单路径
- _fixture_date  (L162-172) staticmethod: 解析 fixture row date 字段,
                          fallback = datetime.min.replace(tzinfo=utc) 当缺 / 非法

D-120 锁 4 helper (5 测):
- D-120-1: _cache_path 同 endpoint + 不同 sorted params 路径不同; 同输入幂等
- D-120-2: _read_cache TTL 内返 data, TTL 外返 None, 缺 cached_at 返 None
- D-120-3: _quota_path = cache_dir/quota.json
- D-120-4: _fixture_date 合法 ISO / 缺 date / 缺 fixture / 非 dict fixture / 非法字符串
- D-120-5: _cache_path + _read_cache 完整 round-trip: 写 cache 后 _read_cache 命中

策略:
- spec-load web.services.apifootball + 实例化 ApiFootballService
  (用 fake transport + now + cache_dir = tmp_path)
- _fixture_date 是 staticmethod, 可以直接调
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types as _types
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def apifootball_mod(tmp_path, monkeypatch):
    """Spec-load web.services.apifootball + 实例化 service 实例."""
    spec = importlib.util.spec_from_file_location(
        "web_services_apifootball_test",
        REPO_ROOT / "web" / "services" / "apifootball.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # monkeypatch config 提供必要常量 (默认 _urlopen + cache TTL 不依赖 _guard_quota)
    fake_config = _types.ModuleType("config")
    fake_config.API_FOOTBALL_KEY = "fake-key"
    fake_config.API_FOOTBALL_BASE_URL = "https://example.com"
    fake_config.API_FOOTBALL_CACHE_DIR = tmp_path / "cache"
    fake_config.API_FOOTBALL_CACHE_TTL_SECONDS = 3600
    fake_config.API_FOOTBALL_TIMEOUT_SECONDS = 10
    fake_config.API_FOOTBALL_ENRICH_ENABLED = False
    fake_config.API_FOOTBALL_DAILY_LIMIT = 100
    fake_config.API_FOOTBALL_MINUTE_LIMIT = 5
    monkeypatch.setattr(mod, "config", fake_config)

    # 实例化 service, now() 返固定 epoch (便于 TTL 测试)
    fixed_now = 1_700_000_000.0

    def fake_transport(req: Request, timeout: int) -> bytes:  # 不会被 _read_cache / _cache_path 触发
        return b"{}"

    service = mod.ApiFootballService(
        transport=fake_transport, now=lambda: fixed_now,
        cache_dir=tmp_path / "cache",
    )
    return mod, service, fixed_now


# ──────────────────────── D-120: 5 测 ────────────────────────


def test_d120_1_cache_path_idempotent_and_param_order_independent(apifootball_mod):
    """D-120-1: _cache_path (L58-60) 同输入幂等; 不同 sorted params 路径不同.

    L59 ``sorted(params.items())`` 保证 a=1&b=2 vs b=2&a=1 生成同一 hash.
    """
    mod, service, _ = apifootball_mod

    # 同输入两次 — hash 应一致 (幂等)
    p_a = service._cache_path("fixtures", {"league": 39, "season": 2025})
    p_b = service._cache_path("fixtures", {"league": 39, "season": 2025})
    assert p_a == p_b
    assert p_a.name.endswith(".json")
    assert p_a.parent == service.cache_dir

    # 不同 endpoint
    p_diff_endpoint = service._cache_path("standings", {"league": 39})
    assert p_diff_endpoint != p_a

    # 不同 params
    p_diff_params = service._cache_path("fixtures", {"league": 40})
    assert p_diff_params != p_a

    # param 顺序无关 (sorted)
    p_order1 = service._cache_path("fixtures", {"a": 1, "b": 2})
    p_order2 = service._cache_path("fixtures", {"b": 2, "a": 1})
    assert p_order1 == p_order2


def test_d120_2_read_cache_within_and_beyond_ttl(apifootball_mod, monkeypatch):
    """D-120-2: _read_cache (L62-69) TTL 内返 data, TTL 外返 None, 缺 cached_at 返 None."""
    mod, service, fixed_now = apifootball_mod
    cache_path = service.cache_dir / "test.json"

    # 场景 a: TTL 内 (cached_at = now - 100s, TTL=3600s)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"cached_at": fixed_now - 100, "data": {"response": "ok"}}),
        encoding="utf-8",
    )
    assert service._read_cache(cache_path) == {"response": "ok"}

    # 场景 b: TTL 外 (cached_at = now - 5000s, TTL=3600s)
    cache_path.write_text(
        json.dumps({"cached_at": fixed_now - 5000, "data": {"response": "stale"}}),
        encoding="utf-8",
    )
    assert service._read_cache(cache_path) is None

    # 场景 c: 缺 cached_at 字段 (KeyError catch)
    cache_path.write_text(json.dumps({"data": {"response": "x"}}), encoding="utf-8")
    assert service._read_cache(cache_path) is None

    # 场景 d: 坏 JSON / 不存在文件
    assert service._read_cache(service.cache_dir / "nonexistent.json") is None
    cache_path.write_text("not-json", encoding="utf-8")
    assert service._read_cache(cache_path) is None


def test_d120_3_quota_path_is_cache_dir_quota_json(apifootball_mod):
    """D-120-3: _quota_path (L71-72) = cache_dir/quota.json."""
    mod, service, _ = apifootball_mod

    out = service._quota_path()

    assert out == service.cache_dir / "quota.json"
    assert out.name == "quota.json"


def test_d120_4_fixture_date_fallback_to_datetime_min(apifootball_mod):
    """D-120-4: _fixture_date (L162-172) 合法 ISO / 各种 fallback 路径."""
    mod, service, _ = apifootball_mod
    fallback = datetime.min.replace(tzinfo=timezone.utc)

    # 合法 ISO (with tz)
    iso_with_tz = "2025-10-17T14:30:00+08:00"
    out1 = service._fixture_date({"fixture": {"date": iso_with_tz}})
    assert out1 == datetime(2025, 10, 17, 14, 30, 0, tzinfo=timezone(timedelta(hours=8)))

    # 合法 ISO (naive -> 补 UTC)
    iso_naive = "2025-10-17T14:30:00"
    out2 = service._fixture_date({"fixture": {"date": iso_naive}})
    assert out2.year == 2025 and out2.tzinfo is not None

    # 缺 fixture.date -> fallback
    out3 = service._fixture_date({"fixture": {}})
    assert out3 == fallback

    # 缺 fixture -> fallback
    out4 = service._fixture_date({})
    assert out4 == fallback

    # date 非 str (None / int / list) -> fallback
    out5 = service._fixture_date({"fixture": {"date": None}})
    assert out5 == fallback
    out6 = service._fixture_date({"fixture": {"date": 12345}})
    assert out6 == fallback

    # 非法 ISO 字符串 -> fallback
    out7 = service._fixture_date({"fixture": {"date": "not-an-iso"}})
    assert out7 == fallback


def test_d120_5_cache_path_round_trip_with_read_cache(apifootball_mod):
    """D-120-5: _cache_path + _read_cache 完整 round-trip — 写 cache 后 _read_cache 命中.

    这是 _cache_path / _read_cache 集成契约, 也是 provider 缓存层核心循环.
    """
    mod, service, fixed_now = apifootball_mod

    # 生成 cache 路径 + 写文件 + _read_cache 命中
    cache_path = service._cache_path("fixtures", {"league": 39})
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"response": [{"fixture_id": 1, "home": "X"}], "errors": []}
    cache_path.write_text(
        json.dumps({"cached_at": fixed_now - 60, "data": payload}),
        encoding="utf-8",
    )

    # _read_cache 命中, 返 data 部分
    out = service._read_cache(cache_path)
    assert out == payload

    # 同 input 再生成 _cache_path 仍指向同一文件 (幂等)
    assert service._cache_path("fixtures", {"league": 39}) == cache_path
    assert service._read_cache(service._cache_path("fixtures", {"league": 39})) == payload


# 辅助导入 (为了 monkeypatch 让 fake_config 是 module-like)
from datetime import timedelta  # noqa: E402
