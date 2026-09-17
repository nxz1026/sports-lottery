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
    """全部可解析预测文件，附解析元数据；坏文件跳过。"""
    docs: list[dict] = []
    for f in iter_prediction_files():
        data = _json_load(f)
        if data is None:
            continue
        docs.append({
            "path": f,
            "name": f.name,
            "generated_at_iso": data.get("generated_at"),
            "generated_at": _parse_dt(data.get("generated_at")),
            "data": data,
        })
    return docs


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
            key = m.get("id", "")
            if not key or key not in seen:
                if key:
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