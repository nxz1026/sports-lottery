"""认队身份用例（P0-TEAMKEY1）：哨兵 id 区段分配 + "同一 af_id 两个名字串只留一行"。

**哨兵 id 区段（全库唯一，永不重叠）**：`_af_body`/`_fd_body` 的队 id 是从场次哨兵 id 推出来的（AF = fid+1/fid+2、
FD = fid+3/fid+4，见 tests/test_align.py），所以**两个用例只要区段相交，就会给不同队名共用一个俱乐部 id**：
老代码（按 name_cn upsert）当场插第二行撞 `team_af_key`，修好后则被静默并成一行 —— 两种结果都不是用例想测的东西。
故分区：本文件 AF 99103xxxx / FD 99104xxxx；tests/test_align.py AF 99101xxxx / FD 99102xxxx；
tests/test_upsert_from_raw.py AF 99000xxxx / FD 55000xxxx。真库 ref.team 无 9xxxxxxx 段（哨兵只活在回滚事务里）。

风格说明：本文件与 tests/test_upsert_from_raw.py 同构（复用 test_align 的假报文helper + 自己的 conn fixture）；
新用例单开文件是因为 test_align.py 已**正好 100 行**（红线：单文件 ≤100 行），加不进去。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.i18n import to_cn  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from store import pg, upsert_fixtures  # noqa: E402
from store.team_identity import FD_KEY  # noqa: E402

from tests.test_align import _af_body, _fd_body, _put, _q  # noqa: E402


@pytest.fixture()
def conn():
    try:
        connection = pg.connect("ing")
    except Exception as exc:
        pytest.skip(f"no db: {type(exc).__name__}: {exc}")
    yield connection
    connection.rollback()
    connection.close()


def test_same_af_id_two_names_keeps_one_row(conn):
    """T3：同一 af_id、两个名字串先后进 raw（AF 跨季改拼写，真数据 157 拜仁就是这么裂成两行的）⇒ ref.team 只多
    1 行、name_cn 保持第一次的值、两个源 id 都落进 aliases（这才是"按 id 认队"的意义：改名不裂行、两源对得上）。
    """
    tag, fid, fd = uuid.uuid4().hex, 991030001, 991040001
    older, newer = f"Bayern Munich {tag}", f"Bayern München {tag}"
    first, again = _af_body(fid, tag, (1, 0), (0, 0), home=older), _af_body(fid + 16, tag, (2, 0), (1, 0), home=newer)
    again["response"][0]["teams"]["home"]["id"], again["response"][0]["teams"]["away"]["id"] = fid + 1, fid + 2
    before = _q(conn, "SELECT count(*) FROM ref.team")[0][0]
    written = upsert_fixtures.run(conn, [_put(conn, "af_raw", first), _put(conn, "af_raw", again),
                                         _put(conn, "fd_raw", _fd_body(fd, tag, (2, 0), (1, 0), home=newer))])
    assert written["fd_merged"] == 1  # 换了拼写也照样对齐：FD 认的是场次+队形，不认"必须同名"
    assert _q(conn, "SELECT count(*) FROM ref.team")[0][0] - before == 2  # 两队各 1 行（不是"两场四行"）
    assert _q(conn, "SELECT name_cn, aliases FROM ref.team WHERE af_id = %s", fid + 1)[0] \
        == (to_cn(older), {"api_football": fid + 1, "football_data": fd + 3})  # 展示名不翻脸 + af/fd 两键齐全


def test_fd_id_owned_by_another_row_is_never_stolen(conn):
    """FD 队 id 已被**别的行**占着（真库现状：38 家俱乐部跨键重复，一个 af_id 行 + 一个 fd_id 行）⇒ 不覆盖、不撞
    team_fd_key（那是 unique violation，会连累整批落库），只在 fd_id_taken 上如实报数。
    """
    tag, fid, fd = uuid.uuid4().hex, 991050001, 991060001
    conn.execute("INSERT INTO ref.team (sport, name_cn, aliases) VALUES ('football', %s, %s)",
                 (f"Occupied {tag}", Jsonb({FD_KEY: fd + 3})))  # 另一行先占住这一侧的 FD 队 id
    written = upsert_fixtures.run(conn, [_put(conn, "af_raw", _af_body(fid, tag, (2, 0), (1, 0))),
                                         _put(conn, "fd_raw", _fd_body(fd, tag, (2, 0), (1, 0)))])
    assert (written["fd_merged"], written["fd_id_taken"]) == (1, 1)  # 场次照样合并；被占的那个队 id 只报数
    assert _q(conn, "SELECT aliases FROM ref.team WHERE af_id = %s", fid + 1)[0] == ({"api_football": fid + 1},)
    assert _q(conn, "SELECT aliases FROM ref.team WHERE name_cn = %s", f"Occupied {tag}")[0] == ({FD_KEY: fd + 3},)
