#!/usr/bin/env python3
"""国内采集机：取官方 JSON → 落 JSONL → 推远端（契约 v1.3，2026-09-15 冻结）。

定位：独立小工具，只做取数/落盘/推送。不连 DB、不解析 HTML、不装重型依赖。
仅 stdlib（urllib/json/hashlib/pathlib/subprocess/tarfile/argparse）。

契约 v1.1（单一真源：doc/国内采集机实施文档-v1.md §5）：
- 通用行外壳：{"kind","topic","snap_ts","fetched_at","endpoint","http_status",
  "collector_host","payload","src_hash"}
- snap_ts = 请求发出时刻（UTC 带 Z），同批同值；官方更新时间留 payload 原样
- kind="error"：errorCode != "0" 或 success != true 时必须产行（失败响应无 value 键）
- payload 官方键名原样，不许改名/清洗/判奖
- src_hash = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
  separators=(",",":")).encode("utf-8")).hexdigest()

用法：
  python collector.py --probe            # 探针模式（v1 遗留，契约冻结后仅复探用）
  python collector.py --collect <topic>  # 采集指定 topic 落 JSONL
  python collector.py --collect-all      # 采集全部 8 topic
  python collector.py --push-batch <topic>...   # 定时任务主路径：B1 采集 + B2 tar 流式推 + G(A) .done 清单（league key-only）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CN_TZ = timezone(timedelta(hours=8))
UTC = timezone.utc
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

UA = CONFIG["ua"]
REFERER = CONFIG["referer"]
QPS = float(CONFIG["qps_interval"])
RETRIES = int(CONFIG["retries"])
BACKOFF = [float(x) for x in CONFIG["backoff"]]
HEAD_BYTES = int(CONFIG["probe_head_bytes"])
HOST = CONFIG["collector_host"]
SSH_ALIAS = CONFIG["push"]["ssh_alias"]
REMOTE_ROOT = CONFIG["push"]["remote_root"]
CONTRACT = "v1.3"

# 玩法块 → 官方 poolCode（§5.1 实测）
_POOL_CODE = {"had": "HAD", "hhad": "HHAD", "crs": "CRS", "ttg": "TTG", "hafu": "HAFU"}
_OFFER_BLOCKS = ("had", "hhad", "crs", "ttg", "hafu")
_ISSUE_GAMES = ("sfc", "jqc", "bqc")


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fname_ts() -> str:
    """文件名用 UTC 时间戳（冒号换 -，Windows 兼容）：2026-09-15T13-30Z。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%MZ")


def src_hash(payload: dict) -> str:
    """官方式子（§5.0）：sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",",":")))。"""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _req(url: str, ua: str, referer: str, timeout: int = 20) -> tuple[int, str, bytes]:
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Referer": referer,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return resp.status, resp.headers.get("Content-Type", ""), body


def fetch(url: str) -> dict:
    """带重试的 GET（指数退避 5/15/45s，QPS 节流）。返回探针记录。"""
    rec = {"url": url, "fetched_at": now_utc()}
    last_err = ""
    for attempt in range(RETRIES):
        try:
            time.sleep(QPS)
            status, ctype, body = _req(url, UA, REFERER)
            rec.update({"http_status": status, "content_type": ctype,
                        "body": body, "attempts": attempt + 1})
            return rec
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    rec.update({"http_status": 0, "content_type": "", "body": b"", "error": last_err,
                "attempts": RETRIES})
    return rec


def _ok(j: dict) -> bool:
    """成功判据（§5.0）：errorCode == "0"（字符串）且 success == true。"""
    return str(j.get("errorCode")) == "0" and j.get("success") is True


def _envelope(topic: str, url: str, rec: dict, snap_ts: str, payload: dict) -> dict:
    return {"kind": "line", "topic": topic, "snap_ts": snap_ts, "fetched_at": now_utc(),
            "endpoint": url, "http_status": rec.get("http_status", 0),
            "collector_host": HOST, "payload": payload, "src_hash": src_hash(payload)}


def _error_row(topic: str, url: str, rec: dict, snap_ts: str, body: bytes) -> dict:
    """失败行（§5.0）：errorCode != "0" 或 success != true。payload 原样放四键（失败响应无 value）。"""
    try:
        j = json.loads(body.decode("utf-8", errors="replace"))
        payload = {k: j.get(k) for k in ("errorCode", "errorMessage", "emptyFlag", "dataFrom")}
    except (ValueError, UnicodeDecodeError):
        payload = {"errorCode": "", "errorMessage": "non-json body", "emptyFlag": None, "dataFrom": None}
    return {"kind": "error", "topic": topic, "snap_ts": snap_ts, "fetched_at": now_utc(),
            "endpoint": url, "http_status": rec.get("http_status", 0),
            "collector_host": HOST, "payload": payload, "src_hash": src_hash(payload)}


def _offer_payload(sub: dict, block: str) -> dict:
    """§5.1：一场 × 一个玩法。官方键原样，options 整块 + oddsHistory 原样。"""
    return {
        "matchId": sub.get("matchId"), "matchNum": sub.get("matchNum"),
        "matchNumStr": sub.get("matchNumStr"), "matchNumDate": sub.get("matchNumDate"),
        "businessDate": sub.get("businessDate"), "matchDate": sub.get("matchDate"),
        "matchTime": sub.get("matchTime"), "leagueId": sub.get("leagueId"),
        "leagueAllName": sub.get("leagueAllName"), "leagueAbbName": sub.get("leagueAbbName"),
        "homeTeamId": sub.get("homeTeamId"), "awayTeamId": sub.get("awayTeamId"),
        "homeTeamAllName": sub.get("homeTeamAllName"), "awayTeamAllName": sub.get("awayTeamAllName"),
        "homeTeamAbbName": sub.get("homeTeamAbbName"), "awayTeamAbbName": sub.get("awayTeamAbbName"),
        "homeRank": sub.get("homeRank"), "awayRank": sub.get("awayRank"),
        "matchStatus": sub.get("matchStatus"), "sellStatus": sub.get("sellStatus"),
        "bettingSingle": sub.get("bettingSingle"), "bettingAllUp": sub.get("bettingAllUp"),
        "poolCode": _POOL_CODE[block], "block": block,
        "options": sub.get(block) or {}, "oddsHistory": sub.get("oddsList") or [],
    }


def parse_jczq_offer(url: str, rec: dict, snap_ts: str) -> list:
    j = json.loads(rec["body"].decode("utf-8", errors="replace"))
    if not _ok(j):
        return [_error_row("jczq_offer", url, rec, snap_ts, rec["body"])]
    rows = []
    for mi in (j.get("value") or {}).get("matchInfoList") or []:
        for sub in mi.get("subMatchList") or []:
            for block in _OFFER_BLOCKS:
                rows.append(_envelope("jczq_offer", url, rec, snap_ts, _offer_payload(sub, block)))
    return rows


def parse_jczq_result(url: str, rec: dict, snap_ts: str) -> list:
    j = json.loads(rec["body"].decode("utf-8", errors="replace"))
    if not _ok(j):
        return [_error_row("jczq_result", url, rec, snap_ts, rec["body"])]
    return [_envelope("jczq_result", url, rec, snap_ts, m)
            for m in (j.get("value") or {}).get("matchResult") or []]


def parse_jc_issue(url: str, rec: dict, snap_ts: str) -> list:
    j = json.loads(rec["body"].decode("utf-8", errors="replace"))
    if not _ok(j):
        return [_error_row("jc_issue", url, rec, snap_ts, rec["body"])]
    value = j.get("value") or {}
    dm = value.get("drawMatch") or {}
    payload = {
        "lotteryGameNum": dm.get("lotteryGameNum"), "lotteryGameName": dm.get("lotteryGameName"),
        "lotteryDrawNum": dm.get("lotteryDrawNum"),
        "lotterySaleBeginTime": dm.get("lotterySaleBeginTime"),
        "lotterySaleEndTime": dm.get("lotterySaleEndTime"),
        "lotteryDrawTime": dm.get("lotteryDrawTime"),
        "drawNumList": value.get("drawNumList") or [],
        "matchList": dm.get("matchList") or [],
    }
    return [_envelope("jc_issue", url, rec, snap_ts, payload)]


def parse_jc_issue_result(url: str, rec: dict, snap_ts: str) -> list:
    j = json.loads(rec["body"].decode("utf-8", errors="replace"))
    if not _ok(j):
        return [_error_row("jc_issue_result", url, rec, snap_ts, rec["body"])]
    value = j.get("value") or {}
    rows = []
    for key in _ISSUE_GAMES:
        detail = value.get(f"{key}Detail")
        if detail:
            payload = dict(detail)
            payload["gameKey"] = key
            rows.append(_envelope("jc_issue_result", url, rec, snap_ts, payload))
    return rows


def parse_jclq_offer(url: str, rec: dict, snap_ts: str) -> list:
    """§5.5：v1.1 不冻结。空（仅 vtoolsConfig）→ 无行（.empty）；首次非空 → 按 §5.1 结构产行 + 状态标记 + 回传探针包。"""
    j = json.loads(rec["body"].decode("utf-8", errors="replace"))
    if not _ok(j):
        return [_error_row("jclq_offer", url, rec, snap_ts, rec["body"])]
    value = j.get("value") or {}
    if not value.get("matchInfoList"):
        return []
    rows = []
    for mi in value.get("matchInfoList") or []:
        for sub in mi.get("subMatchList") or []:
            for block in _OFFER_BLOCKS:
                rows.append(_envelope("jclq_offer", url, rec, snap_ts, _offer_payload(sub, block)))
    st = _load_status()
    if st.get("jclq_offer") != "unverified-shape":
        _update_status(jclq_offer="unverified-shape")
        _pack_probe(rec, "jclq_offer_first_nonempty")
        print("[jclq_offer] 首次抓到非空：已标记 unverified-shape 并回传探针包")
    return rows


def parse_jclq_result(url: str, rec: dict, snap_ts: str) -> list:
    j = json.loads(rec["body"].decode("utf-8", errors="replace"))
    if not _ok(j):
        return [_error_row("jclq_result", url, rec, snap_ts, rec["body"])]
    return [_envelope("jclq_result", url, rec, snap_ts, m)
            for m in (j.get("value") or {}).get("matchResult") or []]


def parse_lottery_draw(url: str, rec: dict, snap_ts: str) -> list:
    j = json.loads(rec["body"].decode("utf-8", errors="replace"))
    if not _ok(j):
        return [_error_row("lottery_draw", url, rec, snap_ts, rec["body"])]
    return [_envelope("lottery_draw", url, rec, snap_ts, it)
            for it in (j.get("value") or {}).get("list") or []]

def parse_jc_odds_history(url: str, rec: dict, snap_ts: str) -> list:
    """jc_odds_history：一行 = 一场在售足球 × 整条赔率走势（getOddsHistoryV1 value 原样）。

    流程：candidates[0]（jczq_offer 端点）取在售场次 matchId → 逐场 GET history_url
    （QPS 节流走 fetch 内置）。payload = value dict 原样全搬（hadList/hhadList/crsList/
    ttgList/hafuList/singleList + 场次元键），无裁剪。单场失败 → 该场 1 条 error 行，不中断。
    """
    spec = CONFIG["topics"]["jc_odds_history"]
    j = json.loads(rec["body"].decode("utf-8", errors="replace"))
    if not _ok(j):
        return [_error_row("jc_odds_history", url, rec, snap_ts, rec["body"])]
    mids = []
    for mi in (j.get("value") or {}).get("matchInfoList") or []:
        for sub in mi.get("subMatchList") or []:
            if spec.get("selling_only") and sub.get("matchStatus") != "Selling":
                continue
            mid = sub.get("matchId")
            if mid:
                mids.append(mid)
    rows = []
    tpl = spec["history_url"]
    for mid in mids:
        hurl = tpl.format(matchId=mid)
        hrec = fetch(hurl)
        hj = json.loads(hrec["body"].decode("utf-8", errors="replace")) if hrec.get("body") else {}
        if not _ok(hj):
            rows.append(_error_row("jc_odds_history", hurl, hrec, snap_ts, hrec.get("body") or hrec.get("error", "").encode()))
            continue
        rows.append(_envelope("jc_odds_history", hurl, hrec, snap_ts, hj.get("value") or {}))
    return rows




_PARSERS = {
    "jczq_offer": parse_jczq_offer, "jczq_result": parse_jczq_result,
    "jc_issue": parse_jc_issue, "jc_issue_result": parse_jc_issue_result,
    "jclq_offer": parse_jclq_offer, "jclq_result": parse_jclq_result,
    "lottery_draw": parse_lottery_draw, "jc_odds_history": parse_jc_odds_history,
}


def _load_status() -> dict:
    p = ROOT / "collector.status.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (ValueError, OSError):
        return {}


def _update_status(**kw) -> None:
    st = _load_status()
    st.update(kw)
    st["contract_version"] = CONTRACT
    (ROOT / "collector.status.json").write_text(
        json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def _pack_probe(rec: dict, tag: str) -> None:
    """单独回传一份探针包（§5.5 首次非空 / 复探用）。"""
    out = ROOT / "probe"
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    name = f"{tag}_{fname_ts()}.json"
    (raw / name).write_bytes(rec["body"])
    summary = {"generated_at": now_utc(), "collector_host": HOST, "tag": tag,
               "results": [{"topic": "jclq_offer", "url": rec["url"],
                            "http_status": rec.get("http_status", 0),
                            "content_type": rec.get("content_type", ""),
                            "raw_file": f"raw/{name}"}]}
    (out / "probe_results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    pack = out / f"probe_{tag}_{fname_ts()}.tar.gz"
    with tarfile.open(pack, "w:gz") as tf:
        tf.add(out / "probe_results.json", arcname="probe_results.json")
        tf.add(raw / name, arcname=f"raw/{name}")
    # league key-only（受限 shell 只放行 `tar -C DIR -xzf -`）：本地 tar 打流 → 远端解到 incoming/cn-collector/
    p_local = subprocess.Popen(
        ["tar", "-czf", "-", "-C", str(pack.parent), str(pack.name)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p_ssh = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
         "-i", str(Path.home() / ".ssh" / "collector-league"),
         SSH_ALIAS, f"tar -C {REMOTE_ROOT}/{HOST} -xzf -"],
        stdin=p_local.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    p_local.stdout.close()
    _o, e = p_ssh.communicate(timeout=120)
    p_local.wait(timeout=60)
    if p_ssh.returncode != 0 or p_local.returncode != 0:
        print(f"[pack_probe] 探针包推送失败 tar_rc={p_local.returncode} ssh_rc={p_ssh.returncode}: {e.decode(errors='replace').strip()}")


def write_batch(topic: str, rows: list) -> Path:
    """落 out/<topic>/<utc>__<batch>.jsonl；0 行产 .empty。追加不覆盖（batch 序号递增）。

    ⚠️ **序号分配必须原子**（2026-10-23 生产实测修）：
    原来「`while (out_dir / f"{ts}__{batch:03d}.jsonl").exists(): batch += 1`」
    然后 `open(path, "w")` 是**两步**，两个进程会选到**同一个序号**、各自
    `open("w")`（截断）写同一个路径 ⇒ **文件内容互相交织、变成非法 JSON**。
    真实后果：2026-10-13/19/21 各有一批 `lottery_draw` / `jczq_offer` 坏在远端
    （远端装载报 `JSONDecodeError: Expecting ',' delimiter`，且该坏文件**留在盘上**，
    每次重装都会重复上报同一批错误）。根因链：`_acquire_singleton_lock` 的
    「exists() → write_text()」同样不原子 ⇒ 同一秒启动的多个批次**同时拿到锁**，
    同时采集、同时撞名。

    ⇒ 现在用 **O_EXCL 独占创建** 分配序号：抢不到就换下一个；抢到了就直接写。
    （`.jsonl` 与 `.empty` 共用同一套序号空间：同一序号下两种后缀都存在时，
    说明历史批已占用该序号 ⇒ 跳过。）
    """
    out_dir = ROOT / "out" / topic
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = fname_ts()
    suffix = ".jsonl" if rows else ".empty"
    for batch in range(1, 1000):
        base = f"{ts}__{batch:03d}"
        if (out_dir / f"{base}.jsonl").exists() or (out_dir / f"{base}.empty").exists():
            continue
        path = out_dir / f"{base}{suffix}"
        try:
            fh = open(path, "x", encoding="utf-8")   # O_EXCL：独占创建，原子
        except FileExistsError:
            continue
        except OSError:
            continue
        try:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        finally:
            fh.close()
        return path
    raise RuntimeError(f"write_batch: {ts} 的批次序号用尽（1..999）")


def _date_vars(base_date: str | None = None) -> dict[str, str]:
    """占位符取值表（**唯一真源**：config.json 的 URL 模板里用这些键）。

    ``base_date``（YYYY-MM-DD）用于**历史回补**：把「今天」挪到那一天，
    于是 ``--date 2026-09-17`` 就能按那天的窗口去查（见 --backfill-range）。

    ⚠️ 为什么要有 ``{today_minus_2}``：足球赛果 jczq_result 查的是
    **matchDate（开球日）**；跨日完赛（北京时间深夜开球、次日凌晨结束）的场次，
    赛果**次日**才发布，而次日查询按 matchDate 过滤就再也捞不到它
    ⇒ 天天只查当天 ⇒ 那类场次的赛果**永久丢失**（生产实测 457 场里丢 65 场）。
    ⇒ 必须**多查前几天**（每天查 D-3..D 共 4 个单日窗口），把「D 日的场次」在
    D+1 日再查一遍 ⇒ 那时它已经有最终赛果了。
    """
    if base_date:
        d0 = datetime.strptime(base_date, "%Y-%m-%d").replace(tzinfo=CN_TZ)
    else:
        d0 = datetime.now(CN_TZ)

    def off(n: int) -> str:
        return (d0 - timedelta(days=n)).strftime("%Y-%m-%d")

    return {
        "{today}": off(0),
        "{yesterday}": off(1),
        "{today_minus_2}": off(2),
        "{today_minus_3}": off(3),
        "{today_minus_7}": off(7),
    }


def _resolve(url_template: str, base_date: str | None = None) -> str:
    """把 URL 模板里的日期占位符替换成实际日期。"""
    for k, v in _date_vars(base_date).items():
        url_template = url_template.replace(k, v)
    return url_template


def _page_size_of(url: str) -> int:
    """从 URL 里取 pageSize（用于「疑似被分页截断」自检；取不到按 0 = 不判）。"""
    m = re.search(r"pageSize=(\d+)", url)
    return int(m.group(1)) if m else 0


def _response_ok(rec: dict) -> bool:
    """一次抓取是否「成功」（HTTP 200 且业务 errorCode=0 / success=true）。"""
    if rec.get("http_status", 0) != 200:
        return False
    try:
        return _ok(json.loads(rec["body"].decode("utf-8", errors="replace")))
    except (ValueError, KeyError, AttributeError):
        return False


def _fetch_with_page_fallback(url: str) -> tuple[dict, dict | None]:
    """抓一次；**pageSize 太大被接口拒绝时自动退回 30**（自愈，不静默）。

    为什么要这层：jczq_result 的 pageSize 由 30 提到 100（单日 30 场会顶到分页上限），
    但官方接口是否接受 pageSize=100 **未经验证** ⇒ 万一被拒，赛果 topic 直接瘫掉。
    ⇒ 这里在失败时用 pageSize=30 再试一次，并把降级写进 status + 打印告警。

    返回 (rec, fallback_info)；fallback_info 为 None 表示没用降级。
    """
    rec = fetch(url)
    if _response_ok(rec):
        return rec, None
    page_size = _page_size_of(url)
    if page_size <= 30:
        return rec, None                      # 本来就是 30 ⇒ 失败另有原因，照常产错误行
    fb_url = re.sub(r"pageSize=\d+", "pageSize=30", url)
    fb = fetch(fb_url)
    info = {"from": url, "to": fb_url, "http_status": rec.get("http_status", 0),
            "fallback_ok": _response_ok(fb)}
    print(f"[fallback] {url} 失败（http={rec.get('http_status', 0)}）⇒ 退回 pageSize=30：{fb_url}")
    return fb, info


def collect(topic: str, base_date: str | None = None) -> int:
    spec = CONFIG["topics"].get(topic)
    if not spec:
        print(f"[collect] 未知 topic: {topic}")
        return 1
    snap_ts = now_utc()
    rows = []
    truncated = []
    fallbacks = []
    for tpl in spec["candidates"]:
        url = _resolve(tpl, base_date)
        rec, fb = _fetch_with_page_fallback(url)
        if fb:
            fallbacks.append(fb)
            url = fb["to"]
        got = _PARSERS[topic](url, rec, snap_ts)
        # 分页自检：单页返回条数 == pageSize ⇒ **可能被截断**（不静默：写进 status + 告警）
        page_size = _page_size_of(url)
        if page_size and len(got) >= page_size:
            truncated.append({"url": url, "rows": len(got), "page_size": page_size})
            print(f"[collect] ⚠️ {url} 返回 {len(got)} 行（= pageSize {page_size}）"
                  f"⇒ 可能被分页截断，请改大 pageSize 或加 pageNo")
        rows.extend(got)
    path = write_batch(topic, rows)
    _update_status(last_collect=topic, last_collect_at=now_utc())
    if truncated:
        _update_status(page_limit_warnings=truncated)
    if fallbacks:
        _update_status(page_size_fallbacks=fallbacks)
    print(f"[collect] {topic}: {len(rows)} 行 → {path}")
    return 0


def collect_all() -> int:
    rc = 0
    for topic in CONFIG["topics"]:
        rc |= collect(topic)
    return rc

def collect_batch(topics: list) -> tuple[int, list]:
    """§6 B1：一批 = 全部 topic 齐全。每个指定 topic 各落一个文件（.jsonl 或 .empty），缺一个都不行。

    返回 (rc, 本批实际产出的文件路径列表)。
    """
    rc = 0
    paths = []
    for topic in topics:
        paths.append(collect_topic(topic))
    return rc, paths


def collect_topic(topic: str, base_date: str | None = None) -> Path:
    """单 topic 采集 + 落文件，返回产出路径（.jsonl 或 .empty）。"""
    spec = CONFIG["topics"].get(topic)
    if not spec:
        print(f"[collect] 未知 topic: {topic}")
        raise ValueError(topic)
    snap_ts = now_utc()
    rows = []
    truncated = []
    for tpl in spec["candidates"]:
        url = _resolve(tpl, base_date)
        rec = fetch(url)
        got = _PARSERS[topic](url, rec, snap_ts)
        page_size = _page_size_of(url)
        if page_size and len(got) >= page_size:
            truncated.append({"url": url, "rows": len(got), "page_size": page_size})
            print(f"[collect_topic] ⚠️ {url} 返回 {len(got)} 行（= pageSize {page_size}）"
                  f"⇒ 可能被分页截断")
        rows.extend(got)
    path = write_batch(topic, rows)
    _update_status(last_collect=topic, last_collect_at=now_utc())
    if truncated:
        _update_status(page_limit_warnings=truncated)
    print(f"[collect] {topic}: {len(rows)} 行 → {path}")
    return path


def probe() -> int:
    """探针模式（v1 遗留）：逐个 GET 候选端点；反爬 HTML 换 UA/Referer 重试 2 次；BLOCKED 标记。"""
    out_dir = ROOT / "probe"
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    results = []
    alt_uas = [
        UA,
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    ]
    alt_refs = [REFERER, "https://www.sporttery.cn/", "https://static.sporttery.cn/"]
    idx = 0
    for topic, spec in CONFIG["topics"].items():
        for tpl in spec["candidates"]:
            idx += 1
            url = _resolve(tpl)
            rec = fetch(url)
            if rec.get("http_status") and "html" in rec.get("content_type", "").lower():
                for k in (1, 2):
                    time.sleep(QPS)
                    try:
                        s, c, b = _req(url, alt_uas[k % len(alt_uas)], alt_refs[k % len(alt_refs)])
                        rec = {"url": url, "fetched_at": now_utc(), "http_status": s,
                               "content_type": c, "body": b, "attempts": 1, "retried_ua": k}
                        if "html" not in c.lower():
                            break
                    except (urllib.error.URLError, TimeoutError, OSError) as e:
                        rec["error"] = f"{type(e).__name__}: {e}"
            body = rec.get("body", b"")
            head = body[:HEAD_BYTES].decode("utf-8", errors="replace")
            blocked = not rec.get("http_status") or "html" in rec.get("content_type", "").lower()
            ext = ".html" if "html" in rec.get("content_type", "").lower() else ".json"
            raw_name = f"{topic}__{idx:02d}{ext}"
            (raw_dir / raw_name).write_bytes(body)
            results.append({
                "topic": topic, "url": url, "http_status": rec.get("http_status", 0),
                "content_type": rec.get("content_type", ""), "blocked": blocked,
                "head": head, "raw_file": f"raw/{raw_name}",
                "fetched_at": rec.get("fetched_at", ""), "error": rec.get("error", ""),
            })
            print(f"[probe] {topic:14} {rec.get('http_status', 0):>3} "
                  f"{'BLOCKED' if blocked else 'OK':7} {url[:90]}")
    summary = {"generated_at": now_utc(), "collector_host": HOST, "results": results}
    (out_dir / "probe_results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    pack = out_dir / "probe_pack.tar.gz"
    with tarfile.open(pack, "w:gz") as tf:
        tf.add(out_dir / "probe_results.json", arcname="probe_results.json")
def _manifest(paths: list) -> str:
    """§G(A)：.done 清单。每行 <相对路径>\t<行数>\t<逐行 src_hash 级联>。

    只列本批实际产出的文件（paths，来自 collect_batch）；tar 与清单一一对应，
    无孤儿、无 double-list。.empty → 行数 0、级联列空。
    """
    lines = []
    for p in paths:
        rel = f"{p.parent.name}/{p.name}"
        if p.suffix == ".empty":
            lines.append(f"{rel}\t0\t")
        else:
            h = hashlib.sha256()
            n = 0
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    n += 1
                    h.update(line.encode("utf-8"))
            lines.append(f"{rel}\t{n}\t{h.hexdigest()}")
    return "\n".join(lines) + "\n"


def _stream_local_tar(local_args: list, timeout: int, label: str = "push") -> int:
    """本地 tar（stdout 打流）→ ssh(league key-only) 远端 tar 解到 remote_base。
    返回 ssh rc；本地 tar 失败抛异常（数据没打包成功，不可推）。"""
    remote_base = f"{REMOTE_ROOT}/{HOST}"
    p_local = subprocess.Popen(
        local_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p_ssh = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
         "-i", str(Path.home() / ".ssh" / "collector-league"),
         SSH_ALIAS, f"tar -C {remote_base} -xzf -"],
        stdin=p_local.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    p_local.stdout.close()
    _out, err = p_ssh.communicate(timeout=timeout)
    p_local.wait(timeout=timeout)
    if p_local.returncode != 0:
        p_local_err = p_local.stderr.read(errors="replace")
        p_ssh.kill()
        print(f"[{label}] 本地 tar 失败 rc={p_local.returncode}: {p_local_err.strip()}")
        return 1
    if p_ssh.returncode != 0:
        print(f"[{label}] ssh 流式失败 rc={p_ssh.returncode}: {err.decode(errors='replace').strip()}")
    return p_ssh.returncode


def _push_paths(paths: list, label: str = "push_batch") -> int:
    """把 out/ 下这批文件按 B2 契约推远端（① 数据先落位 ② .done 清单最后到）。

    与 push_batch 共用同一套推法 —— 回补批（backfill_range）也走这里，
    免得「回补用一套推法、日常用另一套」两处漂移。
    """
    out_dir = ROOT / "out"
    batch_ts = now_utc().rstrip("Z").replace(":", "-")
    done = f"{batch_ts}Z.done"
    manifest = _manifest(paths)
    # .done 以最终时间戳名直接落本地 out/（受限 shell 远端只有 tar -xzf，无法改名；本地保留作幂等锚）
    (out_dir / done).write_text(manifest, encoding="utf-8")
    # ① 数据文件：-C out 使 arcname = topic/<file>.jsonl，与远端 .done 相对路径一致
    rel = [str(p.relative_to(out_dir)) for p in paths]
    r1 = _stream_local_tar(["tar", "-czf", "-", "-C", str(out_dir), *rel], timeout=180, label=label)
    if r1 != 0:
        return 1
    # ② .done 清单（B2 最后一步）：单独推，远端名 = done；数据文件流里不含 .done
    r2 = _stream_local_tar(["tar", "-czf", "-", "-C", str(out_dir), done], timeout=60, label=label)
    if r2 != 0:
        return 1
    print(f"[{label}] 完成：{len(paths)} 个文件 + {done}")
    return 0


def _dates_between(start: str, end: str) -> list[str]:
    """[start, end] 闭区间的日期列表（YYYY-MM-DD）。"""
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    if d1 < d0:
        raise ValueError(f"end({end}) 早于 start({start})")
    out, cur = [], d0
    while cur <= d1:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def backfill_range(start: str, end: str, topic: str = "jczq_result",
                   pages: int = 3, push: bool = True) -> int:
    """**历史回补**：按日期区间逐日重抓（每日期翻 pageNo=1..pages 页）并推远端。

    为什么需要（2026-10-22 生产实测）：
      ① 日常窗口改成 D-3..D 之后，只能修好**从改动生效之后**的场次；
      ② 已经丢掉的那些场次（生产 457 场里 65 场，14%）只能**按历史日期重抓**补回来；
      ③ 官方接口按日期过滤 ⇒ 必须**逐日**查（不能一次查一个月：单页会被总场次顶掉）。

    翻页：单日场次可能超过 pageSize ⇒ 每日期抓 pageNo=1..pages，按 matchId 去重。
    产物：out/<topic>/<ts>__001.jsonl（契约同日常批）⇒ 远端装载器照常装载。
    """
    spec = CONFIG["topics"].get(topic)
    if not spec or not spec.get("candidates"):
        print(f"[backfill] 未知 topic: {topic}")
        return 1
    tpl0 = spec["candidates"][0]
    snap_ts = now_utc()
    rows: list = []
    seen_ids: set = set()
    n_pages = 0
    for day in _dates_between(start, end):
        for page in range(1, max(1, pages) + 1):
            # 该日期的所有日期占位符统一替换成 day ⇒ matchBeginDate=matchEndDate=day
            url = tpl0
            for k, _v in _date_vars(day).items():
                url = url.replace(k, day)
            url = re.sub(r"pageNo=\d+", f"pageNo={page}", url)
            rec, fb = _fetch_with_page_fallback(url)
            if fb:
                url = fb["to"]
            got = _PARSERS[topic](url, rec, snap_ts)
            n_pages += 1
            fresh = 0
            for row in got:
                payload = row.get("payload") or {}
                mid = payload.get("matchId")
                if mid is not None:
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                fresh += 1
                rows.append(row)
            print(f"[backfill] {day} page{page}: {len(got)} 行（新增 {fresh}）")
    path = write_batch(topic, rows)
    print(f"[backfill] {start}~{end}：{n_pages} 次请求，去重后 {len(rows)} 行 → {path}")
    _update_status(last_backfill=f"{topic} {start}~{end}", last_backfill_at=now_utc(),
                    last_backfill_rows=len(rows))
    if not push:
        return 0
    return _push_paths([path], "backfill_range")


def push_batch(topics: list) -> int:
    """B1+B2+G(A)：采集全部指定 topic → tar 流式推远端 → 最后写 .done 清单。

    league key-only（受限 shell 只放行 `tar -C DIR -xzf -`）：
      ① 本地 Windows tar -czf - 只含本批 paths（以 out/ 为基准，arcname=topic/<file>）
         → 管道 → ssh oracle-league `tar -C <incoming>/cn-collector -xzf -`，直接解到最终
         位置（incoming/cn-collector/<topic>/），不经过 staging/mv；
      ② .done 清单同样 tar 流式推（解到 cn-collector/ 根）；
      ③ B2：.done 是最后一步——数据先落位，清单后到；海外侧以 .done 为装载信号。
    原子性：tar 单文件原子解包；半程断流 → 部分文件已落位但该批无 .done，海外侧
    （mismatch/无主清单判定）会丢弃未成套文件，幂等可重推（batch 序号 __NNN 递增不覆盖）。
    """
    rc, paths = collect_batch(topics)
    out_dir = ROOT / "out"
    if not out_dir.exists():
        print("[push_batch] out/ 不存在")
        return 1
    if rc != 0:
        print(f"[push_batch] collect rc={rc}（部分 topic 失败，仍推已产出文件）")
    pushed = _push_paths(paths, "push_batch")
    return rc if pushed == 0 else pushed



def _pid_alive(pid: int) -> bool:
    """进程 pid 是否还活着（**跨平台**）。

    ⚠️ 为什么不能只写 Windows 版：采集端跑在**国内 Windows 工作机**，但
    `collector-cn/collector.py` 同时是**仓库里的生产代码**，其回归测试会在
    **Linux（Oracle）** 上跟着全量 pytest 一起跑。若存活判定只认
    `ctypes.windll.kernel32`，Linux 上会在这一行抛 `AttributeError` ⇒
    **Linux 上跑不动采集端的锁测试**（2026-10-23 实测踩到：Oracle 上 2 failed）。

    - **Windows**：走 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`。
      ⚠️ **不能用 `os.kill(pid, 0)`**：Windows 上 `os.kill` 对非 CTRL_* 信号
      会调用 `TerminateProcess` ⇒ **会把目标进程杀掉**（signal 0 也不例外）。
    - **POSIX**：`os.kill(pid, 0)` 只做存在性+权限探测，不发真信号。
      `ProcessLookupError` ⇒ 已死；`PermissionError` ⇒ 存在但无权限 ⇒ 活着。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        K32 = ctypes.windll.kernel32
        h = K32.OpenProcess(0x0400, 0, pid)
        if h:
            K32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 存在但不允许发信号 ⇒ 进程还在
    except OSError:
        return False
    return True


def _acquire_singleton_lock() -> bool:
    """单例锁：同机同一时间只允许一个 collector 批次在跑。
    任务 IgnoreNew 已挡调度层重叠；此锁兜底 wscript 被 PT30M 杀后 python 僵尸还活着、
    下一轮新 wscript 又拉一个 python 的堆积场景（历史死机前 1GB+ 根因）。
    锁文件 .collector.lock 写本进程 pid；旧 pid 已死 → 接管；还活着 → 返回 False。

    ⚠️ **必须原子拿锁**（2026-10-23 生产实测修）：原来「`if lock.exists()` →
    `lock.write_text()`」是**两步**，同一秒启动的 N 个批次会**同时判定"没有锁"**、
    各自写自己的 pid ⇒ N 个批次**同时采集**。真实后果：Boot+Logon 触发器把多个
    计划任务在同一刻拉起（实测 21:49 一次拉起 4 个），其中三批在**同一分钟内**跑 ⇒
    它们给同一 topic 选了**同一个文件名**（序号分配同样不原子，见 `write_batch`）
    ⇒ 两个进程 `open("w")` 写同一路径 ⇒ **文件内容互相交织、变成非法 JSON**，
    远端装载报 JSONDecodeError 且坏文件留在盘上反复上报。

    ⇒ 现在用 **O_EXCL 独占创建**拿锁：只有能独占创建成功的那个进程算拿到锁；
    锁已存在时再走「旧 pid 是否还活着」的判定（死 pid → 接管）。
    """
    lock = ROOT / ".collector.lock"
    my_pid = os.getpid()
    try:
        fh = open(lock, "x", encoding="utf-8")   # O_EXCL：原子拿锁
    except FileExistsError:
        pass
    except OSError:
        return False
    else:
        try:
            fh.write(str(my_pid))
        finally:
            fh.close()
        return True
    # 锁已存在 ⇒ 旧 pid 是否还活着？
    try:
        old_pid = int(lock.read_text(encoding="utf-8").strip() or "0")
    except (ValueError, OSError):
        old_pid = 0
    if old_pid and old_pid != my_pid and _pid_alive(old_pid):
        return False
    # 旧 pid 已死 ⇒ 接管（罕见路径；真正的护栏是 write_batch 的原子独占创建）
    try:
        lock.write_text(str(my_pid), encoding="utf-8")
    except OSError:
        return False
    return True


def _release_singleton_lock() -> None:
    lock = ROOT / ".collector.lock"
    try:
        if lock.exists() and lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock.unlink()
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="国内采集机（契约 v1.2）")
    ap.add_argument("--probe", action="store_true", help="探针模式（复探用）")
    ap.add_argument("--collect", metavar="TOPIC", help="采集指定 topic 落 JSONL")
    ap.add_argument("--collect-all", action="store_true", help="采集全部 8 topic")
    ap.add_argument("--push-batch", nargs="+", metavar="TOPIC",
                    help="B1+B2+G(A)：采集指定 topic 批 + 原子推远端 + .done 清单")
    ap.add_argument("--backfill-range", nargs=2, metavar=("START", "END"),
                    help="历史回补：逐日重抓该日期区间（含翻页）并推远端（YYYY-MM-DD）")
    ap.add_argument("--backfill-topic", default="jczq_result", help="回补的 topic（缺省 jczq_result）")
    ap.add_argument("--backfill-pages", type=int, default=3, help="每个日期抓几页（缺省 3）")
    ap.add_argument("--no-push", action="store_true", help="回补只落盘不推远端")
    ap.add_argument("--mode-log", metavar="MODE", default="",
                    help="静默模式：stdout/stderr 同时追加写 logs/collector_<MODE>.log（计划任务无窗口时诊断用）")
    args = ap.parse_args()
    # 静默日志：stdout/stderr 同时追加写 logs/collector_<mode>.log（计划任务无窗口时诊断全在这）
    if args.mode_log:
        _lh = ROOT / "logs" / f"collector_{args.mode_log}.log"
        _lh.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = open(_lh, "a", encoding="utf-8")
        sys.stderr = open(_lh, "a", encoding="utf-8")
    # 单例锁：--push-batch 定时任务主路径专用；probe/collect 不锁（可并行诊断）
    _locked = False
    if args.push_batch:
        if not _acquire_singleton_lock():
            print("[singleton] 已有活跃批次在跑，本次 no-op")
            return 0
        _locked = True
    try:
        if args.probe:
            return probe()
        if args.collect:
            return collect(args.collect)
        if args.collect_all:
            return collect_all()
        if args.push_batch:
            return push_batch(args.push_batch)
        if args.backfill_range:
            return backfill_range(args.backfill_range[0], args.backfill_range[1],
                                  topic=args.backfill_topic, pages=args.backfill_pages,
                                  push=not args.no_push)
        ap.print_help()
        return 1
    finally:
        if _locked:
            _release_singleton_lock()


if __name__ == "__main__":
    sys.exit(main())
