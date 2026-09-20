"""Playwright 验证 B 改动。 通过 route() 把 /dashboard/jc/api/ 重写到 /api/。"""
import json, http.cookiejar, urllib.request
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8077"
DASH = BASE + "/static/dashboard.html"
API = BASE + "/api/v1"

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
opener.open(urllib.request.Request(API + "/login", data=json.dumps({"username": "a", "password": "a"}).encode(), headers={"Content-Type": "application/json"}, method="POST"))
cookies = [{"name": c.name, "value": c.value, "url": BASE} for c in cj]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_cookies(cookies)
    page = ctx.new_page()
    console_errors = []

    # 把 /dashboard/jc/api/* 重写到 /api/*
    def rewrite(route):
        url = route.request.url
        if "/dashboard/jc/api/" in url:
            new = url.replace("/dashboard/jc/api/", "/api/")
            route.continue_(url=new)
        else:
            route.continue_()
    page.route("**/*", rewrite)

    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

    page.goto(DASH, wait_until="networkidle")
    page.wait_for_selector("#matchGrid article.match", timeout=20000)
    initial_n = page.locator("#matchGrid article.match").count()
    print(f"[B] 初始卡片数 = {initial_n}")
    assert initial_n > 0, "无今日卡片可测试"

    page.evaluate("localStorage.removeItem('jc_dashboard_follow_v1')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#matchGrid article.match", timeout=20000)

    fs0 = page.locator("#followSummary").inner_text()
    print(f"[B] followSummary 初始 = {fs0!r}")
    assert "未设置" in fs0 or "本地偏好未设置" in fs0

    first_card = page.locator("#matchGrid article.match").first
    home_team = first_card.locator(".team").first.inner_text().strip()
    print(f"[B] 首场 = {home_team}")
    first_card.locator('[data-action="follow-match"]').click()
    page.wait_for_timeout(500)
    fs1 = page.locator("#followSummary").inner_text()
    print(f"[B] 关注后 followSummary = {fs1!r}")
    assert "联赛 1" in fs1 or "球队" in fs1
    btn_text = first_card.locator('[data-action="follow-match"]').inner_text()
    print(f"[B] 关注按钮 = {btn_text!r}")
    assert "关注中" in btn_text

    page.locator('[data-action="personalize"]').click()
    page.wait_for_timeout(500)
    fs2 = page.locator("#followSummary").inner_text()
    print(f"[B] 启用个性化 = {fs2!r}")
    assert "已生效" in fs2
    filtered_n = page.locator("#matchGrid article.match").count()
    print(f"[B] 个性化卡片数 = {filtered_n}")
    assert 1 <= filtered_n <= initial_n

    fsum = page.locator("#filterSummary").inner_text()
    print(f"[B] filterSummary = {fsum!r}")
    assert "个性化" in fsum

    page.locator('[data-action="personalize"]').click()
    page.wait_for_timeout(500)
    after_off = page.locator("#matchGrid article.match").count()
    print(f"[B] 关闭后 = {after_off}")
    assert after_off == initial_n

    if console_errors:
        print(f"[B] console err:\n" + "\n".join(console_errors[:5]))
    assert not console_errors, f"{len(console_errors)} console errors"

    print("\n[B] PASS：关注 chip 现在真正生效")
    browser.close()
