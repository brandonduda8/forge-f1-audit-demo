"""Unit tests: network guard, URL handling, honest scoring, runner modes."""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.path.dirname(HERE)
REPO = os.path.abspath(os.path.join(API, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, API)
sys.path.insert(0, os.path.join(REPO, "audit-demo"))

import netguard  # noqa: E402
import runner  # noqa: E402
from helpers import serve_site  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_sockets():
    yield
    netguard.uninstall()   # install() patches this test process; undo it for the next test


def run_runner(mode, url, loopback=True):
    env = {"PATH": os.environ["PATH"], "HTTPS_PROXY": "http://10.9.9.9:1", "HTTP_PROXY": "http://10.9.9.9:1"}
    if loopback:
        env["SELFSERVE_TEST_ALLOW_LOOPBACK"] = "1"
    p = subprocess.run([sys.executable, "-I", os.path.join(API, "runner.py"), mode, url],
                       capture_output=True, text=True, timeout=120, env=env)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254",
                                "100.64.0.1", "0.0.0.0", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1",
                                "224.0.0.1", "240.0.0.1"])
def test_non_public_addresses_refused(ip, monkeypatch):
    monkeypatch.delenv("SELFSERVE_TEST_ALLOW_LOOPBACK", raising=False)
    assert not netguard.address_allowed(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_allowed(ip):
    assert netguard.address_allowed(ip)


def test_connect_to_loopback_blocked_without_test_flag(monkeypatch):
    monkeypatch.delenv("SELFSERVE_TEST_ALLOW_LOOPBACK", raising=False)
    with pytest.raises(netguard.Blocked):
        netguard.guarded_create_connection(("127.0.0.1", 80), timeout=2)
    with pytest.raises(netguard.Blocked):
        netguard.guarded_create_connection(("8.8.8.8", 22), timeout=2)


def test_redirect_to_metadata_address_blocked(monkeypatch):
    monkeypatch.setenv("SELFSERVE_TEST_ALLOW_LOOPBACK", "1")  # allows 127.0.0.1 only, not 169.254/16
    netguard.install()
    srv, base = serve_site("example-roofing")
    c = netguard.GuardedRequests(5, 20)
    with pytest.raises(netguard.Blocked):
        c.get(base + "/redirect-private")
    assert "blocked" in c.events
    srv.shutdown()


def test_body_cap_and_request_budget(monkeypatch):
    monkeypatch.setenv("SELFSERVE_TEST_ALLOW_LOOPBACK", "1")
    netguard.install()
    srv, base = serve_site("example-roofing")
    c = netguard.GuardedRequests(2, 20)
    with pytest.raises(netguard.TooLarge):
        c.get(base + "/huge")
    assert c.get(base + "/index.html").status_code == 200
    with pytest.raises(netguard.BudgetExceeded):
        c.get(base + "/index.html")
    srv.shutdown()


@pytest.mark.parametrize("raw,want", [
    ("example.com", "https://example.com/"),
    ("  Example.COM/page ", "https://example.com/page"),
    ("http://example.com", "http://example.com/"),
    ("https://example.com:443/x?y=1", "https://example.com:443/x?y=1"),
    ("example.com:443/x", "https://example.com:443/x"),
    ("HTTPS://Example.com", "https://example.com/"),
])
def test_normalize_url_ok(raw, want, monkeypatch):
    monkeypatch.delenv("SELFSERVE_TEST_ALLOW_LOOPBACK", raising=False)
    assert runner.normalize_url(raw) == want


@pytest.mark.parametrize("raw", ["", "ftp://example.com", "javascript:alert(1)", "localhost",
                                 "https://user:pw@example.com", "https://example.com:8080/",
                                 "exa mple.com", "x" * 400, "file:///etc/passwd"])
def test_normalize_url_rejects(raw, monkeypatch):
    monkeypatch.delenv("SELFSERVE_TEST_ALLOW_LOOPBACK", raising=False)
    with pytest.raises(ValueError):
        runner.normalize_url(raw)


def test_unmeasured_category_is_not_scored_zero():
    import audit_pipeline as pipe
    ev = {"checks": {"2.1": {"value": True, "method": "m"}, "2.4": {"value": ["tel:1"], "method": "m"}}}
    sc = pipe.score(ev)
    assert sc["categories"]["Mobile"]["score"] == 100
    assert sc["categories"]["Speed"]["score"] is None   # nothing measured: no invented 0
    assert sc["total"] == 100 and sc["grade"] == "A"


def test_sanitize_turns_caps_into_not_measured():
    ev = {"checks": {
        "1.5": {"value": {"est_total_bytes": 1}, "method": "m"},
        "6.6": {"value": {"checked": 3, "broken": [{"url": "a", "status": 404},
                                                     {"url": "b", "status": "ERR BUDGET time limit reached"},
                                                     {"url": "c", "status": "ERR BLOCKED x"}]}, "method": "m"},
        "2.1": {"value": True, "method": "m"}}}
    out = runner.sanitize_evidence(ev, ["budget:time"], "full")
    assert out["checks"]["1.5"]["status"] == "NOT_MEASURED"
    assert out["checks"]["6.6"]["status"] == "NOT_MEASURED"
    assert out["checks"]["2.1"]["value"] is True
    ev2 = {"checks": {"6.6": {"value": {"checked": 3, "broken": [{"url": "a", "status": 404},
                                                                   {"url": "c", "status": "ERR BLOCKED x"}]}, "method": "m"}}}
    out2 = runner.sanitize_evidence(ev2, ["blocked"], "full")
    assert out2["checks"]["6.6"]["value"]["broken"] == [{"url": "a", "status": 404}]


def test_light_run_makes_exactly_three_requests_and_ignores_proxy_env():
    srv, base = serve_site("sample-plumbing")
    res = run_runner("light", base + "/")
    srv.shutdown()
    assert res["ok"] and res["requests_made"] == 3
    assert 1 <= len(res["top"]) <= 3
    assert set(res) >= {"score", "grade", "failing", "passed", "not_measured"}
    assert "report" not in res and "markdown" not in res     # free check gives no full report


def test_full_run_report_is_selfserve_and_honest():
    srv, base = serve_site("example-roofing")
    res = run_runner("full", base + "/")
    srv.shutdown()
    assert res["ok"] and res["requests_made"] <= 90
    md = res["markdown"]
    for banned in ("Reply to this email", "$150", "$300", "Walkthrough", "refund", "Brandon"):
        assert banned not in md
    rep = res["report"]
    fail_titles = {f["title"] for f in rep["findings"]}
    assert not fail_titles & {p["title"] for p in rep["passed"]}
    ids = [f["id"] for f in rep["findings"]] + [p["id"] for p in rep["passed"]] + [n["id"] for n in rep["not_measured"]]
    assert len(ids) == len(set(ids))
    sev = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}
    ranks = [sev[f["severity"]] for f in rep["findings"]]
    assert ranks == sorted(ranks)
    for f in rep["findings"]:
        assert f["measured"] and f["fix"] and f["effort"] and f["method"]


def test_loopback_target_refused_in_production_mode():
    srv, base = serve_site("example-roofing")
    res = run_runner("light", base + "/", loopback=False)
    srv.shutdown()
    assert not res["ok"]   # port is not 80/443, and 127.0.0.1 is not public
    assert res.get("input_error") or res.get("fatal")


@pytest.mark.parametrize("path,why", [("/forbidden", "HTTP 403"), ("/challenge", "bot-check")])
def test_error_or_bot_check_homepage_is_fatal_not_graded(path, why):
    srv, base = serve_site("example-roofing")
    res = run_runner("light", base + path)
    srv.shutdown()
    assert res["ok"] is False and res["fatal"] is True and "score" not in res
    assert why in res["error"]


def test_slow_drip_homepage_stops_at_deadline():
    srv, base = serve_site("example-roofing")
    import time
    env = {"PATH": os.environ["PATH"], "SELFSERVE_TEST_ALLOW_LOOPBACK": "1", "RUNNER_TEST_DEADLINE": "3"}
    t0 = time.time()
    p = subprocess.run([sys.executable, "-I", os.path.join(API, "runner.py"), "light", base + "/slow"],
                       capture_output=True, text=True, timeout=60, env=env)
    took = time.time() - t0
    srv.shutdown()
    res = json.loads(p.stdout.strip().splitlines()[-1])
    assert res["ok"] is False and res["fatal"] and "too long" in res["error"]
    assert took < 20


def test_https_without_cert_inspection_is_not_measured_in_full_mode():
    ev = {"checks": {"3.7": {"value": {"https": True, "cert": {"valid": False, "error": "x"}}, "method": "m"},
                     "3.8": {"value": {"robots": 200, "sitemap_xml": None}, "method": "m"}}}
    out = runner.sanitize_evidence(ev, [], "full")
    assert out["checks"]["3.7"]["status"] == "NOT_MEASURED"
    assert out["checks"]["3.8"]["status"] == "NOT_MEASURED"


def test_runner_sets_a_memory_ceiling():
    assert 100 * 1024 * 1024 <= runner.MEM_LIMIT_BYTES <= 400 * 1024 * 1024


def test_unreachable_site_is_fatal_not_scored():
    res = run_runner("light", "http://127.0.0.1:9/")
    assert res["ok"] is False and res["fatal"] is True and "score" not in res


def test_square_webhook_signature(monkeypatch):
    import base64
    import hashlib
    import hmac
    import square_api
    monkeypatch.setenv("SQUARE_WEBHOOK_SIGNATURE_KEY", "k1")
    monkeypatch.setenv("SQUARE_WEBHOOK_URL", "https://api.example/api/square/webhook")
    body = b'{"type":"payment.updated"}'
    good = base64.b64encode(hmac.new(b"k1", b"https://api.example/api/square/webhook" + body, hashlib.sha256).digest()).decode()
    assert square_api.verify_webhook(body, good)
    assert not square_api.verify_webhook(body + b" ", good)
    assert not square_api.verify_webhook(body, "")
    assert not square_api.verify_webhook(body, good[:-2] + "AA")
    monkeypatch.delenv("SQUARE_WEBHOOK_SIGNATURE_KEY")
    assert not square_api.verify_webhook(body, good)          # no key configured: refuse everything


def test_square_event_and_checkout_host_checks(monkeypatch):
    import square_api
    monkeypatch.delenv("SELFSERVE_TEST_ALLOW_LOOPBACK", raising=False)
    ev = {"type": "payment.updated", "data": {"object": {"payment": {"order_id": "ORD" + "A" * 22, "status": "COMPLETED"}}}}
    assert square_api.paid_order_id_from_event(ev) == "ORD" + "A" * 22
    ev["data"]["object"]["payment"]["status"] = "APPROVED"
    assert square_api.paid_order_id_from_event(ev) is None
    assert square_api.paid_order_id_from_event({"type": "refund.updated"}) is None
    assert square_api.paid_order_id_from_event("junk") is None
    ok = square_api.checkout_url_ok
    assert ok("https://square.link/u/AbC123") and ok("https://checkout.square.site/merchant/x/checkout/Y")
    assert ok("https://sandbox.square.link/u/x")
    for bad in ("http://square.link/u/x", "https://square.link.evil.example/u/x", "https://evilsquare.link/x",
                "https://127.0.0.1/x", "javascript:alert(1)"):
        assert not ok(bad), bad


def test_square_api_base_must_be_https(monkeypatch):
    import square_api
    monkeypatch.delenv("SELFSERVE_TEST_ALLOW_LOOPBACK", raising=False)
    for k, v in {"SQUARE_ACCESS_TOKEN": "t", "SQUARE_LOCATION_ID": "L1", "SQUARE_ENV": "sandbox",
                 "SQUARE_API_BASE": "http://example.test"}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(RuntimeError):
        square_api.get_paid_order("ORD" + "A" * 22)
    monkeypatch.delenv("SQUARE_API_BASE")
    monkeypatch.setenv("SQUARE_ENV", "staging")
    with pytest.raises(square_api.SquareUnavailable):        # unknown environment: not configured
        square_api.get_paid_order("ORD" + "A" * 22)


def test_square_price_is_clamped(monkeypatch):
    import square_api
    for raw, want in (("2900", 2900), ("100", 100), ("99", 2900), ("abc", 2900), ("100001", 2900)):
        monkeypatch.setenv("CHECKUP_PRICE_CENTS", raw)
        assert square_api.price_cents() == want
