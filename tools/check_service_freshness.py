"""部署后自检：**运行中的 web 服务**的代码版本 == 仓库 HEAD？

为什么需要（2026-10-23 生产实测）：
  部署后忘了 ``systemctl restart league-web`` ⇒ 服务内存里仍是**旧代码**，
  而 ``static/dashboard.html`` 是**每次请求现读磁盘**的 ⇒ 页面变成
  **「静态页是新的、API 是旧的」混合态**：
  页面显示「⚠️ 暂无定价赔率快照（未采集）/ 暂无推荐单」，
  而库里明明有 1.6 小时前的新鲜快照 + 3 条当日荐单 ⇒ 用户以为「没采到」。
  （实测那次就是这样查出来的：服务 10-21 15:21 起没重启过，直到 10-23 09:11 才重启。）

用法：
  python tools/check_service_freshness.py            # 默认查 http://127.0.0.1:8077/health
  python tools/check_service_freshness.py --url http://127.0.0.1:8077/health

退出码：0 = 一致（或服务不可达 ⇒ 未检查，见输出）；1 = **服务落后于仓库**。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "http://127.0.0.1:8077/health"


def _local_head() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=10,
        )
        sha = (out.stdout or "").strip()
        return sha or None
    except Exception as exc:  # noqa: BLE001
        print(f"读本地 HEAD 失败：{exc}", file=sys.stderr)
        return None


def _remote_sha(url: str) -> tuple[str | None, str | None]:
    """返回 (code_sha, error)。"""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    sha = data.get("code_sha")
    return (str(sha) if sha else None), None


def main(argv: list[str] | None = None) -> int:
    # ⚠️ argv 走参数（不读 sys.argv）：这样单测能直接调 main([])，
    #    不会被 pytest 的命令行参数喂给 argparse 而直接退出。
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    args = ap.parse_args(argv)

    local = _local_head()
    remote, err = _remote_sha(args.url)

    if err:
        print(f"⚠ 未检查：{args.url} 不可达（{err}）")
        print("   服务没跑或地址不对 ⇒ 本次**未检查**，不是「一致」。")
        return 0
    if remote is None:
        print(f"⚠ 未检查：{args.url} 的响应里没有 code_sha ⇒ 服务是旧版本（没有该字段）")
        print("   ⇒ 需要 systemctl restart league-web（重启后才会暴露 code_sha）")
        return 1
    if local is None:
        print("⚠ 未检查：读不到本地 HEAD")
        return 0
    if remote == local:
        print(f"✓ 服务代码版本与仓库一致：{remote}")
        return 0

    print("✗ 服务代码版本落后于仓库！")
    print(f"    运行中的服务：{remote}")
    print(f"    仓库 HEAD   ：{local}")
    print("    ⇒ 静态页是新的、API 还是旧的 ⇒ 页面会出现「假的无数据」")
    print("    ⇒ 修复：sudo systemctl restart league-web")
    print("      然后重跑本脚本确认一致。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
