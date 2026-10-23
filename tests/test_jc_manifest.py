"""P0-COLLECT2q4c：.done 第三列级联哈希精确归属 + 半传检测（T1 证据在 /tmp/q4，本文件守语义不碰生产件）。"""
from pathlib import Path

from ingest.jc_manifest import _manifest, _resolve, body_hash
from ingest.jc_read import _from_manifest, files_for_batch


def _two_topics(root: Path, rel: str, lines_a: int, lines_b: int = None) -> tuple[Path, Path]:
    """同 rel 落在两个 topic、行数相同（lines_b=None ⇒ 同 lines_a）但内容不同。"""
    n_b = lines_b if lines_b is not None else lines_a
    fa, fb = root / "jczq_offer" / rel, root / "jczq_result" / rel
    fa.parent.mkdir(parents=True, exist_ok=True)
    fb.parent.mkdir(parents=True, exist_ok=True)
    fa.write_text("\n".join(f"offer-{i}" for i in range(lines_a)) + "\n", encoding="utf-8")
    fb.write_text("\n".join(f"result-{i}" for i in range(n_b)) + "\n", encoding="utf-8")
    return fa, fb


def test_hash_attribution_same_linecount(tmp_path):
    """老算法必错（两 topic 同行数同 rel ⇒ 命中 2 猜不了）、新算法必对：各归各自。"""
    rel = "2026-09-16T04-00-00Z.jsonl"
    fa, fb = _two_topics(tmp_path, rel, 3)
    topics = ("jczq_offer", "jczq_result")
    assert _resolve(tmp_path, topics, rel, 3, body_hash(fa)) == ("jczq_offer", fa)
    assert _resolve(tmp_path, topics, rel, 3, body_hash(fb)) == ("jczq_result", fb)
    assert _resolve(tmp_path, topics, rel, 3) is None  # 无哈希 ⇒ 老档命中 2 ⇒ 仍不猜


def test_hash_mismatch_not_loaded(tmp_path, caplog):
    fa, _ = _two_topics(tmp_path, "x.jsonl", 3)
    assert _resolve(tmp_path, ("jczq_offer", "jczq_result"), "x.jsonl", 3, "0" * 64) is None
    assert "manifest-hash-mismatch rel=x.jsonl" in caplog.text


def test_hash_ambiguous_not_loaded(tmp_path, caplog):
    fa, fb = _two_topics(tmp_path, "z.jsonl", 2)
    fb.write_text(fa.read_text(encoding="utf-8"), encoding="utf-8")  # 内容同 ⇒ 哈希同 ⇒ 命中 2
    assert _resolve(tmp_path, ("jczq_offer", "jczq_result"), "z.jsonl", 2, body_hash(fa)) is None
    assert "manifest-hash-ambiguous" in caplog.text


def test_hash_undefined_or_empty_falls_back(tmp_path):
    fa, _ = _two_topics(tmp_path, "w.jsonl", 3, 5)
    topics = ("jczq_offer", "jczq_result")
    assert _resolve(tmp_path, topics, "w.jsonl", 3, "undefined") == ("jczq_offer", fa)
    assert _resolve(tmp_path, topics, "w.jsonl", 3, "") == ("jczq_offer", fa)
    assert _resolve(tmp_path, topics, "w.jsonl", -1, "") is None  # 老档不命中 ⇒ None（与今天一致）


def test_manifest_parses_hash_column(tmp_path):
    marker = tmp_path / "m.done"
    marker.write_text(
        "jczq_offer/a.jsonl\t1\t" + "f" * 64 + "\n"
        "b.jsonl\t2\tundefined\n"
        "c.jsonl\t0\n", encoding="utf-8")
    man = _manifest(tmp_path, marker)
    assert man["jczq_offer"] == [("jczq_offer/a.jsonl", 1, "f" * 64)]
    assert man[""] == [("b.jsonl", 2, "undefined"), ("c.jsonl", 0, "")]


def test_from_manifest_prefix_rows_bypass_hash_tier(tmp_path):
    """带 topic/ 前缀的行不启用哈希档（维持正常路径语义）。"""
    fa, _ = _two_topics(tmp_path, "p.jsonl", 2)
    man = {"jczq_offer": [("jczq_offer/p.jsonl", 2, "badhash")]}
    out = _from_manifest(tmp_path, man, ("jczq_offer", "jczq_result"))
    assert out["jczq_offer"][0] == (fa, "jsonl")


def test_files_for_batch_hash_disambiguation(tmp_path):
    rel = "2026-09-16T04-00-00Z.jsonl"
    fa, _ = _two_topics(tmp_path, rel, 3)
    (tmp_path / "cn-collector-2026-09-16T04-00-00Z.done").write_text(
        f"{rel}\t3\t{body_hash(fa)}\n", encoding="utf-8")
    out = files_for_batch(tmp_path, tmp_path / "cn-collector-2026-09-16T04-00-00Z.done",
                          ("jczq_offer", "jczq_result"))
    assert out["jczq_offer"] == [(fa, "jsonl")]
    # ⚠️ **2026-10-23 订正（gaps 口径）**：清单里**压根没有** jczq_result
    #   ⇒ 该批的设计里就不含它 ⇒ state="absent"（**不是缺档**）。
    #   旧口径是 "missing" ⇒ 凭空造一条 `<marker>/jczq_result#MISSING`
    #   ⇒ **gaps 虚高**（实测 49 条 gaps 全是这类假缺档）。
    assert out["jczq_result"] == [(None, "absent")]


def test_manifest_listed_but_file_missing_is_rejected_not_absent(tmp_path):
    """⚠️ **2026-10-23**：真缺档与「本就不含」必须能区分。

    - 清单**列了**某文件、但文件读不出来 ⇒ state="rejected" ⇒ **真缺档**
      （文件真的丢了/坏了 ⇒ 批次标 not_ok ⇒ 下轮重试）。
    - 清单**没列**某 topic ⇒ state="absent" ⇒ **不是缺档**（该批设计上不含它）。
    ⇒ 这条锁住两者不被混为一谈：混了 ⇒ gaps 虚高（假缺档淹没真缺档）。
    """
    topic_dir = tmp_path / "jczq_offer"
    topic_dir.mkdir()
    missing_file = topic_dir / "gone.jsonl"       # 清单列了，但盘上没有
    (tmp_path / "b.done").write_text(
        "jczq_offer/gone.jsonl\t7\t" + "0" * 64 + "\n", encoding="utf-8")
    out = files_for_batch(tmp_path, tmp_path / "b.done", ("jczq_offer", "jczq_result"))
    assert out["jczq_offer"] == [(missing_file, "rejected")], "清单列了却缺失 ⇒ 真缺档(rejected)"
    assert out["jczq_result"] == [(None, "absent")], "清单没列 ⇒ 不是缺档(absent)"
