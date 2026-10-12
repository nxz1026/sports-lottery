"""P2-7 验收测试：store.load_prediction_docs LRU + mtime 失效缓存。

修复点：旧实现每请求 glob + json.load 全部预测文件，100+ 文件 × ~50KB JSON
导致 /api/v1/predictions/today 等端点每次扫描 ~30-50ms；同请求内多端点
重复扫描更糟。

修复：模块级缓存 + 文件签名（count + 每文件 mtime 元组）：
- 同签名命中缓存 → 0 IO
- 文件增删 / 引擎写入新文件 → mtime 变化 → 自动失效
- 测试可通过 web.services.store._load_prediction_docs_cached.clear() 强制失效
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1].parent
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _write(path: Path, payload: dict, mtime_offset_seconds: int = 0) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    if mtime_offset_seconds:
        # 改 mtime
        new_mtime = time.time() - mtime_offset_seconds
        import os
        os.utime(path, (new_mtime, new_mtime))


def test_cache_hits_on_repeat_within_same_signature(tmp_path, monkeypatch):
    """同签名（同文件 + 同 mtime）连续调用 → 第二次命中缓存（无 IO）。"""
    from web.services import store as store_mod

    monkeypatch.setattr(store_mod, "OUTPUT_DIR", tmp_path)
    # 第一次写文件
    p1 = tmp_path / "predictions" / "prediction_2026-10-12_10.json"
    p1.parent.mkdir(parents=True)
    _write(p1, {"predictions": [], "generated_at": "2026-10-12T10:00:00+08:00"})

    # 清缓存
    store_mod._load_prediction_docs_cached.clear()
    docs1 = store_mod.load_prediction_docs()
    assert len(docs1) == 1

    # 模拟文件被 stat 后又读取（无变化）。第二次调用应走缓存路径：
    # 把 JSON 内容破坏但 mtime 仍一致（验缓存确实生效）。
    p1.write_text("{garbage", encoding="utf-8")
    docs2 = store_mod.load_prediction_docs()
    # 缓存命中，第二次返回缓存中的旧结果（不是空 json 解析失败）
    assert docs2 == docs1, (
        f"缓存未命中：第二次读到的不是第一次的结果；docs1={len(docs1)} docs2={len(docs2)}"
    )


def test_cache_invalidates_on_new_file(tmp_path, monkeypatch):
    """新增文件（mtime 新）→ 缓存失效，重建。"""
    from web.services import store as store_mod

    monkeypatch.setattr(store_mod, "OUTPUT_DIR", tmp_path)
    (tmp_path / "predictions").mkdir()
    p1 = tmp_path / "predictions" / "prediction_2026-10-12_10.json"
    _write(p1, {"predictions": [{"match": "a"}]})

    store_mod._load_prediction_docs_cached.clear()
    docs1 = store_mod.load_prediction_docs()
    assert len(docs1) == 1

    # 新增文件
    p2 = tmp_path / "predictions" / "prediction_2026-10-12_11.json"
    _write(p2, {"predictions": [{"match": "b"}]})
    docs2 = store_mod.load_prediction_docs()
    assert len(docs2) == 2, f"新增文件应触发缓存失效重建；got {len(docs2)}"


def test_cache_invalidates_on_file_modified(tmp_path, monkeypatch):
    """已有文件被覆盖 → mtime 变化 → 失效。"""
    from web.services import store as store_mod
    import os

    monkeypatch.setattr(store_mod, "OUTPUT_DIR", tmp_path)
    (tmp_path / "predictions").mkdir()
    p1 = tmp_path / "predictions" / "prediction_2026-10-12_10.json"
    _write(p1, {"predictions": [{"match": "v1"}]})

    store_mod._load_prediction_docs_cached.clear()
    docs1 = store_mod.load_prediction_docs()
    assert docs1[0]["data"]["predictions"][0]["match"] == "v1"

    # 覆盖内容 + 推迟时间戳
    time.sleep(1.1)  # 跨秒级 mtime 粒度（FAT/部分 FS 是 2s）
    _write(p1, {"predictions": [{"match": "v2"}]})

    docs2 = store_mod.load_prediction_docs()
    assert docs2[0]["data"]["predictions"][0]["match"] == "v2", (
        f"文件覆写应触发缓存失效；got {docs2[0]['data']['predictions'][0]['match']!r}"
    )


def test_cache_invalidates_on_file_deleted(tmp_path, monkeypatch):
    """文件被删除 → count 变化 → 失效。"""
    from web.services import store as store_mod

    monkeypatch.setattr(store_mod, "OUTPUT_DIR", tmp_path)
    (tmp_path / "predictions").mkdir()
    p1 = tmp_path / "predictions" / "prediction_2026-10-12_10.json"
    p2 = tmp_path / "predictions" / "prediction_2026-10-12_11.json"
    _write(p1, {"predictions": [{"match": "a"}]})
    _write(p2, {"predictions": [{"match": "b"}]})

    store_mod._load_prediction_docs_cached.clear()
    assert len(store_mod.load_prediction_docs()) == 2

    p2.unlink()
    assert len(store_mod.load_prediction_docs()) == 1, (
        "文件删除应触发缓存失效"
    )


def test_cache_clear_for_test_isolation():
    """缓存模块暴露 clear() 方法便于测试隔离。"""
    from web.services import store as store_mod

    store_mod._load_prediction_docs_cached["any_key"] = ("sig", ["doc"])
    store_mod._load_prediction_docs_cached.clear()
    assert store_mod._load_prediction_docs_cached == {}