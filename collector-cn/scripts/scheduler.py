#!/usr/bin/env python3
"""采集排期器 —— 无 cron/systemd 环境下的自管调度。

腾讯沙箱里 `crontab` 与 systemd 都不可用（前者 Permission denied，后者要 no_new_privs
之外的权限），所以用一个常驻进程自己算时间点；由 supervise.sh 拉起并看护。

排期（北京时间，等价于原来的 5 个 Windows 计划任务）：
  每 10 分钟            → offer（7 topic 快照；实施文档 §5.8 的盘口时序硬要求）
  09:30 / 15:30 / 21:30 → offer
  次日 01:30            → offer
  23:30                 → night（8 topic，多一个 jc_odds_history）

设计取舍：
  * **不补跑**。offer 快照是时序数据，错过的点补不回来；漏跑只在日志里留痕，
    避免进程重启后一次性灌出一堆过期快照。
  * 重叠由 collector.py 的单例锁兜底：上一批没跑完时，本批直接 no-op 退出。
  * 只依赖 stdlib，不引入任何第三方包。
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CN_DIR = HERE.parent
WORK_ROOT = Path(os.environ.get("COLLECTOR_WORK_ROOT", "/home/agentuser/Work"))
LOGS = CN_DIR / "logs"
LOG = LOGS / "scheduler.log"
HEART = LOGS / "scheduler.heartbeat"
SEEN = LOGS / "scheduler.seen"
RUNNER = HERE / "collector_linux.sh"

CN = dt.timezone(dt.timedelta(hours=8))
TICK = 20                      # 轮询间隔（秒）
RUN_TIMEOUT = 900              # 单批上限（秒）
LOG_MAX = 5 * 1024 * 1024      # 单个 mode 日志超过 5MB 就轮转

DAILY = {
    "09:30": "offer",
    "15:30": "offer",
    "21:30": "offer",
    "01:30": "offer",
    "23:30": "night",
}


def log(msg: str) -> None:
    stamp = dt.datetime.now(CN).strftime("%Y-%m-%dT%H:%M:%S%z")
    line = f"{stamp} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_seen() -> set:
    if not SEEN.exists():
        return set()
    keep_from = (dt.datetime.now(CN) - dt.timedelta(days=2)).strftime("%Y-%m-%d")
    return {k for k in SEEN.read_text(encoding="utf-8").split() if k[:10] >= keep_from}


def save_seen(keys: set) -> None:
    SEEN.write_text("\n".join(sorted(keys)) + "\n", encoding="utf-8")


def due(now: dt.datetime, seen: set) -> list:
    """这一分钟该跑的 (slot_key, mode) 列表。"""
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    jobs = []
    if now.minute % 10 == 0:
        jobs.append((f"{stamp}#10m", "offer"))
    mode = DAILY.get(now.strftime("%H:%M"))
    if mode:
        jobs.append((f"{stamp}#{now.strftime('%H%M')}", mode))
    return [(k, m) for k, m in jobs if k not in seen]


def rotate(mode: str) -> None:
    path = LOGS / f"collector_{mode}.log"
    try:
        if path.exists() and path.stat().st_size > LOG_MAX:
            path.replace(LOGS / f"collector_{mode}.log.1")
    except OSError as exc:                                    # 轮转失败不能拖垮采集
        log(f"日志轮转失败 mode={mode}: {exc}")


def run(mode: str) -> None:
    rotate(mode)
    log(f"启动 mode={mode}")
    t0 = time.time()
    try:
        rc = subprocess.run([str(RUNNER), mode], timeout=RUN_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        log(f"mode={mode} 超时 {RUN_TIMEOUT}s，放弃本批")
        rc = -1
    except OSError as exc:
        log(f"mode={mode} 无法启动: {exc}")
        rc = -2
    log(f"结束 mode={mode} rc={rc} 用时 {time.time() - t0:.1f}s")


def main() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    os.umask(0o022)
    log(f"scheduler 启动 pid={os.getpid()} cn_dir={CN_DIR} work_root={WORK_ROOT}")
    seen = load_seen()
    while True:
        now = dt.datetime.now(CN)
        HEART.write_text(now.isoformat(timespec="seconds") + "\n", encoding="utf-8")
        for key, mode in due(now, seen):
            seen.add(key)
            save_seen(seen)
            run(mode)
        time.sleep(TICK)


if __name__ == "__main__":
    sys.exit(main())
