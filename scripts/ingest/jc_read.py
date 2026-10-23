"""P0-COLLECT2o 自 jc_load 拆出：批↔文件归属与读行工具；.done 清单优先（行数对账），0 字节 marker 走时间戳窗口兜底。"""
import json
import re
from datetime import datetime
from pathlib import Path

from core.log import logger

from ingest.jc_manifest import _check_entry, _manifest, _resolve

OPTIONAL = ("jc_odds_history",)


class InvalidFileNameError(ValueError):
    """文件名里的**批次时间戳**推不出来（异形名）—— 与 :class:`UnreadableFileError`
    同级的「**文件级可隔离**」错误。

    ⚠️ **P2-C（2026-10-23）**：旧实现直接 ``m.group(1)``（**不校验 m**）⇒
    名字里没有 ``YYYY-MM-DDTHH-MM`` 时抛
    ``AttributeError: 'NoneType' object has no attribute 'group'``
    ⇒ **掀翻整批**（``load_batch`` 的 except ⇒ ``conn.rollback()``）
    ⇒ marker 永久 ``ok=false`` ⇒ **每 10 分钟重装整批**
    （2026-10-23 那次「每天凭空 1 万行 ingest_log」同款后果）。
    ⇒ 现在抛**可区分**的类型，调用方按**文件级**隔离处理：
    该文件记失败 + 原因，**其余文件继续**（沿用 P1 的 ``UnreadableFileError`` 范式，
    不造第二套机制）。
    """


def _stamp(name: str) -> datetime:
    """从文件名推导批次时间戳；推不出来 ⇒ 抛 :class:`InvalidFileNameError`。

    ⚠️ 非法时间戳（如 ``2026-13-45T99-99``）也走同一条路：``datetime`` 会抛
    ``ValueError`` ⇒ 这里统一包成 :class:`InvalidFileNameError`（可区分、不掀翻整批）。
    """
    m = re.search(r"(\d{4})-(\d\d)-(\d\d)T(\d\d)-(\d\d)(?:-(\d\d))?", name)
    if not m:
        raise InvalidFileNameError(f"文件名里没有批次时间戳: {name!r}")
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)), int(m.group(6) or "0"))
    except ValueError as e:
        raise InvalidFileNameError(f"文件名时间戳非法: {name!r}") from e


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
    """0 字节 marker（无清单）的兜底归属：按**文件名时间戳窗口**取文件。

    ⚠️ **P2-C（2026-10-23）**：异形文件名**不许掀翻整批**。
    旧实现对 topic 目录下每个 ``*.jsonl``/``*.empty`` 直接调 ``_stamp(p.name)``；
    只要有一个文件的名字里推不出时间戳（非法字符 / 超长 / 非法时间戳）⇒ 抛
    ``AttributeError`` ⇒ **整批回滚**（``load_batch`` 的 except ⇒ ``conn.rollback()``）
    ⇒ marker 永久 ``ok=false`` ⇒ **每 10 分钟重装整批**
    （2026-10-23 那次「每天凭空 1 万行 ingest_log」同款后果）。

    ⇒ 现在**单文件级隔离**（沿用 P1 的 ``UnreadableFileError`` 范式，不造第二套）：
      - 候选文件推不出时间戳 ⇒ 不参与窗口归属，但以 ``state="badname"`` 交回调用方
        ⇒ 调用方**入台账**（记失败 + 原因），**其余文件照常装载**；
      - marker 自己的名字异形 ⇒ 本批时间窗推不出来 ⇒ **不猜**（不认领任何文件），
        记 warning；这些文件会落到 orphan 阶段被装载（**不丢数据、不掀翻整批**）。
    """
    ms = iter_markers(root)
    i = ms.index(marker)
    try:
        hi = _stamp(marker.name)
    except InvalidFileNameError as e:
        logger.warning("marker 文件名异形（跳过本批归属，文件交由 orphan 阶段）: %s", e)
        return {}
    lo = datetime.min
    if i:
        try:
            lo = _stamp(ms[i - 1].name)
        except InvalidFileNameError as e:
            logger.warning("前一个 marker 文件名异形（窗口下界退化为最早）: %s", e)
            lo = datetime.min
    out = {}
    for topic in topics:
        cands: list[tuple[Path, str]] = []
        for pat in ("*.jsonl", "*.empty"):
            for p in (root / topic).glob(pat):
                try:
                    stamp = _stamp(p.name)
                except InvalidFileNameError as e:
                    # ⚠️ 文件级隔离：不掀翻整批，但要**留痕**（state="badname"
                    # ⇒ 调用方写 ingest_log ok=false + 原因）。
                    logger.warning("异形文件名（已隔离，不掀翻整批）: %s", e)
                    cands.append((p, "badname"))
                    continue
                if lo < stamp <= hi:
                    cands.append((p, p.suffix.lstrip(".")))
        cands.sort(key=lambda item: item[0].name)
        out[topic] = cands
    return out


def files_for_batch(root: Path, marker: Path,
                    topics: tuple[str, ...]) -> dict[str, list[tuple[Path, str]]]:
    man = _manifest(root, marker)
    return _from_manifest(root, man, topics) if man else _window(root, marker, topics)


class UnreadableFileError(OSError):
    """文件**读不出来**（权限 / IO 错误）—— 与「行解析失败」是**两类**问题。

    ⚠️ **P1（2026-10-23）**：以前 `read_lines` 把 `PermissionError` **吞掉并返回 []**
    ⇒ 上层看到「0 行、0 拒绝」⇒ 记 `ops.ingest_log` **ok=true, rows_in=0**
    ⇒ **文件里的数据被静默丢弃，而台账说「装成功」**（生产实测：permission-denied 44 次）。
    ⇒ 现在它**抛**，且是**可区分的类型**：上层据此记 **ok=false + 原因**，
    既不掀翻整批（同 ticket 2q3b「一个文件坏了不该让同批永远重跑」），
    也不谎报成功。
    """


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

    ⚠️ **P1（2026-10-23）新增**：`PermissionError` **不再被吞** ⇒ 抛
    :class:`UnreadableFileError`（「读不出来」必须与「读出来是空的」分开）。
    """
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(s) for s in f if s.strip()]
    except PermissionError as e:
        logger.warning("permission-denied topic=%s 文件=%s", path.parent.name, path)
        raise UnreadableFileError(f"permission-denied: {path}") from e
