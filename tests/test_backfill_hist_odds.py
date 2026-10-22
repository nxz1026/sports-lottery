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

# 赛前赔率（生产实测口径）：ESPN 的赛前 1x2 在 summary["pickcenter"] 里，
# 三侧是**美式** moneyLine ⇒ 下面用十进制书写，测试桩会换算成美式再喂回去。
HOME_DEC = 2.10
DRAW_DEC = 3.40
AWAY_DEC = 4.00


def _decimal_to_american(decimal: float) -> float:
    """十进制 → 美式（ESPN 的 moneyLine 就是美式）。"""
    if decimal >= 2.0:
        return round((decimal - 1.0) * 100.0)
    return round(-100.0 / (decimal - 1.0))


# ────────────────────────── 假数据源 ──────────────────────────

def _event(event_id: str, home: str, away: str, date: str,
           home_id: str = "", away_id: str = "",
           home_score: str = "2", away_score: str = "1") -> dict:
    """造一个 ESPN scoreboard event（结构对齐实测返回）。"""
    return {
        "id": event_id,
        "date": f"{date}T15:00Z",
        "name": f"{away} at {home}",
        "competitions": [{
            "id": event_id,
            "competitors": [
                {"homeAway": "home", "team": {"id": home_id, "displayName": home},
                 "score": home_score},
                {"homeAway": "away", "team": {"id": away_id, "displayName": away},
                 "score": away_score},
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
                 errors: dict[str, Exception] | None = None,
                 live_odds: dict[str, tuple[float | None, float | None, float | None]] | None = None) -> None:
        # events: {(slug, dates): [event, ...]}；odds: {event_id: (home, draw, away)}
        # odds 里任一元素为 None ⇒ summary 里就**缺那一侧**（模拟 ESPN 少给一侧）。
        # live_odds: 只给**终场/滚球价**的 event（模拟生产踩到的那个坑）。
        self.events = events or {}
        self.odds = odds or {}
        self.errors = errors or {}
        self.live_odds = live_odds or {}
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
            if event_id in self.live_odds:
                return {"odds": [self._live_summary(self.live_odds[event_id])]}
            return {"pickcenter": [self._summary(self.odds.get(event_id))]}
        raise AssertionError(f"测试里出现未预期的 URL：{url}")

    @staticmethod
    def _summary(decimals: tuple[float | None, float | None, float | None] | None) -> dict:
        """赛前价：pickcenter 形态，三侧**美式** moneyLine（生产实测口径）。"""
        if decimals is None:
            return {"provider": {"name": "DraftKings"}}
        home, draw, away = decimals
        summary: dict = {"provider": {"id": "100", "name": "DraftKings"}}
        for key, value in (("homeTeamOdds", home), ("drawOdds", draw), ("awayTeamOdds", away)):
            if value is None:
                continue
            summary[key] = {"moneyLine": _decimal_to_american(value)}
        return summary

    @staticmethod
    def _live_summary(decimals: tuple[float | None, float | None, float | None] | None) -> dict:
        """终场/滚球价：odds[] 形态，三侧**十进制** odds.value（生产实测口径）。

        ⚠️ 这是 2026-10-21 被证伪的旧口径 —— 谁赢谁 500/1 ⇒ 回填会造出
        「热门 100% 全胜」的假象 ⇒ 脚本必须**不采信**它。
        """
        if decimals is None:
            return {"provider": {"id": "2000", "name": "Bet 365"}}
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


# ────────────────────────── 11. 生产实测形态（赛前价 vs 终场价）──────────────────────────

def test_american_moneyline_converts_to_decimal():
    """赛前价是**美式** moneyLine ⇒ 必须自己换算成十进制。"""
    assert bf.american_to_decimal(-500) == pytest.approx(1.2)
    assert bf.american_to_decimal(600.0) == pytest.approx(7.0)
    assert bf.american_to_decimal(145) == pytest.approx(2.45)
    assert bf.american_to_decimal(+200) == pytest.approx(3.0)
    # 非数字 / 0 / 换算出来 ≤ 1 ⇒ 当缺失
    assert bf.american_to_decimal(None) is None
    assert bf.american_to_decimal("abc") is None
    assert bf.american_to_decimal(0) is None


def test_live_odds_block_is_never_used_for_backfill():
    """⚠️ 终场/滚球价（``odds[].*.odds.value``）**绝不能**被采信。

    生产实测的坑：多特蒙德 4-1 毕尔巴鄂 ⇒ ``home=1/500(1.002)``、``away=450/1``；
    布拉格斯拉维亚 2-3 朗斯 ⇒ ``home=500/1(501.0)``、``away=1/66``。
    用它回填会造出「概率更高的一方 100% 全胜」的假象（实测 281/281）。
    """
    entry = _entry()
    fake = FakeESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
        live_odds={"740641": (1.002, 51.0, 451.0)},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["filled"] == 0
    assert stats["no_odds"] == 1
    assert entry["home_true_prob"] is None
    assert entry["odds_data_available"] is False


def test_pickcenter_is_preferred_over_live_odds():
    """同一份 summary 里 pickcenter（赛前）与 odds[]（终场）并存 ⇒ 取赛前的。"""
    entry = _entry()
    live_block = {
        "provider": {"id": "2000", "name": "Bet 365"},
        "homeTeamOdds": {"odds": {"summary": "1/500", "value": 1.002}},
        "drawOdds": {"summary": "50/1", "value": 51.0},
        "awayTeamOdds": {"odds": {"summary": "450/1", "value": 451.0}},
    }

    class _MixedESPN(FakeESPN):
        def __call__(self, url, timeout=25):
            if "summary" in url:
                self.calls.append(url)
                return {
                    "odds": [live_block],
                    "pickcenter": [{
                        "provider": {"id": "100", "name": "DraftKings"},
                        "homeTeamOdds": {"moneyLine": 110.0},
                        "drawOdds": {"moneyLine": 240.0},
                        "awayTeamOdds": {"moneyLine": 300.0},
                    }],
                }
            return super().__call__(url, timeout)

    fake = _MixedESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
    )

    _fill_in_place([entry], fake)

    assert entry["home_ml_implied"] == round(1 / HOME_DEC, 4)
    assert entry["draw_implied"] == round(1 / DRAW_DEC, 4)
    assert entry["away_ml_implied"] == round(1 / AWAY_DEC, 4)


def test_odds_block_moneyline_is_used_as_fallback():
    """没有 pickcenter 时，``odds[]`` 里带**块级 moneyLine** 的非滚球块可作兜底。"""
    entry = _entry()
    draftkings = {
        "provider": {"id": "100", "name": "DraftKings"},
        "homeTeamOdds": {"moneyLine": -500},
        "drawOdds": {"moneyLine": 600.0},
        "awayTeamOdds": {"moneyLine": 1400},
    }

    class _OnlyOddsESPN(FakeESPN):
        def __call__(self, url, timeout=25):
            if "summary" in url:
                self.calls.append(url)
                return {"odds": [draftkings]}
            return super().__call__(url, timeout)

    fake = _OnlyOddsESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
    )

    _fill_in_place([entry], fake)

    assert entry["home_ml_implied"] == round(1 / 1.2, 4)
    assert entry["draw_implied"] == round(1 / 7.0, 4)
    assert entry["away_ml_implied"] == round(1 / 15.0, 4)


def test_live_provider_block_is_skipped():
    """provider 名含 ``live`` 的块（ESPN BET - Live Odds）是滚球 ⇒ 跳过。"""
    entry = _entry()
    live_provider = {
        "provider": {"id": "59", "name": "ESPN BET - Live Odds"},
        "homeTeamOdds": {"moneyLine": -10000},
        "drawOdds": {"moneyLine": 600.0},
        "awayTeamOdds": {"moneyLine": 1400},
    }

    class _LiveProviderESPN(FakeESPN):
        def __call__(self, url, timeout=25):
            if "summary" in url:
                self.calls.append(url)
                return {"odds": [live_provider]}
            return super().__call__(url, timeout)

    fake = _LiveProviderESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["filled"] == 0
    assert entry["home_true_prob"] is None


def test_implausible_odds_block_is_rejected():
    """实测踩过的坑：ESPN 偶尔给不自洽的块（平局 1.364 ⇒ 平局概率 73%）⇒ 整块丢掉。"""
    entry = _entry()
    # draw -175 ⇒ 十进制 1.571 ⇒ 平局概率 63%，足球不可能 ⇒ 整块丢掉
    garbled = {
        "provider": {"id": "100", "name": "DraftKings"},
        "homeTeamOdds": {"moneyLine": 275.0},
        "drawOdds": {"moneyLine": -175.0},
        "awayTeamOdds": {"moneyLine": 1600.0},
    }

    class _GarbledESPN(FakeESPN):
        def __call__(self, url, timeout=25):
            if "summary" in url:
                self.calls.append(url)
                return {"pickcenter": [garbled]}
            return super().__call__(url, timeout)

    fake = _GarbledESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["filled"] == 0
    assert stats["no_odds"] == 1
    assert entry["home_true_prob"] is None
    assert entry["odds_data_available"] is False


def test_prematch_implied_probability_cap_rejects_certain_winner():
    """任一侧隐含概率 > 97% ⇒ 那是终场价（赛前不可能）⇒ 整块丢掉。"""
    entry = _entry()
    # home 1.005 ⇒ 隐含 99.5% ⇒ 典型的终场价特征
    certain = {
        "provider": {"id": "100", "name": "DraftKings"},
        "homeTeamOdds": {"moneyLine": -20000},
        "drawOdds": {"moneyLine": 5000.0},
        "awayTeamOdds": {"moneyLine": 50000.0},
    }

    class _CertainESPN(FakeESPN):
        def __call__(self, url, timeout=25):
            if "summary" in url:
                self.calls.append(url)
                return {"pickcenter": [certain]}
            return super().__call__(url, timeout)

    fake = _CertainESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20")]},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["filled"] == 0
    assert entry["home_true_prob"] is None


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
        "provider": {"id": "100", "name": "DraftKings"},
        "homeTeamOdds": {"teamId": "999", "moneyLine": -400.0},
        "drawOdds": {"moneyLine": 300.0},
        "awayTeamOdds": {"teamId": "382", "moneyLine": 500.0},
    }

    class _SwappedESPN(FakeESPN):
        def __call__(self, url, timeout=25):
            if "summary" in url:
                self.calls.append(url)
                return {"pickcenter": [swapped]}
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
        "provider": {"id": "100", "name": "DraftKings"},
        "homeTeamOdds": {"teamId": "382", "moneyLine": -400.0},
        "drawOdds": {"moneyLine": 300.0},
        "awayTeamOdds": {"teamId": "364", "moneyLine": 500.0},
    }

    class _OkESPN(FakeESPN):
        def __call__(self, url, timeout=25):
            if "summary" in url:
                self.calls.append(url)
                return {"pickcenter": [ok]}
            return super().__call__(url, timeout)

    fake = _OkESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20",
                                           home_id="382", away_id="364")]},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["filled"] == 1
    assert entry["home_ml_implied"] == round(1 / 1.25, 4)


# ────────────────────────── 12. 比分一致性校验 ──────────────────────────

def test_score_mismatch_is_skipped():
    """event 的最终比分与样本 score 不一致 ⇒ 整场不填（同名同日期的别的赛事会骗过纯键匹配）。"""
    entry = _entry(score="2-1")
    fake = FakeESPN(
        # 同一队名同日期，但 ESPN 那场的比分是 5-0 ⇒ 不是样本这一场
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20",
                                           home_score="5", away_score="0")]},
        odds={"740641": (HOME_DEC, DRAW_DEC, AWAY_DEC)},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["filled"] == 0
    assert stats["score_mismatch"] == 1
    assert stats["unmatched"] == 1
    assert entry["home_true_prob"] is None
    assert fake.summary_calls() == [], "比分都对不上 ⇒ 不该去请求 summary"


def test_matching_score_is_accepted():
    """event 比分与样本一致 ⇒ 正常填（这道闸不能误杀好数据）。"""
    entry = _entry(score="2-1")
    fake = FakeESPN(
        events={("eng.1", "2025"): [_event("740641", "Arsenal", "Chelsea", "2025-09-20",
                                           home_score="2", away_score="1")]},
        odds={"740641": (HOME_DEC, DRAW_DEC, AWAY_DEC)},
    )

    stats = _fill_in_place([entry], fake)

    assert stats["filled"] == 1
    assert stats["score_mismatch"] == 0


# ────────────────────────── 13. 统计合理性闸 ──────────────────────────

def _make_entries(n: int, winners: str) -> list[dict]:
    """造 n 条样本：winners = "home" ⇒ 主队全赢；"away" ⇒ 客队全赢；"mixed" ⇒ 有输有赢。"""
    entries = []
    for i in range(n):
        if winners == "home":
            score, decimals = "2-1", (1.30, 5.0, 9.0)
        elif winners == "away":
            score, decimals = "1-2", (5.0, 4.0, 1.80)
        else:
            score, decimals = ("2-1" if i % 2 == 0 else "1-2"), (2.2, 3.3, 3.3)
        entries.append(_entry(score=score, home=f"H{i}", away=f"A{i}",
                              home_en=f"Home {i} FC", away_en=f"Away {i} FC",
                              date=f"2025-09-{(i % 28) + 1:02d}"))
    return entries


def _fake_for_entries(entries: list[dict], decimals: tuple[float, float, float]) -> FakeESPN:
    """给一批条目造对应的 ESPN 桩（每条一个 event，比分与样本一致）。"""
    events = []
    odds = {}
    for i, entry in enumerate(entries):
        event_id = f"evt{i}"
        home_score, away_score = entry["score"].split("-")
        events.append(_event(event_id, entry["home_en"], entry["away_en"],
                             entry["kickoff_utc"][:10],
                             home_score=home_score, away_score=away_score))
        odds[event_id] = decimals
    return FakeESPN(events={("eng.1", "2025"): events}, odds=odds)


def _fill_and_audit(entries: list[dict], decimals: tuple[float, float, float]) -> list[dict]:
    """跑一遍回填（用桩数据源），返回被就地填过的 entries。"""
    fake = _fake_for_entries(entries, decimals)
    stats, failures = bf.run_backfill(entries, fake, timeout=25, sleep=0, max_workers=2)
    assert failures == []
    assert stats.filled == len(entries)
    return entries


def _make_calibrated_entries(n: int = 60) -> list[dict]:
    """造 n 条**与赔率隐含分布一致**的样本（体检应当放行）。

    赔率 (2.2, 3.3, 3.3) ⇒ 去水后隐含 主胜 42.9% / 平局 28.6% / 客胜 28.6%；
    热门条件胜率期望 = 0.429 / (0.429 + 0.286) = 60.0%。
    ⇒ 60 场里 26 主胜 / 17 平局 / 17 客胜，正好命中期望。
    """
    entries = []
    for i in range(n):
        if i < 26:
            score = "2-1"
        elif i < 43:
            score = "1-1"
        else:
            score = "1-2"
        entries.append(_entry(score=score, home=f"H{i}", away=f"A{i}",
                              home_en=f"Home {i} FC", away_en=f"Away {i} FC",
                              date=f"2025-09-{(i % 28) + 1:02d}"))
    return entries


def test_audit_rejects_favorite_always_wins():
    """概率更高的一方全赢 ⇒ 实际分布与赔率隐含分布对不上 ⇒ 体检不通过。"""
    entries = _make_entries(60, "home")
    audit = bf.validate_filled_sample(_fill_and_audit(entries, (1.30, 5.0, 9.0)))

    assert audit.ok is False
    assert any("实际分布" in problem for problem in audit.problems)


def test_audit_passes_for_calibrated_sample():
    """赛果分布与赔率隐含分布一致 ⇒ 体检通过（**不许误伤好数据**）。"""
    entries = _make_calibrated_entries(60)
    audit = bf.validate_filled_sample(_fill_and_audit(entries, (2.2, 3.3, 3.3)))

    assert audit.ok is True
    assert audit.problems == []


def test_audit_flags_collapsed_draw_rate():
    """有赔率子集一场平局都没有，但赔率说平局概率 28.6% ⇒ 体检不通过。"""
    entries = _make_entries(60, "mixed")  # "mixed" = 主客交替、全是分胜负
    audit = bf.validate_filled_sample(_fill_and_audit(entries, (2.2, 3.3, 3.3)))

    assert audit.ok is False
    assert any("平局" in problem for problem in audit.problems)


def test_audit_flags_too_many_certain_favorites():
    """「几乎确定」的热门占比过高 ⇒ 终场价的指纹 ⇒ 体检不通过。

    赔率 (1.04, 15, 30) ⇒ 原始隐含 0.9615（没过每场 0.97 的闸）、去水后热门 ≈ 0.906，
    赛果按这个隐含分布造（54 主胜 / 4 平 / 2 客胜）⇒ 只有「高置信占比」这一条会亮。
    """
    scores = ["2-1"] * 54 + ["1-1"] * 4 + ["1-2"] * 2
    entries = []
    for i, score in enumerate(scores):
        entries.append(_entry(score=score, home=f"H{i}", away=f"A{i}",
                              home_en=f"Home {i} FC", away_en=f"Away {i} FC",
                              date=f"2025-09-{(i % 28) + 1:02d}"))
    filled = _fill_and_audit(entries, (1.04, 15.0, 30.0))
    audit = bf.validate_filled_sample(filled)

    assert audit.ok is False
    assert any("隐含概率" in problem for problem in audit.problems)


def test_audit_skips_small_samples():
    """样本太少 ⇒ 没有统计效力 ⇒ 只记 note，不判刑。"""
    entries = _make_calibrated_entries(10)
    audit = bf.validate_filled_sample(_fill_and_audit(entries, (2.2, 3.3, 3.3)))

    assert audit.ok is True
    assert any("无效力" in note for note in audit.notes)


def test_cli_refuses_to_write_when_audit_fails(tmp_path, capsys):
    """体检不过 ⇒ 退出码 1 且**文件一个字节都不动**。"""
    entries = _make_entries(60, "home")
    path = _write_sample(tmp_path / "sample.json", entries)
    before = path.read_bytes()
    fake = _fake_for_entries(entries, (1.30, 5.0, 9.0))

    exit_code = _run_cli(path, fake, ["--execute"])

    assert exit_code == 1
    assert path.read_bytes() == before, "体检不过就不许写盘"
    assert "统计体检不通过" in capsys.readouterr().err
