# tests/web_ui — Playwright browser smoke tests

This directory holds **browser-level** tests that drive `static/dashboard.html`
through a headless Chromium via [Playwright]. They catch DOM regressions that
plain pytest + TestClient cannot (e.g. a JS error that breaks the page,
a view-switch that doesn't fire, a button that's clickable when it shouldn't be).

## Scope

These tests are deliberately **backend-independent**. They:

- serve `static/` on a session-scoped local HTTP server (random port),
- stub every `/api/**` response so the dashboard loads offline,
- drive the page exactly the way a user would (clicks, navigation, assertions on
  visible text), and
- exit cleanly with a `skip` reason if Playwright is not installed so the rest
  of the suite still passes on dev boxes without browsers.

## Install

```bash
pip install playwright pytest-playwright
playwright install chromium
```

`pytest-playwright` is optional; the bundled `conftest.py` only needs the core
`playwright` package. The Chromium download is ~170 MB and is required only
for the first install.

## Run

```bash
# only this directory
pytest tests/web_ui/ -v

# with marker filter
pytest tests/web_ui/ -v -m web_ui

# with pytest-playwright auto-fixtures (--browser chromium is implicit if installed)
pytest tests/web_ui/ -v --browser chromium
```

If Playwright isn't installed, you'll see:

```
tests/web_ui/test_dashboard_browser.py ssssssss ssssssss sss
```

All tests `skip` cleanly — the rest of the suite continues normally.

## Layout

| File                                  | Purpose                                                         |
|---------------------------------------|-----------------------------------------------------------------|
| `conftest.py`                         | `dashboard_url`, `browser`, `page` fixtures + skip gate         |
| `test_dashboard_browser.py`           | 9 smoke tests covering h1, KPIs, view switch, chip toggle, …    |
| `__init__.py`                         | makes the directory a Python package                            |

## When to add a test here

Add a browser test when the regression you're guarding against lives in
`static/dashboard.html`'s JS / DOM, not in `web/routers/*` or
`scripts/core/*`. Example candidates: race between `state.all` fetch and
the lazy-render sentinel; chip-grouping when both football and basketball
leagues are present; live-text reconnect/backoff visual state.

For HTTP API behaviour (auth, status codes, JSON shape) keep using the
plain `TestClient` tests under `tests/web/` — they are faster and don't need
a browser binary.