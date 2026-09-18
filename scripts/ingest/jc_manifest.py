"""P0-JCSPLIT3b 自 jc_read 搬出：.done 清单解析四件套（_count_lines/_manifest/_check_entry/_resolve）。"""
import hashlib
from pathlib import Path

from core.log import logger


def _count_lines(path: Path) -> int:
    with open(path, encoding="utf-8") as f:
        return sum(1 for s in f if s.strip())


def body_hash(p: Path) -> str:
    """采集机 .done 第三列定义（队长 14:52 三批真包 7/7 复算命中）：sha256(逐行 utf-8 字节去掉换行按序拼接)。"""
    return hashlib.sha256("".join(p.read_text(encoding="utf-8").splitlines()).encode("utf-8")).hexdigest()


def _manifest(root: Path, marker: Path) -> dict[str, list[tuple[str, int, str]]]:
    """.done 文本清单 → {topic: [(相对路径, 声明行数, 声明哈希)]}；无前缀行挂 ""；0 字节 marker ⇒ {} 走窗口。"""
    out: dict[str, list[tuple[str, int, str]]] = {}
    for line in marker.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        rel = parts[0].strip()
        try: decl = int(parts[1].strip())
        except (IndexError, ValueError): decl = -1
        dhash = parts[2].strip() if len(parts) > 2 else ""
        key = rel.split("/", 1)[0] if "/" in rel else ""
        out.setdefault(key, []).append((rel, decl, dhash))
    return out


def _check_entry(topic: str, p: Path, decl: int) -> tuple[Path, str]:
    if not p.exists():
        logger.warning("manifest-file-missing topic=%s 文件=%s", topic, p)
        return (p, "rejected")
    actual = _count_lines(p)
    if actual != decl:
        logger.warning("manifest-mismatch topic=%s 声明=%d 实际=%d 文件=%s",
                       topic, decl, actual, p)
        return (p, "rejected")
    return (p, p.suffix.lstrip("."))


def _resolve(root: Path, topics: tuple[str, ...], rel: str, decl: int, dhash: str = ""):
    """清单丢了目录前缀时消歧：先按声明哈希精确归属（唯一命中才算），退回唯一"行数吻合"，否则 None（不许猜）。"""
    if dhash and dhash != "undefined":
        hits = [(t, root / t / rel) for t in topics
                if (root / t / rel).is_file() and body_hash(root / t / rel) == dhash]
        if len(hits) == 1:
            logger.warning("manifest-哈希消歧 topic=%s 文件=%s", hits[0][0], rel)
            return hits[0]
        logger.warning("manifest-hash-%s rel=%s 声明=%s 命中=%d",
                       "ambiguous" if hits else "mismatch", rel, dhash[:12], len(hits))
        return None
    hits = [(t, root / t / rel) for t in topics
            if (root / t / rel).is_file() and _count_lines(root / t / rel) == decl]
    if len(hits) != 1:
        logger.warning("manifest-无法消歧 rel=%s 声明=%d 命中=%d", rel, decl, len(hits))
        return None
    logger.warning("manifest-无目录前缀 topic=%s 文件=%s（按目录消歧）", hits[0][0], rel)
    return hits[0]
