"""backfill_hist_odds.py 的行为锁（**全程离线**）。

被测脚本的取数入口 ``fetch_json(url, timeout)`` 是可替换的纯函数边界，
测试统一注入假 fetcher（``FakeESPN``）⇒ **测试里不会出现任何真实网络调用**
（另有 autouse fixture 把真 fetch_json 换成会炸的桩，双保险）。

锁住的口径：
  · 三侧赔率齐全 ⇒ 真实概率 / 隐含概率 / odds_data_available 都填对，且与
    ``core.data.parse.remove_vig`` 的输出一致（去水口径不另起炉灶）；
  · **只填不覆盖**（已有值不动）⇒ 幂等，重跑逐字相同；
  · 缺一侧赔率 ⇒ 整场不填（不填一半）；
  · 队名/日期对不上 ⇒ 不匹配（**绝不模糊匹配**）；
  · 未知 league ⇒ 明确跳过并计数，不抛未捕获异常；
  · 默认干跑不写盘，``--execute`` 才写；文件不存在 ⇒ 退出码 2；
  · 统计数字自洽（总数 = 命中 + 未匹配 + 失败）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
for _p in (str(REPO_ROOT), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backfill_hist_odds as bf  # noqa: E402
from core.data.parse import remove_vig  # noqa: E402

# 十进制赔率（生产 summary 里就是这套形态：homeTeamOdds/drawOdds/awayTeamOdds.odds.value）
HOME_DEC = 2.10
DRAW_DEC = 3.40
AWAY_DEC = 4.00


# ────────────────────────── 假数据源 ──────────────────────────

def _event(event_id: str, home: str, away: str, date: str,
           home_id: str = "", away_id: str = "") -> dict:
    """造一个 ESPN scoreboard event（结构对齐实测返回）。"""
    return {
        "id": event_id,
        "date": f"{date}T15:00Z",
        "name": f"{away} at {home}",
        "competitions": [{
            "id": event_id,
            "competitors": [
                {"homeAway": "home", "team": {"id": home_id, "displayName": home}},
                {"homeAway": "away", "team": {"id": away_id, "displayName": away}},
            ],
            "odds": [None],  # 已完赛场次的 scoreboard 就是没有赔率（实测）
        }],
    }


def _entry(league: str = "epl", home: str = "Arsenal FC", away: str = "Chelsea FC",
           date: str = "2025-09-20", **over) -> dict:
    """造一条历史样本条目（字段形态对齐生产实测：赔率字段全是 null/false）。"""
    entry = {
        "name": "阿森纳 vs 切尔西",
        "status": "STATUS_FULL_TIME",
        "completed": True,
        "kickoff_utc": f"{date}T15:00Z",
        "time_to_kickoff_h": -100.0,
        "home": "阿森纳",
        "away": "切尔西",
        "home_en": home,
        "away_en": away,
        "home_abbr": "ARS",
        "away_abbr": "CHE",
        "score": "2-1",
        "home_form": "DWDDW",
        "away_form": "LWDWW",
        "home_form_score": 0.5,
        "away_form_score": 0.5,
        "home_record": "1-0-0",
        "away_record": "1-0-0",
        "home_record_score": 0.5,
        "away_record_score": 0.5,
        "ml_home_close": None,
        "draw_ml": None,
        "draw_implied": None,
        "home_true_prob": None,
        "draw_true_prob": None,
        "away_true_prob": None,
        "spread_home_line": "",
        "spread_home_close_odds": "",
        "spread_movement_score": 0.0,
        "total_over_close": "",
        "total_under_close": "",
        "odds_data_available": False,
        "league": league,
    }
    entry.update(over)
    return entry


class FakeESPN:
    """假 ESPN 取数器：按 URL 分派 scoreboard / summary，全程不碰网络。"""

    def __init__(self, events: dict[tuple[str, str], list[dict]] | None = None,
                 odds: dict[str, tuple[float | None, float | None, float | None]] | None = None,
                 errors: dict[str, Exception] | None = None) -> None:
        # events: {(slug, dates): [event, ...]}；odds: {event_id: (home, draw, away)}
        # odds 里任一元素为 None ⇒ summary 里就**缺那一侧**（模拟 ESPN 少给一侧）。
        self.events = events or {}
        self.odds = odds or {}
        self.errors = errors or {}
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float = 25) -> dict:
        self.calls.append(url)
        if "scoreboard" in url:
            slug, dates = self._parse_scoreboard(url)
            return {"events": self.events.get((slug, dates), [])}
        if "summary" in url:
            event_id = self._parse_event(url)
            if event_id in self.errors:
                raise self.errors[event_id]
            return {"odds": [self._summary(self.odds.get(event_id))]}
        raise AssertionError(f"测试里出现未预期的 URL：{url}")

    @staticmethod
    def _summary(decimals: tuple[float | None, float | None, float | None] | None) -> dict:
        if decimals is None:
            return {"provider": {"name": "Bet 365"}}
        home, draw, away = decimals
        summary: dict = {"provider": {"id": "2000", "name": "Bet 365"}}
        for key, value in (("homeTeamOdds", home), ("drawOdds", draw), ("awayTeamOdds", away)):
            if value is None:
                continue
            summary[key] = {"odds": {"summary": str(value), "value": value}}
        return summary

    @staticmethod
    def _parse_scoreboard(url: str) -> tuple[str, str]:
        slug = url.split("/soccer/")[1].split("/scoreboard")[0]
        dates = url.split("dates=")[1].split("&")[0]
        return slug, dates

    @staticmethod
    def _parse_event(url: str) -> str:
        return url.split("event=")[1].split("&")[0]

    def summary_calls(self) -> list[str]:
        return [url for url in self.calls if "summary" in url]


def _fake_basic() -> FakeESPN:
    """标准场景：scoreboard 有 Arsenal vs Chelsea（2025-09-20），summary 三侧齐全。"""
    return FakeESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
        odds={"740641": (HOME_DEC, DRAW_DEC, AWAY_DEC)},
    )


@pytest.fixture(autouse=True)
def _forbid_real_network(monkeypatch):
    """把真 fetch_json 换成会炸的桩 ⇒ 测试里真联网会立刻失败（而不是悄悄跑出去）。"""

    def _boom(url, timeout=25):
        raise AssertionError(f"测试里不许真联网，却调用了 fetch_json：{url}")

    monkeypatch.setattr(bf, "fetch_json", _boom)


def _write_sample(path: Path, entries: list[dict]) -> Path:
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _fill_in_place(entries: list[dict], fake: FakeESPN) -> dict:
    """直接跑核心流程（就地改 entries）——用来断言「填了什么」。"""
    stats, failures = bf.run_backfill(entries, fake, timeout=25, sleep=0, max_workers=2)
    assert failures == []
    return stats.as_dict()


def _run_cli(path: Path, fake: FakeESPN, extra: list[str] | None = None) -> int:
    return bf.main(["--file", str(path), "--sleep", "0", *(extra or [])], fetcher=fake)


def _stats_from_output(out: str) -> dict:
    """从 stdout 里抠出统计 JSON 行（格式固定，测试逐字核对用）。"""
    for line in out.splitlines():
        if line.startswith("[backfill] 统计："):
            return json.loads(line.split("：", 1)[1])
    raise AssertionError(f"输出里没有统计行：\n{out}")


# ────────────────────────── 1. 正常填数 ──────────────────────────

def test_fills_true_probs_and_implied_from_summary():
    """三侧赔率齐全 ⇒ 真实概率 / 隐含概率 / odds_data_available 全填，且与 remove_vig 一致。"""
    entry = _entry()
    stats = _fill_in_place([entry], _fake_basic())

    home_true, draw_true, away_true = remove_vig(1 / HOME_DEC, 1 / DRAW_DEC, 1 / AWAY_DEC)
    assert entry["home_true_prob"] == round(home_true, 4)
    assert entry["draw_true_prob"] == round(draw_true, 4)
    assert entry["away_true_prob"] == round(away_true, 4)
    assert entry["home_ml_implied"] == round(1 / HOME_DEC, 4)
    assert entry["draw_implied"] == round(1 / DRAW_DEC, 4)
    assert entry["away_ml_implied"] == round(1 / AWAY_DEC, 4)
    assert entry["odds_data_available"] is True
    assert stats["filled"] == 1
    # 去水后三侧概率之和仍是 1（否则就是口径坏了）
    total = entry["home_true_prob"] + entry["draw_true_prob"] + entry["away_true_prob"]
    assert total == pytest.approx(1.0, abs=1e-3)


def test_does_not_touch_other_fields():
    """只填赔率字段 ⇒ score/status/kickoff_utc/ml_home_close 等一个都不许动。"""
    entry = _entry()
    before = dict(entry)

    _fill_in_place([entry], _fake_basic())

    for key in ("score", "status", "kickoff_utc", "name", "league", "ml_home_close", "draw_ml"):
        assert entry[key] == before[key], key
    assert entry["ml_home_close"] is None
    assert entry["draw_ml"] is None


# ────────────────────────── 2. 只填不覆盖 ──────────────────────────

def test_existing_value_is_not_overwritten():
    """已有 home_true_prob=0.7 ⇒ 跑完仍是 0.7（绝不覆盖已有值）。"""
    entry = _entry(home_true_prob=0.7)

    _fill_in_place([entry], _fake_basic())

    assert entry["home_true_prob"] == 0.7
    # 其余缺的还是照填
    assert entry["draw_true_prob"] is not None
    assert entry["away_true_prob"] is not None


def test_existing_odds_data_available_true_is_kept():
    """odds_data_available 已是 true ⇒ 不动（已填的不动）。"""
    entry = _entry(odds_data_available=True)

    _fill_in_place([entry], _fake_basic())

    assert entry["odds_data_available"] is True


# ────────────────────────── 3. 幂等 ──────────────────────────

def test_second_run_changes_nothing(tmp_path):
    """同一输入跑两次 ⇒ 文件内容逐字相同，第二次新增 0 条。"""
    path = _write_sample(tmp_path / "sample.json", [_entry()])
    fake = _fake_basic()

    assert _run_cli(path, fake, ["--execute"]) == 0
    first = path.read_bytes()

    assert _run_cli(path, fake, ["--execute"]) == 0
    second = path.read_bytes()

    assert first == second
    payload = json.loads(second.decode("utf-8"))
    assert payload[0]["home_true_prob"] is not None


def test_second_run_reports_zero_new_entries(tmp_path, capsys):
    """第二次跑：命中仍在，但「成功填赔率」= 0（新增 0 条）。"""
    path = _write_sample(tmp_path / "sample.json", [_entry()])
    fake = _fake_basic()

    assert _run_cli(path, fake, ["--execute"]) == 0
    capsys.readouterr()
    assert _run_cli(path, fake, ["--execute"]) == 0
    stats = _stats_from_output(capsys.readouterr().out)

    assert stats["matched"] == 1
    assert stats["filled"] == 0
    assert stats["already_filled"] == 1


# ────────────────────────── 4. 缺一侧赔率 ⇒ 整场不填 ──────────────────────────

def test_missing_away_odds_fills_nothing():
    """summary 缺一侧 ⇒ 该场**一个字段都不填**（不填一半），且计入「未成功」。"""
    entry = _entry()
    fake = FakeESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
        odds={"740641": (HOME_DEC, DRAW_DEC, None)},  # 缺客胜
    )

    stats = _fill_in_place([entry], fake)

    assert entry["home_true_prob"] is None
    assert entry["draw_true_prob"] is None
    assert entry["away_true_prob"] is None
    assert entry.get("home_ml_implied") is None
    assert entry.get("away_ml_implied") is None
    assert entry["odds_data_available"] is False
    assert stats["filled"] == 0
    assert stats["no_odds"] == 1


def test_missing_odds_counts_as_not_filled():
    """缺一侧赔率 ⇒ 命中 event 但「成功填赔率」= 0。"""
    entry = _entry()
    fake = FakeESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
        odds={"740641": (HOME_DEC, None, AWAY_DEC)},  # 缺平局
    )

    stats = _fill_in_place([entry], fake)

    assert stats["matched"] == 1
    assert stats["filled"] == 0
    assert stats["no_odds"] == 1


# ────────────────────────── 5. 认不上 ⇒ 不匹配（绝不模糊匹配） ──────────────────────────

def test_team_name_mismatch_is_not_matched():
    """队名对不上 ⇒ 不匹配、不填、计数 +1，且**不会**去请求 summary。"""
    entry = _entry(home="Arsenal FC", away="Chelsea FC")
    fake = FakeESPN(
        events={("eng.1", "2025"): [_event("740641", "Tottenham", "Chelsea", "2025-09-20")]},
        odds={"740641": (HOME_DEC, DRAW_DEC, AWAY_DEC)},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["matched"] == 0
    assert stats["unmatched"] == 1
    assert stats["filled"] == 0
    assert entry["home_true_prob"] is None
    assert fake.summary_calls() == [], "没匹配上就不该去抓 summary"


def test_date_mismatch_is_not_matched():
    """队名对但日期对不上 ⇒ 不匹配（日期是匹配键的一部分）。"""
    entry = _entry(date="2025-09-21")
    fake = FakeESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
        odds={"740641": (HOME_DEC, DRAW_DEC, AWAY_DEC)},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["unmatched"] == 1
    assert entry["home_true_prob"] is None


def test_similar_team_name_is_not_fuzzy_matched():
    """相近队名（如 Manchester United vs Manchester City）**不许**模糊匹配上。"""
    entry = _entry(home="Manchester United FC", away="Manchester City FC")
    fake = FakeESPN(
        events={("eng.1", "2025"): [_event("740641", "Manchester City", "Arsenal", "2025-09-20")]},
        odds={"740641": (HOME_DEC, DRAW_DEC, AWAY_DEC)},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["unmatched"] == 1
    assert entry["home_true_prob"] is None


# ────────────────────────── 6. 未知 league ──────────────────────────

def test_unknown_league_is_skipped_and_counted():
    """未知 league ⇒ 明确跳过并计数告警，不抛未捕获异常，也不去猜 slug。"""
    entry = _entry(league="xyz")
    fake = _fake_basic()

    stats = _fill_in_place([entry], fake)

    assert stats["unknown_league"] == 1
    assert stats["unmatched"] == 1
    assert stats["matched"] == 0
    assert entry["home_true_prob"] is None
    # 未知 league 不该产生任何 scoreboard 请求（更不能猜 slug）
    assert fake.calls == []


def test_unknown_league_argument_returns_2(tmp_path):
    """--leagues 传未知联赛 ⇒ 参数错误 ⇒ 退出码 2。"""
    path = _write_sample(tmp_path / "sample.json", [_entry()])
    assert bf.main(["--file", str(path), "--leagues", "xyz"], fetcher=_fake_basic()) == 2


# ────────────────────────── 7. 干跑 / 写盘 ──────────────────────────

def test_dry_run_does_not_write_file(tmp_path):
    """默认（干跑）⇒ 文件内容与 mtime 都不变。"""
    path = _write_sample(tmp_path / "sample.json", [_entry()])
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    assert _run_cli(path, _fake_basic()) == 0

    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime


def test_execute_writes_file(tmp_path):
    """--execute ⇒ 真写回，且字段落盘。"""
    path = _write_sample(tmp_path / "sample.json", [_entry()])
    before_bytes = path.read_bytes()

    assert _run_cli(path, _fake_basic(), ["--execute"]) == 0

    assert path.read_bytes() != before_bytes
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload[0]["home_true_prob"] is not None
    assert payload[0]["odds_data_available"] is True


def test_dry_run_and_execute_are_mutually_exclusive(tmp_path):
    """--dry-run 与 --execute 互斥 ⇒ 退出码 2（别让人以为写进去了）。"""
    path = _write_sample(tmp_path / "sample.json", [_entry()])
    assert bf.main(["--file", str(path), "--dry-run", "--execute"], fetcher=_fake_basic()) == 2


# ────────────────────────── 8. 统计自洽 ──────────────────────────

def test_stats_are_self_consistent(tmp_path, capsys):
    """统计必须自洽：总数 = 命中 + 未匹配 + 失败；命中 = 填上 + 无赔率 + 已有值。"""
    entries = [
        _entry(),                                             # 命中且能填
        _entry(home="Tottenham FC"),                          # 认不上（队名）
        _entry(league="xyz"),                                 # 未知 league
        _entry(away="Everton FC"),                            # 认不上
    ]
    path = _write_sample(tmp_path / "sample.json", entries)
    fake = FakeESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
        odds={"740641": (HOME_DEC, DRAW_DEC, AWAY_DEC)},
    )

    assert _run_cli(path, fake) == 0
    stats = _stats_from_output(capsys.readouterr().out)

    assert stats["total"] == 4
    assert stats["total"] == stats["matched"] + stats["unmatched"]
    assert stats["matched"] == (stats["filled"] + stats["no_odds"]
                                + stats["already_filled"] + stats["fetch_failed"])
    assert stats["filled"] == 1
    assert stats["unmatched"] == 3
    assert stats["unknown_league"] == 1
    assert stats["leagues"] == {"epl": 3, "xyz": 1}


def test_fetch_failure_is_counted_and_returns_1(tmp_path, capsys):
    """summary 抓取抛异常 ⇒ 计入「抓取失败」，失败率过高 ⇒ 退出码 1。"""
    path = _write_sample(tmp_path / "sample.json", [_entry()])
    fake = FakeESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
        errors={"740641": RuntimeError("boom")},
    )

    assert _run_cli(path, fake) == 1
    stats = _stats_from_output(capsys.readouterr().out)

    assert stats["fetch_failed"] == 1
    assert stats["filled"] == 0
    assert stats["total"] == stats["matched"] + stats["unmatched"]
    assert stats["matched"] == (stats["filled"] + stats["no_odds"]
                                + stats["already_filled"] + stats["fetch_failed"])


# ────────────────────────── 9. 输入错误 ──────────────────────────

def test_missing_file_returns_2(tmp_path, capsys):
    """样本文件不存在 ⇒ 退出码 2 且报错清晰（不许静默新建空文件）。"""
    missing = tmp_path / "nope.json"
    assert bf.main(["--file", str(missing)], fetcher=_fake_basic()) == 2
    err = capsys.readouterr().err
    assert "不存在" in err
    assert not missing.exists(), "不许因为文件缺失就新建一个"


def test_invalid_json_returns_2(tmp_path):
    """文件不是合法 JSON ⇒ 退出码 2。"""
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert bf.main(["--file", str(path)], fetcher=_fake_basic()) == 2


def test_non_list_payload_returns_2(tmp_path):
    """顶层不是 list ⇒ 退出码 2。"""
    path = tmp_path / "obj.json"
    path.write_text(json.dumps({"matches": []}), encoding="utf-8")
    assert bf.main(["--file", str(path)], fetcher=_fake_basic()) == 2


# ────────────────────────── 10. 其它口径 ──────────────────────────

def test_league_filter_and_limit(tmp_path, capsys):
    """--leagues 只处理指定联赛；--limit 只处理前 N 条。"""
    entries = [
        _entry(league="epl"),
        _entry(league="laliga", home="Real Madrid CF", away="Sevilla FC"),
        _entry(league="ucl"),
    ]
    path = _write_sample(tmp_path / "sample.json", entries)
    fake = FakeESPN(
        events={
            ("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")],
            ("esp.1", "2025"): [_event("700002", "Real Madrid", "Sevilla", "2025-09-20")],
        },
        odds={"740641": (HOME_DEC, DRAW_DEC, AWAY_DEC), "700002": (2.5, 3.2, 3.0)},
    )

    assert _run_cli(path, fake, ["--leagues", "epl,laliga"]) == 0
    stats = _stats_from_output(capsys.readouterr().out)

    assert stats["total"] == 2
    assert stats["skipped_league_filter"] == 1
    assert stats["filled"] == 2


def test_limit_only_processes_first_n(tmp_path, capsys):
    """--limit N ⇒ 只处理前 N 条。"""
    entries = [_entry(), _entry(home="Tottenham FC"), _entry(home="Liverpool FC")]
    path = _write_sample(tmp_path / "sample.json", entries)
    fake = FakeESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
        odds={"740641": (HOME_DEC, DRAW_DEC, AWAY_DEC)},
    )

    assert _run_cli(path, fake, ["--limit", "1"]) == 0
    stats = _stats_from_output(capsys.readouterr().out)

    assert stats["total"] == 1
    assert stats["filled"] == 1


def test_summary_request_uses_decimal_odds_only():
    """summary 只取十进制赔率 ⇒ 绝不写 ml_home_close / draw_ml（美式赔率口径不能混填）。"""
    entry = _entry()

    _fill_in_place([entry], _fake_basic())

    assert entry["ml_home_close"] is None
    assert entry["draw_ml"] is None


def test_league_slug_mapping_is_the_agreed_one():
    """联赛 slug 映射锁死（猜错 slug = 跨联赛错配）。"""
    assert bf.LEAGUE_SLUGS == {
        "ucl": "uefa.champions",
        "epl": "eng.1",
        "laliga": "esp.1",
        "seriea": "ita.1",
        "bundesliga": "ger.1",
        "ligue1": "fra.1",
    }


# ────────────────────────── 11. 生产实测形态（summary odds 的三种怪癖）──────────────────────────

def test_decimal_odds_reads_flat_draw_side():
    """Bet365 的 drawOdds 把 value **直接挂在侧上**（没有嵌套 odds）⇒ 也要认。"""
    assert bf._decimal_odds({"summary": "20/1", "value": 21.0, "handicap": 0.0}) == 21.0
    assert bf._decimal_odds({"team": {"x": 1}, "odds": {"summary": "1/66", "value": 1.015}}) == 1.015


def test_decimal_odds_rejects_american_moneyline():
    """``moneyLine`` 是美式赔率 ⇒ 绝不认（否则隐含概率全错）。"""
    assert bf._decimal_odds({"moneyLine": -500, "favorite": True}) is None
    assert bf._decimal_odds({"moneyLine": 600.0}) is None
    # 赔率 ≤ 1（隐含概率 ≥ 1）没有数学意义 ⇒ 当缺失
    assert bf._decimal_odds({"value": 1.0}) is None
    assert bf._decimal_odds({"value": 0.8}) is None
    # handicap ≠ 0 ⇒ 那是让球盘不是 1x2
    assert bf._decimal_odds({"odds": {"value": 1.9, "handicap": -1.5}}) is None


def test_bet365_block_is_found_even_when_not_first():
    """实测：odds 块顺序不固定，DraftKings 块可能排在 Bet365 前面 ⇒ 必须逐块扫。"""
    entry = _entry()
    draftkings = {
        "provider": {"id": "100", "name": "DraftKings"},
        "homeTeamOdds": {"moneyLine": -500},
        "drawOdds": {"moneyLine": 600.0},
        "awayTeamOdds": {"moneyLine": 1400},
    }
    bet365 = {
        "provider": {"id": "2000", "name": "Bet 365"},
        "homeTeamOdds": {"odds": {"summary": "1/150", "value": 1.007}},
        "drawOdds": {"summary": "28/1", "value": 29.0},
        "awayTeamOdds": {"odds": {"summary": "80/1", "value": 81.0}},
    }

    class _MultiBlockESPN(FakeESPN):
        def __call__(self, url, timeout=25):
            if "summary" in url:
                self.calls.append(url)
                return {"odds": [draftkings, bet365]}
            return super().__call__(url, timeout)

    fake = _MultiBlockESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
    )

    _fill_in_place([entry], fake)

    assert entry["home_ml_implied"] == round(1 / 1.007, 4)
    assert entry["draw_implied"] == round(1 / 29.0, 4)
    assert entry["away_ml_implied"] == round(1 / 81.0, 4)
    assert entry["home_true_prob"] is not None


def test_implausible_odds_block_is_rejected():
    """实测踩过的坑：ESPN 偶尔给不自洽的块（平局 1.364 ⇒ 平局概率 73%）⇒ 整块丢掉。"""
    entry = _entry()
    garbled = {
        "provider": {"id": "2000", "name": "Bet 365"},
        "homeTeamOdds": {"odds": {"summary": "11/4", "value": 3.75}},
        "drawOdds": {"summary": "4/11", "value": 1.364},
        "awayTeamOdds": {"odds": {"summary": "16/1", "value": 17.0}},
    }

    class _GarbledESPN(FakeESPN):
        def __call__(self, url, timeout=25):
            if "summary" in url:
                self.calls.append(url)
                return {"odds": [garbled]}
            return super().__call__(url, timeout)

    fake = _GarbledESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["filled"] == 0
    assert stats["no_odds"] == 1
    assert entry["home_true_prob"] is None
    assert entry["odds_data_available"] is False


def test_off_market_block_is_rejected():
    """三侧都是 ``value=0``（summary="OFF"，该场没开盘）⇒ 当缺失，不填。"""
    entry = _entry()
    off = {
        "provider": {"id": "2000", "name": "Bet 365"},
        "homeTeamOdds": {"odds": {"summary": "OFF", "value": 0.0}},
        "drawOdds": {"summary": "OFF", "value": 0.0},
        "awayTeamOdds": {"odds": "OFF", "value": 0.0},
    }

    class _OffESPN(FakeESPN):
        def __call__(self, url, timeout=25):
            if "summary" in url:
                self.calls.append(url)
                return {"odds": [off]}
            return super().__call__(url, timeout)

    fake = _OffESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["filled"] == 0
    assert entry["home_true_prob"] is None


def test_orientation_mismatch_is_rejected():
    """赔率块的主/客 team id 与 event 不一致 ⇒ 整块丢掉（镜像概率 = 脏数据）。"""
    entry = _entry()
    swapped = {
        "provider": {"id": "2000", "name": "Bet 365"},
        "homeTeamOdds": {"teamId": "999", "odds": {"value": 1.5}},
        "drawOdds": {"value": 4.0},
        "awayTeamOdds": {"teamId": "382", "odds": {"value": 6.0}},
    }

    class _SwappedESPN(FakeESPN):
        def __call__(self, url, timeout=25):
            if "summary" in url:
                self.calls.append(url)
                return {"odds": [swapped]}
            return super().__call__(url, timeout)

    fake = _SwappedESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20",
                                           home_id="382", away_id="364")]},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["filled"] == 0
    assert entry["home_true_prob"] is None


def test_matching_orientation_is_accepted():
    """主/客 team id 对得上 ⇒ 正常填（方向闸不能误杀好数据）。"""
    entry = _entry()
    ok = {
        "provider": {"id": "2000", "name": "Bet 365"},
        "homeTeamOdds": {"teamId": "382", "odds": {"value": 1.5}},
        "drawOdds": {"value": 4.0},
        "awayTeamOdds": {"teamId": "364", "odds": {"value": 6.0}},
    }

    class _OkESPN(FakeESPN):
        def __call__(self, url, timeout=25):
            if "summary" in url:
                self.calls.append(url)
                return {"odds": [ok]}
            return super().__call__(url, timeout)

    fake = _OkESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20",
                                           home_id="382", away_id="364")]},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["filled"] == 1
    assert entry["home_ml_implied"] == round(1 / 1.5, 4)
