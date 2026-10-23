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

退出码（**三态**，2026-10-23 修）：
  0 = 一致
  1 = **落后于仓库** 或 **响应异常**（fail-closed：服务活着但答非所问 ⇒ 也是问题）
  2 = **未检查**（服务不可达 / 读不到本地 HEAD）—— 原来这里也返回 0，
      于是 shell 门禁里「未检查」与「通过」**同码** ⇒ 每次部署自检都可能假绿。
      （审计实测：起一个返回 HTML 的假 /health，脚本印「未检查」却 exit 0。）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "http://127.0.0.1:8077/health"

EXIT_OK = 0
EXIT_STALE = 1
EXIT_UNCHECKED = 2


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


def _remote_sha(url: str) -> tuple[str | None, str | None, str | None]:
    """返回 (code_sha, error, kind)。

    ``kind``：``None`` = 正常；``"unreachable"`` = 连不上；``"bad_response"`` =
    连上了但**响应不是预期的 JSON**（服务活着、答非所问 ⇒ 也要 fail-closed）。
    ⚠️ 原来这两种都归成同一个 ``error`` ⇒ 诊断文案会把「返回了 502 页面」说成
    「服务没跑」⇒ 把排障引向错误方向。
    """
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            raw = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}", "unreachable"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}", "unreachable"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"响应不是合法 JSON（{exc}）", "bad_response"
    if not isinstance(data, dict):
        return None, "响应不是 JSON 对象", "bad_response"
    sha = data.get("code_sha")
    return (str(sha) if sha else None), None, None


def main(argv: list[str] | None = None) -> int:
    # ⚠️ argv 走参数（不读 sys.argv）：这样单测能直接调 main([])，
    #    不会被 pytest 的命令行参数喂给 argparse 而直接退出。
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    args = ap.parse_args(argv)

    local = _local_head()
    remote, err, kind = _remote_sha(args.url)

    if err:
        if kind == "bad_response":
            # 服务**活着**但答非所问（反代 502 页 / 登录挑战页 / 协议不对）
            # ⇒ 这是**问题**，不是「没检查」⇒ fail-closed（退出码 1）。
            print(f"✗ {args.url} 的响应不是预期 JSON（{err}）")
            print("    ⇒ 服务活着但答非所问（反代错误页？鉴权挑战页？）")
            print("    ⇒ 先确认反代与 /health 的可达性，再 systemctl restart league-web")
            return EXIT_STALE
        print(f"⚠ 未检查：{args.url} 不可达（{err}）")
        print("   服务没跑或地址不对 ⇒ 本次**未检查**，不是「一致」。")
        print("   ⇒ 退出码 2（**未检查**），shell 门禁里不许当成通过。")
        return EXIT_UNCHECKED
    if remote is None:
        print(f"✗ {args.url} 的响应里没有 code_sha ⇒ 服务是旧版本（没有该字段）")
        print("   ⇒ 需要 systemctl restart league-web（重启后才会暴露 code_sha）")
        return EXIT_STALE
    if local is None:
        print("⚠ 未检查：读不到本地 HEAD")
        print("   ⇒ 退出码 2（**未检查**），shell 门禁里不许当成通过。")
        return EXIT_UNCHECKED
    if remote == local:
        print(f"✓ 服务代码版本与仓库一致：{remote}")
        return EXIT_OK

    print("✗ 服务代码版本落后于仓库！")
    print(f"    运行中的服务：{remote}")
    print(f"    仓库 HEAD   ：{local}")
    print("    ⇒ 静态页是新的、API 还是旧的 ⇒ 页面会出现「假的无数据」")
    print("    ⇒ 修复：sudo systemctl restart league-web")
    print("      然后重跑本脚本确认一致。")
    return EXIT_STALE


if __name__ == "__main__":
    raise SystemExit(main())
