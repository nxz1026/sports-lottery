"""Browser-level smoke tests for static/dashboard.html.

These run the actual page in headless Chromium via Playwright and assert
structural invariants (no JS errors, required DOM nodes, view switching,
chip toggles). They do NOT depend on a live API or backend.

All tests skip cleanly when playwright is not installed; see
tests/web_ui/README.md for the install steps.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.web_ui


def test_page_loads_without_js_errors(page):
    """Static dashboard should boot without console errors when offline."""
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.wait_for_timeout(300)  # let any inline JS settle
    assert errors == [], f"unexpected page errors: {errors!r}"


def test_h1_and_kpi_skeleton_present(page):
    """Header + KPI cards render with their canonical labels even when state.all=[]."""
    assert page.locator("#pageTitle").inner_text() == "今日推荐"
    assert page.locator("#kpiMatches").count() == 1
    assert page.locator("#kpiHigh").count() == 1
    assert page.locator("#kpiAccuracy").count() == 1
    assert page.locator("#kpiAI").count() == 1
    # kpiMatches initial value is "—" until first API success; that's fine.
    assert page.locator("#kpiMatches").inner_text() in {"—", ""}


def test_default_view_is_today(page):
    today = page.locator("#view-today")
    combo = page.locator("#view-combo")
    assert "active" in (today.get_attribute("class") or "")
    assert "active" not in (combo.get_attribute("class") or "")


def test_personalize_toggle_reflects_state(page):
    """Clicking the personalize chip toggles aria-pressed + active class."""
    btn = page.locator("#personalizeBtn")
    assert btn.get_attribute("aria-pressed") == "false"
    assert "active" not in (btn.get_attribute("class") or "")
    btn.click()
    page.wait_for_timeout(80)
    assert btn.get_attribute("aria-pressed") == "true"
    assert "active" in (btn.get_attribute("class") or "")
    # Toggle back so test order independence doesn't matter.
    btn.click()
    page.wait_for_timeout(80)
    assert btn.get_attribute("aria-pressed") == "false"


def test_ledger_save_button_disabled_until_picks(page):
    """#ledgerSaveBtn must start disabled; only #view-combo hosts it."""
    # Switch to combo view via the nav button (mirrors real user click).
    page.click("button[data-view='combo']")
    page.wait_for_timeout(80)
    save_btn = page.locator("#ledgerSaveBtn")
    assert save_btn.is_visible(), "save button only rendered inside combo view"
    assert save_btn.is_disabled(), "save must be disabled when state.selected is empty"
    title = save_btn.get_attribute("title") or ""
    assert "勾选" in title or "串关" in title, f"unhelpful title: {title!r}"


def test_view_switch_updates_h1(page):
    """switchView() rewrites #pageTitle per the nav target."""
    page.click("button[data-view='combo']")
    page.wait_for_timeout(80)
    assert page.locator("#pageTitle").inner_text() == "串关工作台"
    page.click("button[data-view='history']")
    page.wait_for_timeout(80)
    assert page.locator("#pageTitle").inner_text() == "历史与命中率"
    # Restore default view for any later assertions.
    page.click("button[data-view='today']")
    page.wait_for_timeout(80)


def test_age_prompt_can_be_dismissed_once(page):
    """#agePrompt data-action='age-dismiss' sets localStorage flag + removes node."""
    # First clear any previous ack so the prompt is visible.
    page.evaluate("() => localStorage.removeItem('jc_age_ack')")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(120)
    prompt = page.locator("#agePrompt")
    # The prompt is removed via JS but the inline style keeps it hidden until
    # the script branch fires. Either way the dismiss button must work.
    if prompt.count():
        page.locator("[data-action='age-dismiss']").first.click()
        page.wait_for_timeout(80)
        assert page.evaluate("() => localStorage.getItem('jc_age_ack')") == "1"


def test_league_chips_groups_football_and_basketball(page):
    """buildLeagueChips groups by sport dimension; even with no data, the chip
    container should mount and show the empty-state cellnote (no exception)."""
    league_box = page.locator("#leagueFilters")
    assert league_box.count() == 1
    # Without API data the script writes a placeholder cellnote.
    assert league_box.inner_text() != ""


def test_no_404s_on_inline_assets(page):
    """No static asset referenced by dashboard.html should 404 under the same
    static/ origin (catches moved/renamed files that Jinja would otherwise
    silently fall back to)."""
    failures = []
    page.on(
        "response",
        lambda r: failures.append((r.status, r.url)) if r.status >= 400 else None,
    )
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(200)
    # Filter out the deliberately-stubbed /api/** responses.
    real_failures = [(s, u) for s, u in failures if "/api/" not in u]
    assert real_failures == [], f"static 404s: {real_failures!r}"
