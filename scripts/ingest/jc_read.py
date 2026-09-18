"""P0-COLLECT2o 自 jc_load 拆出：批↔文件归属与读行工具；.done 清单优先（行数对账），0 字节 marker 走时间戳窗口兜底。"""
import json
import re
from datetime import datetime
from pathlib import Path

from ingest.jc_manifest import _count_lines, _manifest, _check_entry, _resolve
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
        out[topic] = [] if not entries and topic in OPTIONAL else ([(None, "missing")]
            if not entries else [_check_entry(topic, root / rel, decl) for rel, decl, _h in entries])
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
    with open(path, encoding="utf-8") as f:
        return [json.loads(s) for s in f if s.strip()]
