"""采集器「赛果窗口 + 分页兜底 + 历史回补」的契约测试（2026-10-22）。

背景（生产实测）：竞彩赛果接口按 ``matchBeginDate=matchEndDate=开球日`` 过滤，
而**跨日完赛**（北京时间深夜开球、次日凌晨结束）的场次赛果**次日**才发布
⇒ 只查当天 ⇒ 那类场次的赛果**永久丢失**（457 场里丢 65 场，14%）。

本文件锁住三件事：
  ① 日期占位符只有**一处真源**（``_date_vars`` / ``_resolve``），且支持 ``{today_minus_2}``；
  ② ``pageSize`` 提到 100 时若接口拒绝 ⇒ **自动退回 30**（自愈，不静默）；
  ③ 回补按**逐日 + 翻页**抓，按 matchId 去重。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = REPO_ROOT / "collector-cn" / "collector.py"
CONFIG_PATH = REPO_ROOT / "collector-cn" / "config.json"


def _load_mod():
    spec = importlib.util.spec_from_file_location("collector_window_test", COLLECTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── ① 占位符 ────────────────────────────────────────────────────────────

def test_date_vars_supports_all_placeholders():
    mod = _load_mod()
    v = mod._date_vars("2026-10-22")
    assert v["{today}"] == "2026-10-22"
    assert v["{yesterday}"] == "2026-10-21"
    assert v["{today_minus_2}"] == "2026-10-20"
    assert v["{today_minus_3}"] == "2026-10-19"
    assert v["{today_minus_7}"] == "2026-10-15"


def test_resolve_replaces_every_date_placeholder():
    mod = _load_mod()
    tpl = ("...?matchBeginDate={today_minus_3}&matchEndDate={today_minus_3}"
           "&x={yesterday}&y={today_minus_2}&z={today}")
    out = mod._resolve(tpl, "2026-10-22")
    assert "matchBeginDate=2026-10-19&matchEndDate=2026-10-19" in out
    assert "x=2026-10-21" in out
    assert "y=2026-10-20" in out
    assert "z=2026-10-22" in out


def test_date_vars_without_base_date_uses_today():
    mod = _load_mod()
    v = mod._date_vars()
    assert "{today}" in v and "{today_minus_2}" in v


def test_placeholder_substitution_is_single_sourced():
    """占位符替换只留一处真源（collect / collect_topic 各调 _resolve）。"""
    src = COLLECTOR.read_text(encoding="utf-8")
    assert src.count('url.replace("{today}", today)') == 0
    assert src.count("_resolve(") >= 3      # collect / collect_topic / probe 用它


# ── ② 分页兜底 ──────────────────────────────────────────────────────────

def test_page_size_of_parses_url():
    mod = _load_mod()
    assert mod._page_size_of("...?pageSize=100&pageNo=1") == 100
    assert mod._page_size_of("...?pageNo=1") == 0


def test_fetch_with_page_fallback_retries_with_30(monkeypatch):
    mod = _load_mod()
    calls: list[str] = []

    def fake_fetch(url: str) -> dict:
        calls.append(url)
        if "pageSize=100" in url:
            return {"http_status": 400, "body": b"{}", "content_type": "application/json"}
        return {"http_status": 200, "body": b'{"errorCode":"0","success":true,"value":{}}'}

    monkeypatch.setattr(mod, "fetch", fake_fetch)
    rec, info = mod._fetch_with_page_fallback("https://x/?pageSize=100&pageNo=1")
    assert info is not None
    assert "pageSize=30" in info["to"]
    assert rec["http_status"] == 200
    assert len(calls) == 2


def test_fetch_with_page_fallback_no_fallback_when_ok(monkeypatch):
    mod = _load_mod()
    calls: list[str] = []

    def fake_fetch(url: str) -> dict:
        calls.append(url)
        return {"http_status": 200, "body": b'{"errorCode":"0","success":true,"value":{}}'}

    monkeypatch.setattr(mod, "fetch", fake_fetch)
    rec, info = mod._fetch_with_page_fallback("https://x/?pageSize=100&pageNo=1")
    assert info is None
    assert len(calls) == 1


def test_fetch_with_page_fallback_skips_when_already_30(monkeypatch):
    """本来就是 pageSize=30 ⇒ 失败另有原因，不该再打一次（免得白等）。"""
    mod = _load_mod()
    calls: list[str] = []

    def fake_fetch(url: str) -> dict:
        calls.append(url)
        return {"http_status": 500, "body": b"", "content_type": ""}

    monkeypatch.setattr(mod, "fetch", fake_fetch)
    rec, info = mod._fetch_with_page_fallback("https://x/?pageSize=30&pageNo=1")
    assert info is None
    assert len(calls) == 1


# ── ③ 配置：4 个单日窗口 ───────────────────────────────────────────────

def test_config_jczq_result_is_four_single_day_windows():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cands = cfg["topics"]["jczq_result"]["candidates"]
    assert len(cands) == 4
    # 每条 URL 都必须是「单日」：matchBeginDate == matchEndDate
    for url in cands:
        import re
        b = re.search(r"matchBeginDate=([^&]+)", url).group(1)
        e = re.search(r"matchEndDate=([^&]+)", url).group(1)
        assert b == e, url
    # 且 4 条覆盖 D-3 / D-2 / D-1 / D 四个不同日期
    dates = set()
    for url in cands:
        import re
        dates.add(re.search(r"matchBeginDate=([^&]+)", url).group(1))
    assert dates == {"{today_minus_3}", "{today_minus_2}", "{yesterday}", "{today}"}


def test_config_jczq_result_asks_bigger_page_size():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for url in cfg["topics"]["jczq_result"]["candidates"]:
        assert "pageSize=100" in url


def test_config_documents_why_the_window_changed():
    """配置里必须写清「为什么改成 4 天窗口」（后人别又改回单日）。"""
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    spec = cfg["topics"]["jczq_result"]
    assert "_why_window" in spec and "跨日" in spec["_why_window"]
    assert "_page_size" in spec


def test_basketball_result_window_is_untouched():
    """篮球的 7 日窗口是本来就对的，别被顺手改坏。"""
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    url = cfg["topics"]["jclq_result"]["candidates"][0]
    assert "{today_minus_7}" in url and "{today}" in url


# ── ④ 回补 ──────────────────────────────────────────────────────────────

def test_dates_between_is_inclusive():
    mod = _load_mod()
    days = mod._dates_between("2026-10-20", "2026-10-22")
    assert days == ["2026-10-20", "2026-10-21", "2026-10-22"]


def test_dates_between_rejects_reversed_range():
    mod = _load_mod()
    try:
        mod._dates_between("2026-10-22", "2026-10-20")
    except ValueError:
        return
    raise AssertionError("end 早于 start 应当抛 ValueError")


def test_backfill_range_fetches_each_day_and_pages(monkeypatch, tmp_path):
    """回补：逐日 × 逐页，且按 matchId 去重。"""
    mod = _load_mod()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "write_batch", lambda topic, rows: tmp_path / "batch.jsonl")
    monkeypatch.setattr(mod, "_update_status", lambda **kw: None)
    urls: list[str] = []

    def fake_fetch(url: str) -> dict:
        urls.append(url)
        import re
        day = re.search(r"matchBeginDate=([^&]+)", url).group(1)
        page = re.search(r"pageNo=(\d+)", url).group(1)
        mid = int(day.replace("-", "")) * 10 + int(page)
        payload = {"matchId": mid, "matchDate": day, "winFlag": "H"}
        body = json.dumps({"errorCode": "0", "success": True,
                           "value": {"matchResult": [payload]}}).encode("utf-8")
        return {"http_status": 200, "body": body, "content_type": "application/json"}

    monkeypatch.setattr(mod, "fetch", fake_fetch)
    rc = mod.backfill_range("2026-10-20", "2026-10-21", pages=2, push=False)
    assert rc == 0
    # 2 天 × 2 页 = 4 次请求
    assert len(urls) == 4
    assert "matchBeginDate=2026-10-20&matchEndDate=2026-10-20" in urls[0]
    assert "pageNo=1" in urls[0] and "pageNo=2" in urls[1]
    assert "matchBeginDate=2026-10-21&matchEndDate=2026-10-21" in urls[2]


def test_backfill_range_dedupes_by_match_id(monkeypatch, tmp_path):
    """同一 matchId 在多页/多日重复出现 ⇒ 只留一条。"""
    mod = _load_mod()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    written: dict = {}
    monkeypatch.setattr(mod, "write_batch",
                        lambda topic, rows: (written.update(rows=rows), tmp_path / "b.jsonl")[1])
    monkeypatch.setattr(mod, "_update_status", lambda **kw: None)

    def fake_fetch(url: str) -> dict:
        payload = {"matchId": 42, "matchDate": "2026-10-20"}
        body = json.dumps({"errorCode": "0", "success": True,
                           "value": {"matchResult": [payload]}}).encode("utf-8")
        return {"http_status": 200, "body": body, "content_type": "application/json"}

    monkeypatch.setattr(mod, "fetch", fake_fetch)
    mod.backfill_range("2026-10-20", "2026-10-21", pages=2, push=False)
    assert len(written["rows"]) == 1


def test_cli_exposes_backfill_range_flag():
    src = COLLECTOR.read_text(encoding="utf-8")
    assert '"--backfill-range"' in src
    assert "backfill_range(" in src
