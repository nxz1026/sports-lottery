"""Stage A3 结构断言：dashboard.html 应包含所有 P0-A3 新增要素。"""
import re
import sys
from pathlib import Path

html = Path("static/dashboard.html").read_text(encoding="utf-8")

checks = [
    ("bjtTodayStr function", r"function\s+bjtTodayStr\s*\(\s*\)\s*\{"),
    ("bjtTodayStr called in renderMatches", r"const\s+todayStr\s*=\s*bjtTodayStr\s*\(\s*\)"),
    ("isPlayed lambda", r"const\s+isPlayed\s*=\s*p\s*=>"),
    ("playedCards filter", r"const\s+playedCards\s*=\s*cards\.filter\s*\(\s*isPlayed"),
    ("upcomingCards filter", r"const\s+upcomingCards\s*=\s*cards\.filter\s*\(\s*p\s*=>\s*!\s*isPlayed"),
    ("section-h3 class in CSS", r"\.section-h3"),
    (".played class in CSS", r"\.match\.played"),
    (".badge-status in CSS", r"\.badge-status"),
    ("今日推荐 header in template", r"今日推荐\s*·\s*\$\{upcomingCards\.length\}"),
    ("今日已完赛 header in template", r"今日已完赛"),
    ("played-h3 class in CSS", r"\.played-h3"),
]
fail = []
for name, pat in checks:
    if not re.search(pat, html):
        fail.append(name)
        print(f"FAIL: {name}")
    else:
        print(f"OK  : {name}")
if fail:
    sys.exit(1)
print("\nall checks passed")