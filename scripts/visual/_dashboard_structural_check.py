"""Stage A3 结构断言：dashboard.html 应包含所有 P0-A3 新增要素。

⚠️ **P0 订正（2026-10-23）**：这张表里有 4 条断言锁的是**旧文案**。P0 把卡片角标
   「方向概率 / 主胜率 / 模型未给」换成「**这条的赢面（模型概率）** / **模型把握度（非赢面）** /
   **还没算出来**」之后，它们**当场变红**（守门断言自己在说谎）。
   ⇒ 按纪律「补能力时必须把所有『说它不存在』的地方一起翻过来（**包括守门断言本身**）」
   ⇒ 这里同步订正，并**加严**：旧文案**不许**回来（它正是「把握度冒充赢面」的载体）。
"""
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
    ("今日推荐 header in template", r"今日推荐\s*<span[^>]*>\s*\$\{upcomingCards\.length\}"),
    ("今日已完赛 header in template", r"今日已完赛"),
    ("played-h3 class in CSS", r"\.played-h3"),
    ("P1-B5 lazy chunk size", r"const\s+CHUNK\s*=\s*\d+"),
    ("P1-B5 lazy-sentinel CSS", r"\.lazy-sentinel"),
    ("P1-B5 IntersectionObserver", r"IntersectionObserver"),
    ("P1-B5 observer disconnect guard", r"state\._lazyObserver.*disconnect"),
    ("P0 赢面角标 = 这条的赢面（模型概率）", r"<small>这条的赢面（模型概率）</small>"),
    ("P0 把握度角标 = 模型把握度（非赢面）", r"<small>模型把握度（非赢面）</small>"),
    ("P0 缺值兜底 = 还没算出来", r"<small>还没算出来</small>"),
    ("P1-A5 .odds-na CSS class", r"\.odds\.odds-na"),
    ("P1-A9 closeHint function", r"function\s+closeHint"),
    ("P1-A9 .close-hint CSS class", r"\.close-hint"),
    ("P1-A9 closeHint called in renderCard", r"\$\{closeHint\(p,dir\)\}"),
]
fail = []
for name, pat in checks:
    if not re.search(pat, html):
        fail.append(name)
        print(f"FAIL: {name}")
    else:
        print(f"OK  : {name}")
# 负向断言：P0 之前那套错文案**不许**回来（它们正是「把握度冒充赢面」的载体）。
# ⚠️ 纪律：补能力时必须把所有「说它不存在」的地方一起翻过来（**包括守门断言本身**）。
for name, pat in (
    ("旧角标「方向概率」不许回来（P0 之前把把握度当赢面）", r"<small>方向概率</small>"),
    ("旧角标「主胜率」不许回来", r"<small>主胜率</small>"),
    ("旧兜底「模型未给」不许回来", r"<small>模型未给</small>"),
):
    if re.search(pat, html):
        fail.append(name)
        print(f"FAIL: {name}")
    else:
        print(f"OK  : {name}")
if fail:
    sys.exit(1)
print("\nall checks passed")
