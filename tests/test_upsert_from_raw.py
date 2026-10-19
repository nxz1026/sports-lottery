"""raw 假块（自 INSERT）→ ref.team/fact.fixture/fact.fixture_result：I1~I4 不变式 + A2/A3/A6/A7（未对齐 FD 只进
ops.ingest_log、fact 只随 AF 增长）。真库单事务、收尾整体 ROLLBACK（league_ing 对 fact/ref 无 DELETE），连不上库
pytest.skip；库里已有真存量（fixture/result 5341/8822、fd_align 未对齐留痕 3074 条），断言一律相对增量，或按**本批
哨兵 id** 限定作用域——`unmatched_fd` 的"最新一条"读法会被更新的生产留痕顶掉，测试与 CLI 汇总不许依赖它。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.i18n import to_cn  # noqa: E402
from store import pg, selfcheck, upsert_fixtures, upsert_results  # noqa: E402

from tests.test_align import COUNT_SQL, _af_body, _fd_body, _put, _q  # noqa: E402


@pytest.fixture()
def conn():
    try:
        connection = pg.connect("ing")
    except Exception as exc:
        pytest.skip(f"no db: {type(exc).__name__}: {exc}")
    yield connection
    connection.rollback()
    connection.close()


def test_i1_i2_i3_i4_two_sources_share_one_af_fixture(conn):
    tag, fid, fd_id = uuid.uuid4().hex, 990000101, 550000101
    hashes = [_put(conn, "af_raw", _af_body(fid, tag, (1, 1), (0, 1))),
              _put(conn, "fd_raw", _fd_body(fd_id, tag, (3, 0), (1, 0)))]  # 两源 ft 故意不一致
    before, cov_before = _q(conn, COUNT_SQL)[0], selfcheck.source_coverage(conn)["both"]
    written, results = upsert_fixtures.run(conn, hashes), upsert_results.run(conn, hashes)
    assert _q(conn, COUNT_SQL)[0][0] - before[0] == 1  # A1：fixture 只随 AF 增长（FD 贡献 0）
    assert selfcheck.source_coverage(conn)["both"] - cov_before == 1
    assert (written["fixtures"], written["fd_merged"], written["fd_unmatched"]) == (1, 1, 0)
    assert (results["af_results"], results["fd_results"], results["fd_unmatched"]) == (1, 1, 0)
    assert selfcheck.unmatched_fd(conn, fd_ids={fd_id}) == []  # A3：作用域＝本批哨兵 id，不读"史上最新留痕"
    assert _q(conn, "SELECT source, ft_h FROM fact.fixture_result WHERE fixture_id = %s ORDER BY source", fid) \
        == [("api_football", 1), ("football_data", 3)]  # A2：两源各一行，谁也不覆盖谁
    assert _q(conn, "SELECT source_ids -> 'football_data', (SELECT count(*) FROM fact.fixture"
                    " WHERE fixture_id = %s) FROM fact.fixture WHERE fixture_id = %s", fd_id, fid) == [(fd_id, 0)]
    assert _q(conn, "SELECT n_sources FROM fact.v_result_conflict WHERE fixture_id = %s", fid)[0] == (2,)  # I2
    counts = _q(conn, COUNT_SQL)[0]
    upsert_fixtures.run(conn, hashes)  # I1 幂等：同一份 raw 再跑一遍
    upsert_results.run(conn, hashes)
    assert _q(conn, COUNT_SQL)[0] == counts
    assert _q(conn, "SELECT name_cn FROM ref.team WHERE aliases->>'api_football' = %s", str(fid + 1))[0][0] \
        == to_cn(f"Home {tag}")  # I3：只认 to_cn(原名)，不造中文名
    joined = _q(conn, "SELECT count(*) FROM fact.fixture f JOIN ref.league USING (league_key)"
                      " JOIN ref.team h ON h.team_id = f.home_team_id"
                      " JOIN ref.team a ON a.team_id = f.away_team_id WHERE f.fixture_id = %s", fid)[0][0]
    assert joined == 1  # I4：主客队与联赛都能在 ref 找到


def test_unmatched_fd_only_lands_in_ingest_log(conn):
    """A3/A4/A6：队名不符与"差 60 秒"的 FD 行既不建 fixture 也不写赛果，只进 ops.ingest_log.rejected。"""
    tag, fid, other, late = uuid.uuid4().hex, 990000301, 550000301, 550000302
    hashes = [_put(conn, "af_raw", _af_body(fid, tag, (2, 0), (1, 0))),
              _put(conn, "fd_raw", _fd_body(other, tag, (2, 0), (1, 0), home=f"Other {tag}")),
              _put(conn, "fd_raw", _fd_body(late, tag, (2, 0), (1, 0), kick="2024-08-16T19:01:00Z"))]
    before = _q(conn, COUNT_SQL)[0]
    upsert_fixtures.run(conn, hashes)
    results = upsert_results.run(conn, hashes)
    after = _q(conn, COUNT_SQL)[0]
    assert (after[0] - before[0], after[1] - before[1], after[2] - before[2]) == (1, 1, 1)
    assert sorted(r["reason"] for r in results["rejected"]) == ["name_mismatch", "no_time"]
    assert _q(conn, "SELECT (SELECT count(*) FROM fact.fixture WHERE fixture_id IN (%s, %s)),"
                    " (SELECT count(*) FROM fact.fixture_result WHERE fixture_id IN (%s, %s))",
              other, late, other, late)[0] == (0, 0)  # A3：FD-only 不进 fact
    rejected = selfcheck.unmatched_fd(conn, fd_ids={other, late})  # 作用域＝本批两条哨兵 id
    assert sorted(r["fd_id"] for r in rejected) == [other, late]
    assert set(rejected[0]) == {"fd_id", "home", "away", "utc_date", "reason"}  # 明细含两队原名与开球时刻
    upsert_fixtures.run(conn, hashes)  # A6：再跑一遍，fact 计数不变
    upsert_results.run(conn, hashes)
    assert _q(conn, COUNT_SQL)[0] == (after[0], after[1], after[2] + 1)  # 只多一条 append-only 日志


def test_scoped_unmatched_fd_ignores_newer_foreign_log_row(conn):
    """本批判定只认本批哨兵 id：库里有更新的留痕（生产未对齐）也不参与；旧"最新一条"读法仍看得到它。"""
    tag, fid, fd_id, fdid = uuid.uuid4().hex, 990000401, 550000401, 560000901
    fake = [{"fd_id": fdid, "home": "Foreign A", "away": "Foreign B", "utc_date": "2024-08-16T19:00:00Z",
             "reason": "name_mismatch"}]
    conn.execute("INSERT INTO ops.ingest_log (topic, src_file, rows_in, rows_ups, rejected, ok)"
                 " VALUES ('fd_align', 'raw.fd_raw', 1, 0, %s, false)", (Jsonb(fake),))  # 假"最新"留痕
    hashes = [_put(conn, "af_raw", _af_body(fid, tag, (2, 1), (1, 0))),
              _put(conn, "fd_raw", _fd_body(fd_id, tag, (2, 1), (1, 0)))]
    written, results = upsert_fixtures.run(conn, hashes), upsert_results.run(conn, hashes)
    assert (written["fd_unmatched"], results["fd_unmatched"], results["rejected"]) == (0, 0, [])
    assert selfcheck.unmatched_fd(conn, src_file="raw.fd_raw", fd_ids={fd_id}) == []  # 同源名的两批：只算本批
    assert [r["fd_id"] for r in selfcheck.unmatched_fd(conn, src_file="raw.fd_raw", fd_ids={fdid})] == [fdid]
    assert [r["fd_id"] for r in selfcheck.unmatched_fd(conn)] == [fdid]  # 旧"最新一条"读法仍看得到假留痕
