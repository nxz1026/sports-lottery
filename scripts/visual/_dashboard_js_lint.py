"""一次性 lint 脚本：从 dashboard.html 抽取 <script> 内容并跑 node --check。"""
from pathlib import Path
import re
import subprocess
import sys

html = Path("static/dashboard.html").read_text(encoding="utf-8")
# 简单非贪婪匹配全部 <script> 块（非外部 src 引用）
blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", html)
print(f"found {len(blocks)} inline <script> blocks")
combined = "\n;\n".join(blocks)
import os
out = Path(os.environ.get("TEMP", ".")) / "dashboard.js"
out.write_text(combined, encoding="utf-8")
r = subprocess.run(["node", "--check", str(out)], capture_output=True, text=True)
if r.returncode != 0:
    print("STDERR:", r.stderr)
    sys.exit(1)
print("OK JS syntax check passed")