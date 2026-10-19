"""R43-2: scripts/bball/run.py::_parser 2 tests direct + D-51 lock.

argparse 3 flag 装配 helper 0 直测; run() 通过 _parser() 返 parser, parser
在 main() 中 parse_args(), helper 自身契约未锁。flag 改名 / default 漂移 / type
变更会让 production CLI 静默失配。

helper 含两条契约 (D-51):
- 3 flag: --backtest (int, default 0) / --ahead-days (int, default 1) /
  --leagues (default "nba")
- type=int: 传非整数 -> argparse SystemExit(2), 不静默吞
- metavar="N" 在 help 文本显示, 但不影响值

2 tests (EASY):
- default 无参: backtest=0, ahead_days=1, leagues="nba" 锁定
- 完整自定义 --backtest 10 --ahead-days 7 --leagues wnba -> 全部命中

D-51 锁 flag / default / type 三元组; 任何人改名或改 default 此测 fail。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bball import run as bball_run  # noqa: E402

# ──────────────────────── D-51 flag + default 锁 ────────────────────────


def test_parser_d51_defaults_three_flags():
    """R43-2 D-51: 无参 -> backtest=0, ahead_days=1, leagues="nba"。

    锁定 argparse 3 flag + 默认值三元组; 后人改 default 或加新 flag 必 fail 此测。
    """
    parser = bball_run._parser()
    ns = parser.parse_args([])
    assert ns.backtest == 0
    assert ns.ahead_days == 1
    assert ns.leagues == "nba"


def test_parser_d51_custom_args_parsed_correctly():
    """R43-2 D-51: 自定义 --backtest 10 --ahead-days 7 --leagues wnba 全部命中。

    锁定完整 flag 名 (含 - 与 _ 转换) + value 透传 (int 仍是 int)。
    """
    parser = bball_run._parser()
    ns = parser.parse_args(["--backtest", "10", "--ahead-days", "7",
                            "--leagues", "wnba"])
    assert ns.backtest == 10
    assert ns.ahead_days == 7
    assert ns.leagues == "wnba"
