#!/usr/bin/env python3
"""5 个 collector 计划任务统一改为 S4U + wscript.exe 直接跑 VBS，无黑框。

原理：
  - LogonType=S4U：任务无需凭据即可在非交互会话运行，开机自动登录场景下也能跑；
    配合 wscript 完全无 console 窗口，连 -WindowStyle Hidden 的短暂黑框都没有。
  - Exec：wscript.exe，Args：escaped VBS 路径 + mode（offer/night）。
  - 原任务（0930/1530/2130=offer、2330=night、offer_10m=offer）逐个 XML 往返重建。

用法：python3 scripts/convert_tasks_s4u.py [--dry-run]
"""
import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VBS = ROOT / "scripts" / "collector_silent.vbs"

NAMES = [
    ("collector_0930", "offer"),
    ("collector_1530", "offer"),
    ("collector_2130", "offer"),
    ("collector_2330", "night"),
    ("collector_offer_10m", "offer"),
]


def run(cmd: list[str], capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=capture, text=True, shell=False, encoding="utf-8", errors="replace")


def export_xml(name: str) -> str:
    """导出任务 XML（UTF-16 LE BOM），返回 UTF-8 文本。"""
    r = run(["schtasks", "/query", "/tn", name, "/xml"])
    if r.returncode != 0:
        raise RuntimeError(f"export {name} failed: {r.stderr.strip()}")
    raw = r.stdout.encode("utf-8")
    # schtasks /xml 输出常带 UTF-16 LE BOM
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le")
    return r.stdout


def build_xml(orig: str, mode: str) -> str:
    xml = orig
    xml = re.sub(r"<LogonType>.*?</LogonType>", "<LogonType>S4U</LogonType>", xml, flags=re.S)
    xml = re.sub(r"<Command>.*?</Command>", "<Command>wscript.exe</Command>", xml, flags=re.S)
    # XML 里双引号必须写成 &quot;
    vbs_escaped = str(VBS).replace("\\", "/").replace('"', "&quot;")
    xml = re.sub(r"<Arguments>.*?</Arguments>", f'<Arguments>"{vbs_escaped}" {mode}</Arguments>', xml, flags=re.S)
    return xml


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="只打印将改动的 XML，不写入")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的同名任务")
    args = ap.parse_args()

    if not VBS.is_file():
        sys.exit(f"VBS 不存在: {VBS}")

    for name, mode in NAMES:
        xml = export_xml(name)
        new_xml = build_xml(xml, mode)

        if args.dry_run:
            print(f"=== {name} (mode={mode}) ===")
            print(new_xml)
            continue

        tmp = Path(__file__).with_name(f"tmp_{name}.xml")
        # 写 UTF-16 LE BOM（schtasks /create /xml 要求）
        tmp.write_bytes(b"\xff\xfe" + new_xml.encode("utf-16-le"))

        if args.force:
            run(["schtasks", "/delete", "/tn", name, "/f"])
        r = run(["schtasks", "/create", "/tn", name, "/xml", str(tmp)])
        tmp.unlink(missing_ok=True)
        ok = r.returncode == 0
        print(f"{name}: {r.stderr.strip() if not ok else r.stdout.strip()}")
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
