"""End to end: the real server process, a fake Stripe, and local fixture sites.

Flow tested: free check -> (buyer pays on Stripe) -> report page asks for the
report with the session id -> report + PDF; plus every refusal path and the
automatic refund for a site that cannot be reached.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from helpers import FakeStripe, serve_site  # noqa: E402

ORIGIN = "https://checkup.example"
LINK = "plink_test_checkup"
GOOD, UNPAID, OTHER_LINK, OLD, DOWN, LIVE_ON_TEST, NOSITE = (
    "cs_test_" + x * 24 for x in "abcdefg")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def call(base, path, body=None, origin=ORIGIN, timeout=150):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    if origin:
        req.add_header("Origin", origin)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def jcall(*a, **k):
    code, headers, body = call(*a, **k)
    return code, headers, json.loads(body)


@pytest.fixture(scope="module")
def stack():
    stripe = FakeStripe()
    roof, roof_url = serve_site("example-roofing")
    plumb, plumb_url = serve_site("sample-plumbing")
    stripe.add(GOOD, roof_url + "/")
    stripe.add(UNPAID, roof_url + "/", paid=False)
    stripe.add(OTHER_LINK, roof_url + "/", link="plink_test_someone_else")
    stripe.add(OLD, roof_url + "/", created=time.time() - 31 * 24 * 3600)
    stripe.add(DOWN, "http://127.0.0.1:9/", pi="pi_test_down")
    stripe.add(LIVE_ON_TEST, roof_url + "/", live=True)
    stripe.add(NOSITE, "")
    port = free_port()
    env = {"PATH": os.environ["PATH"], "PORT": str(port), "BIND": "127.0.0.1",
           "SITE_ORIGIN": ORIGIN, "STRIPE_SECRET_KEY": "rk_test_fake_key_for_tests",
           "STRIPE_PAYMENT_LINK_ID": LINK, "STRIPE_API_BASE": stripe.base,
           "SELFSERVE_TEST_ALLOW_LOOPBACK": "1", "LIMIT_FREE_PER_SITE": "50",
           "LIMIT_FREE_PER_IP": "4"}
    proc = subprocess.Popen([sys.executable, "-I", os.path.join(API, "server.py")], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if call(base, "/health")[0] == 200:
                break
        except Exception:
            time.sleep(0.1)
    yield {"base": base, "stripe": stripe, "plumb": plumb_url, "roof": roof_url}
    proc.terminate()
    out, err = proc.communicate(timeout=10)
    # session ids must never be written to the server log
    for sid in (GOOD, DOWN, UNPAID):
        assert sid not in out + err
    for s in (roof, plumb):
        s.shutdown()


def test_health_and_cors(stack):
    code, h, body = jcall(stack["base"], "/health")
    assert code == 200 and body["ok"]
    assert h.get("Access-Control-Allow-Origin") == ORIGIN
    code, h, _ = jcall(stack["base"], "/health", origin="https://evil.example")
    assert "Access-Control-Allow-Origin" not in h


def test_free_check_returns_teaser_only(stack):
    code, _, body = jcall(stack["base"], "/api/check", {"url": stack["plumb"] + "/"})
    assert code == 200 and body["ok"]
    assert body["grade"] in "ABCDF" and 0 <= body["score"] <= 100
    assert 1 <= len(body["top"]) <= 3 and set(body["top"][0]) == {"severity", "title"}
    assert "report" not in body and "markdown" not in body and "url" not in body
    stack["mini_json"] = body


def test_free_check_bad_input(stack):
    code, _, body = jcall(stack["base"], "/api/check", {"url": "javascript:alert(1)"})
    assert code == 400 and not body["ok"]
    code, _, body = jcall(stack["base"], "/api/check", {"url": "http://127.0.0.1:9/"})
    assert code == 422 and "couldn't load" in body["error"]


def test_free_check_rate_limited_per_ip(stack):
    # LIMIT_FREE_PER_IP=4 and three checks already ran in the tests above
    codes = [jcall(stack["base"], "/api/check", {"url": "nope"})[0] for _ in range(2)]
    assert codes[-1] == 429


def test_paid_report_end_to_end(stack):
    t0 = time.time()
    code, _, body = jcall(stack["base"], "/api/report?session_id=" + GOOD)
    first = time.time() - t0
    assert code == 200 and body["ok"], body
    rep = body["report"]
    assert {c["name"] for c in rep["categories"]} == {"Speed", "Mobile", "SEO basics", "Contact path", "Reviews", "Trust signals"}
    assert rep["findings"] and rep["passed"] and rep["not_measured"]
    assert body["pdf_url"] == "/api/report.pdf?session_id=" + GOOD
    assert "markdown" not in body
    t0 = time.time()
    code, _, again = jcall(stack["base"], "/api/report?session_id=" + GOOD)
    assert code == 200 and again["report"] == rep and time.time() - t0 < first + 1  # served from cache
    stack["report_json"] = body
    code, h, pdf = call(stack["base"], body["pdf_url"])
    assert code == 200 and h["Content-Type"] == "application/pdf" and pdf[:5] == b"%PDF-" and len(pdf) > 5000
    assert "attachment" in h["Content-Disposition"]


@pytest.mark.parametrize("sid,code", [
    (UNPAID, 402), (OTHER_LINK, 402), (OLD, 402), (LIVE_ON_TEST, 402), (NOSITE, 402),
    ("cs_test_" + "z" * 24, 402), ("cs_live_" + "a" * 24, 402), ("not-a-session", 402)])
def test_report_refusals(stack, sid, code):
    c, _, body = jcall(stack["base"], "/api/report?session_id=" + sid)
    assert c == code and not body["ok"] and body["error"]


def test_unreachable_site_offers_and_issues_one_refund(stack):
    code, _, body = jcall(stack["base"], "/api/report?session_id=" + DOWN)
    assert code == 422 and body["refundable"] is True
    code, _, r1 = jcall(stack["base"], "/api/refund", {"session_id": DOWN})
    code2, _, r2 = jcall(stack["base"], "/api/refund", {"session_id": DOWN})
    assert code == 200 and r1 == {"ok": True, "status": "succeeded"}
    assert code2 == 200 and r2["ok"]
    refunds = stack["stripe"].refunds
    assert len({h.get("Idempotency-Key") for h, _ in refunds}) == 1          # one refund, replayed
    assert all(f["payment_intent"] == ["pi_test_down"] for _, f in refunds)
    assert stack["stripe"].auth_seen == {"Bearer rk_test_fake_key_for_tests"}


def test_no_refund_after_delivered_report(stack):
    code, _, body = jcall(stack["base"], "/api/refund", {"session_id": GOOD})
    assert code == 409 and not body["ok"]
    assert all(f["payment_intent"] != ["pi_test_1"] for _, f in stack["stripe"].refunds)


def test_unknown_routes_and_bad_bodies(stack):
    assert jcall(stack["base"], "/admin")[0] == 404
    assert call(stack["base"], "/api/check", body=None)[0] == 404   # GET on a POST route
    code, _, _ = call(stack["base"], "/api/refund", {"session_id": "x" * 5000})
    assert code == 400


def test_refuses_to_start_with_live_key_in_test_mode():
    env = {"PATH": os.environ["PATH"], "PORT": str(free_port()), "BIND": "127.0.0.1",
           "STRIPE_SECRET_KEY": "rk_live_x", "SELFSERVE_TEST_ALLOW_LOOPBACK": "1"}
    p = subprocess.run([sys.executable, "-I", os.path.join(API, "server.py")], env=env,
                       capture_output=True, text=True, timeout=20)
    assert p.returncode != 0 and "refusing to start" in p.stderr


def test_worker_env_carries_no_secret(monkeypatch):
    sys.path.insert(0, API)
    import server
    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_test_secret")
    env = server.worker_env()
    assert not any("STRIPE" in k or "rk_test" in v for k, v in env.items())


def test_site_renders_real_api_output(stack, tmp_path):
    """The static site's own render code, fed the real responses above."""
    import shutil
    if not shutil.which("node"):
        pytest.skip("node not installed")
    mini, rep = tmp_path / "mini.json", tmp_path / "report.json"
    mini.write_text(json.dumps(stack["mini_json"]))
    rep.write_text(json.dumps(stack["report_json"]))
    site_tests = os.path.join(API, "..", "site", "tests")
    p = subprocess.run(["node", os.path.join(site_tests, "contract.mjs"), str(mini), str(rep)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    print(p.stdout)
