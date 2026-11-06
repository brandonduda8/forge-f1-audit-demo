"""Offline harness for the Forge F1 audit code.

* Puts f1_measure.py, render_pdf.py and audit_pipeline.py on sys.path.
* Blocks real network access for every test (socket connect raises).
* `fake_web` replaces requests.get/requests.head inside f1_measure with a
  dict-backed transport serving recorded/hand-written HTML fixtures.
"""
import datetime
import os
import socket
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
for sub in ("f1-audit-product/tools", "audit-demo"):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise OSError("network disabled in tests")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def fixture_text(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class FakeResponse:
    def __init__(self, url, status=200, body="", headers=None, elapsed_ms=250):
        self.url = url
        self.status_code = status
        self.text = body
        self.content = body.encode("utf-8")
        self.headers = headers or {}
        self.elapsed = datetime.timedelta(milliseconds=elapsed_ms)


class FakeWeb:
    """routes: {url: {"status":..,"body":..,"headers":..,"elapsed_ms":..}}.
    Unknown URLs raise ConnectionError, like an unreachable host."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _resp(self, method, url):
        self.calls.append((method, url))
        r = self.routes.get(url)
        if r is None:
            import requests
            raise requests.ConnectionError("no fixture for %s" % url)
        body = r.get("body", "") if method == "GET" else ""
        return FakeResponse(url, r.get("status", 200), body, r.get("headers"),
                            r.get("elapsed_ms", 250))

    def get(self, url, **kw):
        return self._resp("GET", url)

    def head(self, url, **kw):
        return self._resp("HEAD", url)


@pytest.fixture
def fake_web(monkeypatch):
    import f1_measure

    def install(routes, cert=None):
        web = FakeWeb(routes)
        monkeypatch.setattr(f1_measure.requests, "get", web.get)
        monkeypatch.setattr(f1_measure.requests, "head", web.head)
        monkeypatch.setattr(f1_measure, "cert_info",
                            lambda host: cert if cert is not None else
                            {"valid": True, "expires": "2027-01-01T00:00:00+00:00",
                             "sans": [host], "host_in_san": True})
        return web
    return install


ACME = "https://acme-plumbing.test"


def acme_routes():
    return {
        ACME + "/": {"body": fixture_text("acme-home.html"), "elapsed_ms": 420},
        ACME + "/services": {"body": fixture_text("acme-services.html")},
        ACME + "/about": {"body": fixture_text("acme-about.html")},
        ACME + "/contact": {"body": fixture_text("acme-contact.html")},
        ACME + "/old-page": {"status": 404, "body": "Not found"},
        ACME + "/img/team.webp": {"headers": {"Content-Length": "120000"}},
        ACME + "/img/hero.jpg": {"headers": {"Content-Length": "700000"}},
        ACME + "/js/app.js": {"headers": {"Content-Length": "50000"}},
        ACME + "/js/analytics.js": {"headers": {"Content-Length": "10000"}},
        ACME + "/css/site.css": {"headers": {"Content-Length": "20000"}},
        ACME + "/css/print.css": {"headers": {}},  # no Content-Length -> counted as 0
        ACME + "/robots.txt": {"body": "User-agent: *"},
        ACME + "/sitemap.xml": {"status": 404},
        ACME + "/sitemap_index.xml": {"body": "<sitemapindex/>"},
    }


EXAMPLE = "https://example.com"


def example_routes():
    return {
        EXAMPLE + "/": {"body": fixture_text("example-com-home.html"), "elapsed_ms": 95},
        EXAMPLE + "/s.js": {"headers": {"Content-Length": "1200"}},
        EXAMPLE + "/robots.txt": {"status": 404},
        EXAMPLE + "/sitemap.xml": {"status": 404},
        EXAMPLE + "/sitemap_index.xml": {"status": 404},
    }
