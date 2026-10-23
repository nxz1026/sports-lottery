"""P0-COLLECT2o 自 jc_load 拆出：批↔文件归属与读行工具；.done 清单优先（行数对账），0 字节 marker 走时间戳窗口兜底。"""
import json
import re
from datetime import datetime
from pathlib import Path

from core.log import logger

from ingest.jc_manifest import _check_entry, _manifest, _resolve

OPTIONAL = ("jc_odds_history",)


def _stamp(name: str) -> datetime:
    m = re.search(r"(\d{4})-(\d\d)-(\d\d)T(\d\d)-(\d\d)(?:-(\d\d))?", name)
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5)), int(m.group(6) or "0"))


def iter_markers(root: Path, only: str | None = None) -> list[Path]:
    ms = sorted(root.glob("*.done"))
    key = only + ".done" if only and not only.endswith(".done") else only
    return [m for m in ms if m.name == key] if key else ms


def _from_manifest(root: Path, man: dict[str, list[tuple[str, int, str]]],
                   topics: tuple[str, ...]) -> dict[str, list[tuple[Path, str]]]:
    out: dict[str, list[tuple[Path, str]]] = {}
    resolved: dict[str, list[tuple[str, int, str]]] = {}
    for rel, decl, dhash in man.get("", []):
        if hit := _resolve(root, topics, rel, decl, dhash):
            resolved.setdefault(hit[0], []).append((f"{hit[0]}/{rel}", decl, dhash))
    for topic in topics:
        entries = man.get(topic, []) + resolved.get(topic, [])
        if not entries:
            # ⚠️ **2026-10-23 订正（原口径错 ⇒ gaps 虚高）**：
            # 原来「清单里没有该 topic」一律造一个 ``(None, "missing")`` ⇒
            # 该 topic 被记一条 ``<marker>#MISSING`` 的 file_arrival ⇒ **gaps +1**。
            # 但「**故意少推的残缺批次**」（回补批 / 单 topic 推送 / 手动验证批）
            # 的清单**本来就没有**其它 topic ⇒ 于是**每次都凭空造缺档**。
            # 实测：gaps 43 → 49（+6 全来自本轮单 topic 推送的残缺批）。
            # ⇒ 现在区分两种「没有」：
            #   ① **清单里压根没这个 topic** ⇒ state="absent" ⇒ **不是缺档**
            #      （这批设计上就不含它）；OPTIONAL topic 同样按 absent 处理；
            #   ② **清单里列了、但文件读不出来/行数对不上** ⇒ 由 ``_check_entry``
            #      返回 state="rejected" ⇒ 这才是**真缺档**（`_load_file` 会失败，
            #      批次标 not_ok ⇒ 下轮重试）。
            out[topic] = [(None, "absent")]
            continue
        out[topic] = [_check_entry(topic, root / rel, decl) for rel, decl, _h in entries]
    return out


def _window(root: Path, marker: Path,
            topics: tuple[str, ...]) -> dict[str, list[tuple[Path, str]]]:
    ms = iter_markers(root)
    i = ms.index(marker)
    lo, hi = (_stamp(ms[i - 1].name) if i else datetime.min), _stamp(marker.name)
    out = {}
    for topic in topics:
        cands = [p for pat in ("*.jsonl", "*.empty")
                 for p in (root / topic).glob(pat) if lo < _stamp(p.name) <= hi]
        cands.sort(key=lambda p: p.name)
        out[topic] = [(p, p.suffix.lstrip(".")) for p in cands]
    return out


def files_for_batch(root: Path, marker: Path,
                    topics: tuple[str, ...]) -> dict[str, list[tuple[Path, str]]]:
    man = _manifest(root, marker)
    return _from_manifest(root, man, topics) if man else _window(root, marker, topics)


def read_lines(path: Path) -> list[dict]:
    """读一个 JSONL 文件。

    ⚠️ **契约（ticket P0-COLLECT2f/2g 明确要求，2026-10-23 复核后维持不变）**：
    「整文件 `json.loads` 逐行；**空行跳过**；**任何一行解析失败 ⇒ 直接抛**
    （不吞、不跳坏行）」。

    ⇒ **不要**在这里改成「坏行跳过、好行照装」—— 那会**掩盖数据损坏**
    （文件里少装了多少行没人知道），且违背队长定的契约。
    ⇒ 坏文件的正确处置是**在装载之前把它从落港目录隔离出去**
    （见 ``docs/infra/jc-staging-retention.sh`` 的「坏文件隔离」步骤）：
    隔离后它不再被装载 ⇒ 也不会再拖死同批其它文件；
    同时 ``ops.ingest_log`` 会留一行 ok=false 记录「这个文件读不出来」⇒ 排障可见。
    """
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(s) for s in f if s.strip()]
    except PermissionError:
        logger.warning("permission-denied topic=%s 文件=%s", path.parent.name, path)
        return []
