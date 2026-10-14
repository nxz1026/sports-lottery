"""web.services.store — 引擎产物唯一读取层。

只读 `predictions/`、`results/`、`references/` 下的 JSON；绝不触发引擎子进程。
数据语义以 docs/web/DATA_CONTRACT.md §2/§3/§4 为准：
- 预测文件名时间戳粒度到小时且为 BJT（§3.1），同名同小时互相覆盖；
- 「每联赛最新一次运行」按 §3.3：全量扫描后依 `generated_at` 取最大（无法解析时退化为 mtime）；
- 坏 JSON / 缺目录 / 字段缺失一律跳过并 log warning，永不抛穿。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BJT = ZoneInfo("Asia/Shanghai")
# 引擎 FOOTBALL_DIR 语义：LP_OUTPUT_DIR 覆盖，默认 scripts/（契约 §9-5）。
BASE_DIR = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = Path(os.environ.get("LP_OUTPUT_DIR", str(BASE_DIR / "scripts")))

logger = logging.getLogger("web.store")


def _json_load(path: Path) -> dict | None:
    """宽容解析 JSON（utf-8/gbk 回退）；坏文件 → None + warning，永不抛穿。"""
    raw: bytes | None = None
    try:
        raw = path.read_bytes()
    except OSError:
        logger.warning("跳过不可读文件: %s", path)
        return None
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            logger.warning("跳过坏 JSON（JSON 格式错误，%s 编码）: %s", enc, path)
            return None
        if isinstance(data, dict) and data:
            return data
        logger.warning("跳过非对象/空 JSON: %s", path)
        return None
    logger.warning("跳过编码无法识别的文件: %s", path)
    return None


def _parse_dt(value) -> datetime | None:
    """解析 ISO8601 时间戳（兼容尾部 Z）；失败返回 None。"""
    if not value or not isinstance(value, str):
        return None
    try:
        norm = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(norm)
    except ValueError:
        return None


def iter_prediction_files() -> list[Path]:
    d = OUTPUT_DIR / "predictions"
    if not d.is_dir():
        logger.warning("预测目录缺失: %s", d)
        return []
    return sorted(d.glob("prediction_*.json"))


def load_prediction_docs() -> list[dict]:
    """全部可解析预测文件，附解析元数据；坏文件跳过。

    返回 doc 含 ``path`` / ``name`` / ``generated_at``（ISO 字符串 + datetime）
    / ``data``（原始 JSON）/ ``mtime``（文件 stat mtime，float epoch 秒）。
    ``mtime`` 是 web 侧 ``league_accuracy_web`` 的窗口过滤（cutoff）所必须；
    早期版本漏写 ``mtime`` 字段，导致 ``_collect_league_data`` 拿 ``doc.get("mtime", 0)``
    永远为 0，cutoff 过滤把所有文件跳过，命中率永远"数据积累中"。

    P2-7 性能：模块级 LRU 缓存 + mtime 失效。
    触发：每个端点（predictions/today、accuracy、combo/eval 等）调用此函数
    会全量 glob + json.load 100+ 文件。LRU 后单 worker 内 5s 内重复请求
    直接命中缓存；引擎写入新文件 → mtime 变化 → 自动失效。

    缓存按 OUTPUT_DIR 分别持有条目（同一进程内不同路径不会串数据）。

    R17 #8（cache invalidation 边界）：mtime-based invalidation 假设文件系统
    时间戳精度足以分辨单次重写（NTFS / ext4 / APFS 都是 100ns 级）——
    网络盘 / FAT32 / 某些虚拟化 FS 上 mtime 精度可能只有 2s，**理论存在
    "内容变化但 mtime 不变"的极端情形**（如 sub-second 多次重写）。当前数据流
    下不可触发；若未来接入网络盘 / 低精度 FS，应额外把 file_size 进 sig：
      sig_id = (len(file_sigs),) + tuple(
          (name, mtime, size) for name, mtime in file_sigs
      )
    """
    files = iter_prediction_files()
    cache_key = str(OUTPUT_DIR)
    try:
        file_sigs = tuple((f.name, f.stat().st_mtime) for f in files)
    except OSError as exc:
        logger.warning("stat predictions 失败: %s", exc)
        _load_prediction_docs_cached.pop(cache_key, None)
        return []
    sig_id = (len(file_sigs),) + tuple(s for _, s in file_sigs)
    cached = _load_prediction_docs_cached.get(cache_key)
    if cached is not None and cached[0] == sig_id:
        return cached[1]
    docs: list[dict] = []
    for f in files:
        data = _json_load(f)
        if data is None:
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        docs.append({
            "path": f,
            "name": f.name,
            "generated_at_iso": data.get("generated_at"),
            "generated_at": _parse_dt(data.get("generated_at")),
            "data": data,
            "mtime": mtime,
        })
    _load_prediction_docs_cached[cache_key] = (sig_id, docs)
    return docs


_load_prediction_docs_cached: dict = {}
_pred_index_by_pair_cached: dict = {}


def pred_index_by_pair() -> dict[tuple[str, str], dict]:
    """全部可解析预测文档的 ``(home, away) -> entry`` 反查索引（O(1) 命中）。

    用途：web/routers/haf + combo 路由把原本线性 O(N×M) 的 ``teams_match``
    模糊匹配降到字典命中 + 兜底 fuzzy（miss 才走）。

    实现要点：
      * 与 ``load_prediction_docs`` **共享 mtime 签名缓存**（``_load_prediction_docs_cached``
        由 ``load_prediction_docs`` 内 LRU 维护，先调它拿 docs；自己再额外独立 sig
        cache 让 dict 重建可独立失效）。
      * key 标准化为 ``(home.strip(), away.strip())`` —— 与现有 fuzzy 兜底一致。
      * first-match-wins：同一对阵若多文档都出现，保留**最早**（按文件名升序的
        sorted output）—— 对齐 ``haf._find_prediction`` 现行选择语义。
      * 坏 / 缺 home/away 的 entry 跳过（不污染索引）。

    缓存按 ``OUTPUT_DIR`` 分键（与 ``load_prediction_docs`` 对齐）；同一进程不同
    ``LP_OUTPUT_DIR`` 不串数据。测试可用 ``_pred_index_by_pair_cached.clear()``
    强制失效。
    """
    files = iter_prediction_files()
    cache_key = str(OUTPUT_DIR)
    try:
        file_sigs = tuple((f.name, f.stat().st_mtime) for f in files)
    except OSError as exc:
        logger.warning("stat predictions 失败: %s", exc)
        _pred_index_by_pair_cached.pop(cache_key, None)
        return {}
    sig_id = (len(file_sigs),) + tuple(s for _, s in file_sigs)
    cached = _pred_index_by_pair_cached.get(cache_key)
    if cached is not None and cached[0] == sig_id:
        return cached[1]
    docs = load_prediction_docs()
    idx: dict[tuple[str, str], dict] = {}
    for doc in docs:
        preds = (doc.get("data") or {}).get("predictions") or []
        for e in preds:
            if not isinstance(e, dict):
                continue
            h = (e.get("home") or "").strip()
            a = (e.get("away") or "").strip()
            if not h or not a:
                continue
            # first-match-wins：同一对阵只有在**尚未入索引**时才覆盖，与 haf
            # 现行循环语义一致；现实数据每对阵只出现在一份最新 doc 里。
            idx.setdefault((h, a), e)
    _pred_index_by_pair_cached[cache_key] = (sig_id, idx)
    return idx


def _file_dt(path: Path) -> datetime | None:
    """文件名时间戳 `prediction_YYYY-MM-DD_HH.json`（BJT 小时粒度，契约 §3.1）。"""
    m = re.fullmatch(r"prediction_(\d{4}-\d{2}-\d{2})_(\d{2})\.json", path.name)
    if not m:
        return None
    try:
        return datetime.strptime(
            f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H").replace(tzinfo=BJT)
    except ValueError:
        return None


def _as_aware(dt: datetime | None) -> datetime | None:
    """naive 时间戳按 BJT 补时区，统一为 aware 后比较/转时间戳。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=BJT)


def _newer(a: dict, b: dict) -> bool:
    # 文件名时间戳是 BJT 运行时刻权威（§3.1）；同小时才回退 generated_at/mtime。
    fa, fb = _file_dt(a["path"]), _file_dt(b["path"])
    if fa and fb and fa != fb:
        return fa > fb
    ta, tb = _as_aware(a.get("generated_at")), _as_aware(b.get("generated_at"))
    if ta and tb and ta != tb:
        return ta > tb
    # 时间戳缺失/相同 → mtime 兜底（同小时覆盖后磁盘只剩一份，天然最新）。
    return a["path"].stat().st_mtime > b["path"].stat().st_mtime


def latest_by_league(leagues: list[str] | None = None) -> dict[str, dict]:
    """每联赛最新一次运行（契约 §3.3：按 generated_at 最大，后出者胜）。"""
    latest: dict[str, dict] = {}
    for doc in load_prediction_docs():
        league = doc["data"].get("league")
        if not isinstance(league, str) or not league:
            continue
        if leagues is not None and league not in leagues:
            continue
        if league not in latest or _newer(doc, latest[league]):
            latest[league] = doc
    return latest


def history_by_league(leagues: list[str] | None = None) -> dict[str, list]:
    """每联赛历史预测文件（按 generated_at 降序，等价→mtime 降序）。"""
    groups: dict[str, list] = {}
    for doc in load_prediction_docs():
        league = doc["data"].get("league")
        if not isinstance(league, str) or not league:
            continue
        if leagues is not None and league not in leagues:
            continue
        groups.setdefault(league, []).append(doc)
    for docs in groups.values():
        docs.sort(
            key=lambda d: _as_aware(d.get("generated_at")) or datetime.min.replace(tzinfo=BJT),
            reverse=True,
        )
    return groups


def bjt_today() -> date:
    """「今日」以 BJT 比赛日口径（契约 §5 PLAN，BJT=Asia/Shanghai）。"""
    return datetime.now(BJT).date()


def _window_dates(data: dict) -> tuple[date, date] | None:
    """解析紧凑窗口 `YYYYMMDD-YYYYMMDD`（契约 §2.1）。"""
    w = data.get("data_window")
    if not isinstance(w, str) or "-" not in w:
        return None
    start_s, end_s = w.split("-", 1)
    if len(start_s) != 8 or len(end_s) != 8:
        return None
    try:
        return (
            date(int(start_s[:4]), int(start_s[4:6]), int(start_s[6:8])),
            date(int(end_s[:4]), int(end_s[4:6]), int(end_s[6:8])),
        )
    except ValueError:
        return None


def covers_date(data: dict, day: date) -> bool:
    win = _window_dates(data)
    return bool(win and win[0] <= day <= win[1])


def results_for_date(day_str: str) -> list[dict]:
    """读 results/result_{date}.json（文件名 UTC 口径，契约 §4.1）。"""
    data = _json_load(OUTPUT_DIR / "results" / f"result_{day_str}.json")
    if data is None:
        return []
    matches = data.get("matches")
    return matches if isinstance(matches, list) else []


def results_by_bjt_date(day: date) -> list[dict]:
    """BJT 比赛日赛果：BJT 凌晨 0-8 点属前一日 UTC，合并两文件按 id 去重。"""
    seen: set[str] = set()
    out: list[dict] = []
    for d in (day, day - timedelta(days=1)):
        for m in results_for_date(d.isoformat()):
            # 无 id 的行也要参与去重：用整条记录的稳定指纹兜底，避免同一场
            # 比赛在两天文件里都缺 id 时被重复导出（真实数据 id 恒非空，此为边界保护）。
            key = m.get("id") or json.dumps(m, sort_keys=True, ensure_ascii=False, default=str)
            if key not in seen:
                seen.add(key)
                out.append(m)
    return out


def calibration_states() -> dict[str, dict]:
    """读 references/.calibration_state_{league}.json（契约 §2.4），坏文件跳过。"""
    refs = OUTPUT_DIR / "references"
    states: dict[str, dict] = {}
    if not refs.is_dir():
        logger.warning("校准状态目录缺失: %s", refs)
        return states
    for f in sorted(refs.glob(".calibration_state_*.json")):
        league = f.name[len(".calibration_state_"):-len(".json")]
        data = _json_load(f)
        if data is not None:
            states[league] = data
    return states


def _stable_match_key(rec: dict) -> str:
    """与 scripts/core/backtest._bk_stable_key 同语义的只读副本：
    英文原名优先（home_en|away_en），回退 name/match 中文名。

    web 侧不 import scripts（契约 §1：只读引擎产物 JSON）；连接键语义必须
    与引擎侧一致，否则「待结算」计数与未来 accuracy 出数口径会漂移。
    引擎侧改动本键规则时须同步这里（见 tests/test_p0_window_mc_stablekey.py
    修复 B 注释）。
    """
    h = str(rec.get("home_en") or "").strip()
    a = str(rec.get("away_en") or "").strip()
    if h and a:
        return f"{h}|{a}"
    return str(rec.get("name") or rec.get("match") or "").strip()


def pending_predictions_by_league() -> dict[str, int]:
    """每联赛「已预测但尚未完赛」的预测场次（稳定键去重后）。

    结算口径：某预测键（home_en|away_en 或 中文名）出现在任一历史文件
    past_matches 中带有效比分（"x-y"）的记录里，即视为已结算。
    用于页面空态提示（"N 场待结算，首场比赛结束后自动出数"），替代
    accuracy 恒空时无信息量的 "—"。
    """
    settled: set[str] = set()
    preds: dict[str, set[str]] = {}
    for doc in load_prediction_docs():
        league = doc["data"].get("league")
        if not isinstance(league, str) or not league:
            continue
        bucket = preds.setdefault(league, set())
        for m in doc["data"].get("past_matches") or []:
            if not isinstance(m, dict):
                continue
            key = _stable_match_key(m)
            score = str(m.get("score") or "")
            if key and "-" in score:
                try:
                    h, a = score.split("-")[:2]
                    int(h)
                    int(a)
                except ValueError:
                    continue
                settled.add(key)
        for p in doc["data"].get("predictions") or []:
            if not isinstance(p, dict):
                continue
            key = _stable_match_key(p)
            if key:
                bucket.add(key)
    return {lg: len(keys - settled) for lg, keys in preds.items()}