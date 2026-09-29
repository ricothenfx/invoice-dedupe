"""DB-free smoke tests for the Phase 3 web app: assets served, index reachable.

Uses ``TestClient`` without a context manager so the app lifespan (which needs
a database) never runs — these tests only exercise static serving.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from invoice_dedupe.api import app

client = TestClient(app)

ASSETS = [
    ("/static/app.js", "invoice-dedupe review app"),
    ("/static/styles.css", "pair-card"),
    ("/static/vendor/react.production.min.js", "React"),
    ("/static/vendor/react-dom.production.min.js", "ReactDOM"),
]


def test_index_served():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert '<div id="root"></div>' in resp.text
    # offline requirement: assets must be served locally, never from a CDN
    assert "cdn" not in resp.text.lower()
    assert "unpkg" not in resp.text.lower()
    assert "googleapis" not in resp.text.lower()


def test_static_assets_served():
    for path, marker in ASSETS:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert marker in resp.text, path


def test_theme_support():
    # index.html must bootstrap the theme before first paint (no flash)
    resp = client.get("/")
    assert "data-theme" in resp.text
    assert "localStorage" in resp.text
    # both palettes must exist in the stylesheet
    css = client.get("/static/styles.css")
    assert css.status_code == 200
    assert ":root[data-theme=\"light\"]" in css.text
