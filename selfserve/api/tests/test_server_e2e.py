"""End to end: the real server process, a fake Square, and local fixture sites.

Flow tested: free check -> /api/checkout makes a Square hosted checkout ->
(buyer pays on Square) -> signed webhook starts the report early -> report page
asks for the report with the order id -> report + PDF; plus every refusal path
and the automatic refund for a site that cannot be reached.
"""
import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from helpers import LOC, FakeSquare, serve_site  # noqa: E402

ORIGIN = "https://checkup.example"
TOKEN = "EAAAtest_fake_token_for_tests"
SIG_KEY = "test_signature_key_123"
HOOK_URL = "https://api.checkup.example/api/square/webhook"
(GOOD, UNPAID, OTHER_LOC, NO_MARKER, OLD, LOW, EUR, DOWN, NOSITE, DELIVERED, FORBID) = (
    "ORD" + x * 22 for x in "ABCDEFGHIJK")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def call(base, path, body=None, origin=ORIGIN, timeout=150, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    if origin:
        req.add_header("Origin", origin)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def jcall(*a, **k):
    code, headers, body = call(*a, **k)
    return code, headers, json.loads(body)


def sign(raw, key=SIG_KEY, url=HOOK_URL):
    return base64.b64encode(hmac.new(key.encode(), url.encode() + raw, hashlib.sha256).digest()).decode()


def hook(base, event, signature=None):
    raw = json.dumps(event).encode()
    sig = sign(raw) if signature is None else signature
    return call(base, "/api/square/webhook", raw=raw, origin=None,
                headers={"x-square-hmacsha256-signature": sig} if sig else None)


def paid_event(oid, status="COMPLETED"):
    return {"merchant_id": "M1", "type": "payment.updated", "event_id": "e-" + oid,
            "data": {"type": "payment", "id": "p", "object": {"payment": {"order_id": oid, "status": status}}}}


@pytest.fixture(scope="module")
def stack():
    sq = FakeSquare()
    roof, roof_url = serve_site("example-roofing")
    plumb, plumb_url = serve_site("sample-plumbing")
    sq.add(GOOD, roof_url + "/", email="owner@roof.example")
    sq.add(DELIVERED, roof_url + "/", delivered=True)
    sq.add(FORBID, roof_url + "/forbidden")
    sq.add(UNPAID, roof_url + "/", paid=False)
    sq.add(OTHER_LOC, roof_url + "/", location="LSOMEONEELSE01")
    sq.add(NO_MARKER, roof_url + "/", marker=None)
    sq.add(OLD, roof_url + "/", created=time.time() - 31 * 24 * 3600)
    sq.add(LOW, roof_url + "/", amount=100)
    sq.add(EUR, roof_url + "/", currency="EUR")
    sq.add(DOWN, "http://127.0.0.1:9/")
    sq.add(NOSITE, "")
    port = free_port()
    env = {"PATH": os.environ["PATH"], "PORT": str(port), "BIND": "127.0.0.1",
           "SITE_ORIGIN": ORIGIN, "SQUARE_ACCESS_TOKEN": TOKEN, "SQUARE_ENV": "sandbox",
           "SQUARE_LOCATION_ID": LOC, "SQUARE_API_BASE": sq.base,
           "SQUARE_WEBHOOK_SIGNATURE_KEY": SIG_KEY, "SQUARE_WEBHOOK_URL": HOOK_URL,
           "SELFSERVE_TEST_ALLOW_LOOPBACK": "1", "LIMIT_FREE_PER_SITE": "50",
           "LIMIT_FREE_PER_IP": "4", "PAID_WORKERS": "2"}
    proc = subprocess.Popen([sys.executable, "-I", os.path.join(API, "server.py")], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if call(base, "/health")[0] == 200:
                break
        except Exception:
            time.sleep(0.1)
    yield {"base": base, "sq": sq, "plumb": plumb_url, "roof": roof_url}
    proc.terminate()
    out, err = proc.communicate(timeout=10)
    # order ids and the token must never be written to the server log
    for secret in (GOOD, DOWN, UNPAID, TOKEN, SIG_KEY):
        assert secret not in out + err
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
    assert code == 422 and "couldn't check" in body["error"]


def test_free_check_rate_limited_per_ip(stack):
    # LIMIT_FREE_PER_IP=4 and three checks already ran in the tests above
    codes = [jcall(stack["base"], "/api/check", {"url": "nope"})[0] for _ in range(2)]
    assert codes[-1] == 429


def test_checkout_makes_square_link_without_card_data(stack):
    sq = stack["sq"]
    code, _, body = jcall(stack["base"], "/api/checkout", {"url": "  " + stack["plumb"] + "  "})
    assert code == 200 and body["ok"], body
    assert body["checkout_url"].startswith(sq.base + "/checkout/") and body["order_id"]
    assert set(body) == {"ok", "checkout_url", "order_id"}
    sent = sq.links[-1]
    order = sent["order"]
    assert order["location_id"] == LOC
    assert order["metadata"] == {"apex": "apex-checkup-v1", "website": stack["plumb"] + "/"}  # normalized
    assert order["line_items"][0]["base_price_money"] == {"amount": 2900, "currency": "USD"}
    assert sent["checkout_options"]["redirect_url"] == ORIGIN + "/report.html"
    assert sent["idempotency_key"] and not any(k in json.dumps(sent) for k in ("card_number", "cvv", "source_id"))
    stack["checkout_order"] = body["order_id"]
    code, _, body = jcall(stack["base"], "/api/checkout", {"url": "Bob-Roofing.example"})
    assert code == 200 and sq.links[-1]["order"]["metadata"]["website"] == "https://bob-roofing.example/"
    # an address we couldn't check never reaches Square
    n = len(sq.links)
    code, _, body = jcall(stack["base"], "/api/checkout", {"url": "javascript:alert(1)"})
    assert code == 400 and not body["ok"] and len(sq.links) == n


def test_unpaid_checkout_order_gets_no_report(stack):
    code, _, body = jcall(stack["base"], "/api/report?order_id=" + stack["checkout_order"])
    assert code == 402 and "completed payment" in body["error"]


def test_webhook_rejects_bad_signatures(stack):
    oid = stack["checkout_order"]
    assert hook(stack["base"], paid_event(oid), signature="")[0] == 401
    assert hook(stack["base"], paid_event(oid), signature=sign(b"other body"))[0] == 401
    assert hook(stack["base"], paid_event(oid), signature=sign(json.dumps(paid_event(oid)).encode(), key="wrong"))[0] == 401
    assert hook(stack["base"], paid_event(oid), signature=sign(json.dumps(paid_event(oid)).encode(),
                                                               url="https://evil.example/hook"))[0] == 401
    assert call(stack["base"], "/api/square/webhook", raw=b"x" * 70000, origin=None)[0] == 400


def test_signed_payment_webhook_builds_report_before_buyer_lands(stack):
    sq, oid = stack["sq"], stack["checkout_order"]
    code, _, _ = hook(stack["base"], paid_event(oid, status="APPROVED"))   # not paid yet: nothing runs
    assert code == 200
    time.sleep(1)
    assert not [u for u in sq.order_puts if u[0] == oid]
    sq.pay(oid, email="owner@plumb.example")                               # buyer pays on Square
    t0 = time.time()
    code, _, body = hook(stack["base"], paid_event(oid))
    assert code == 200 and time.time() - t0 < 3                            # answers Square at once
    for _ in range(240):                                                   # report runs in the background
        if [u for u in sq.order_puts if u[0] == oid]:
            break
        time.sleep(0.5)
    assert [u for u in sq.order_puts if u[0] == oid], "webhook did not start the report"
    t0 = time.time()
    code, _, body = jcall(stack["base"], "/api/report?orderId=" + oid)     # Square's own redirect parameter
    assert code == 200 and body["ok"] and time.time() - t0 < 3             # already built
    assert body["pdf_url"] == "/api/report.pdf?order_id=" + oid


def test_paid_report_end_to_end(stack):
    sq = stack["sq"]
    t0 = time.time()
    code, _, body = jcall(stack["base"], "/api/report?order_id=" + GOOD)
    first = time.time() - t0
    assert code == 200 and body["ok"], body
    rep = body["report"]
    assert {c["name"] for c in rep["categories"]} == {"Speed", "Mobile", "SEO basics", "Contact path", "Reviews", "Trust signals"}
    assert rep["findings"] and rep["passed"] and rep["not_measured"]
    assert body["pdf_url"] == "/api/report.pdf?order_id=" + GOOD
    assert "markdown" not in body
    t0 = time.time()
    code, _, again = jcall(stack["base"], "/api/report?order_id=" + GOOD)
    assert code == 200 and again["report"] == rep and time.time() - t0 < first + 1  # served from cache
    stack["report_json"] = body
    code, h, pdf = call(stack["base"], body["pdf_url"])
    assert code == 200 and h["Content-Type"] == "application/pdf" and pdf[:5] == b"%PDF-" and len(pdf) > 5000
    assert "attachment" in h["Content-Disposition"]
    # delivery is recorded on the Square order itself, so it survives a restart
    puts = [b for o, b in sq.order_puts if o == GOOD]
    assert len(puts) == 1 and puts[0]["order"]["metadata"] == {"apex_report_delivered": "1"}
    assert puts[0]["order"]["version"] == 3


def test_parallel_loads_share_one_run(stack):
    import threading
    oid = "ORD" + "P" * 22
    stack["sq"].add(oid, stack["plumb"] + "/")
    out = []
    ts = [threading.Thread(target=lambda: out.append(jcall(stack["base"], "/api/report?order_id=" + oid)))
          for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert [c for c, _, _ in out] == [200, 200, 200]
    assert len({json.dumps(b["report"], sort_keys=True) for _, _, b in out}) == 1
    assert len([u for u in stack["sq"].order_puts if u[0] == oid]) == 1     # one run, one delivery mark


def test_error_homepage_is_not_graded_and_is_refundable(stack):
    code, _, body = jcall(stack["base"], "/api/report?order_id=" + FORBID)
    assert code == 422 and body["refundable"] is True and "HTTP 403" in body["error"]


@pytest.mark.parametrize("oid,code", [
    (UNPAID, 402), (OTHER_LOC, 402), (NO_MARKER, 402), (OLD, 402), (LOW, 402), (EUR, 402), (NOSITE, 402),
    ("ORD" + "Z" * 22, 402), ("bad id!", 402), ("short", 402)])
def test_report_refusals(stack, oid, code):
    c, _, body = jcall(stack["base"], "/api/report?order_id=" + urllib.parse.quote(oid))
    assert c == code and not body["ok"] and body["error"]


def test_unreachable_site_offers_and_issues_one_refund(stack):
    sq = stack["sq"]
    code, _, body = jcall(stack["base"], "/api/report?order_id=" + DOWN)
    assert code == 422 and body["refundable"] is True
    code, _, r1 = jcall(stack["base"], "/api/refund", {"order_id": DOWN})
    code2, _, r2 = jcall(stack["base"], "/api/refund", {"order_id": DOWN})
    assert code == 200 and r1 == {"ok": True, "status": "pending"}         # Square refunds start PENDING
    assert code2 == 200 and r2 == {"ok": True, "status": "succeeded"}
    assert len(sq.refunds) == 1                                            # the second click saw it refunded
    r = sq.refunds[0]
    assert r["payment_id"] == "PAY" + DOWN[-20:] and r["amount_money"] == {"amount": 2900, "currency": "USD"}
    assert r["idempotency_key"] == ("apex-refund-" + DOWN)[:45]
    assert {h for h in sq.headers_seen} == {("Bearer " + TOKEN, "2025-01-23")}
    # once refunded (Square says so), the report link stops working
    code, _, body = jcall(stack["base"], "/api/report?order_id=" + DOWN)
    assert code == 402 and "refunded" in body["error"]


def test_no_refund_after_delivered_report(stack):
    code, _, body = jcall(stack["base"], "/api/refund", {"order_id": GOOD})
    assert code == 409 and not body["ok"]
    # not in this server's memory, but the Square order says the report was delivered
    code, _, body = jcall(stack["base"], "/api/refund", {"order_id": DELIVERED})
    assert code == 409 and not body["ok"]
    assert all(r["payment_id"] not in ("PAY" + GOOD[-20:], "PAY" + DELIVERED[-20:]) for r in stack["sq"].refunds)


def test_find_my_report(stack):
    roof_host = stack["roof"].split("//")[1]
    code, _, body = jcall(stack["base"], "/api/find", {"email": "Owner@Roof.example", "website": stack["roof"]})
    assert code == 200 and body["reports"][0]["path"] == "report.html?order_id=" + GOOD, body
    assert body["reports"][0]["site"] == roof_host.split(":")[0]
    assert len(stack["sq"].lists) >= 2          # more payments than one page: it followed the cursor
    q = stack["sq"].lists[0]
    assert q["location_id"] == [LOC] and q["begin_time"] and q["sort_order"] == ["DESC"]
    # right email, wrong website: nothing (an email alone never lists someone's purchases)
    code, _, body = jcall(stack["base"], "/api/find", {"email": "owner@roof.example", "website": "other.example"})
    assert code == 404 and "reports" not in body
    code, _, body = jcall(stack["base"], "/api/find", {"email": "not-an-email", "website": "x.example"})
    assert code == 400


def test_unknown_routes_and_bad_bodies(stack):
    assert jcall(stack["base"], "/admin")[0] == 404
    assert call(stack["base"], "/api/check", body=None)[0] == 404   # GET on a POST route
    code, _, _ = call(stack["base"], "/api/refund", {"order_id": "x" * 5000})
    assert code == 400
    code, _, _ = call(stack["base"], "/api/refund", {"order_id": "../v2/payments"})
    assert code == 400


@pytest.mark.parametrize("square_env", ["production", "live"])
def test_refuses_to_start_unsafely(square_env):
    env = {"PATH": os.environ["PATH"], "PORT": str(free_port()), "BIND": "127.0.0.1",
           "SQUARE_ENV": square_env, "SELFSERVE_TEST_ALLOW_LOOPBACK": "1"}
    p = subprocess.run([sys.executable, "-I", os.path.join(API, "server.py")], env=env,
                       capture_output=True, text=True, timeout=20)
    assert p.returncode != 0 and "refusing to start" in p.stderr


def test_worker_env_carries_no_secret(monkeypatch):
    sys.path.insert(0, API)
    import server
    monkeypatch.setenv("SQUARE_ACCESS_TOKEN", "EAAAsecret")
    monkeypatch.setenv("SQUARE_WEBHOOK_SIGNATURE_KEY", "sigsecret")
    env = server.worker_env()
    assert not any("SQUARE" in k or "secret" in v for k, v in env.items())


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


def test_pdf_survives_emoji_and_non_latin_text(tmp_path):
    md = tmp_path / "r.md"
    md.write_text("# Report for \U0001F680 Caf\u00e9 \u6771\u4eac\n\nTitle: \u201cBest\u201d \U0001F600\n")
    pdf = tmp_path / "r.pdf"
    p = subprocess.run([sys.executable, "-I", os.path.join(API, "..", "..", "f1-audit-product", "tools", "render_pdf.py"),
                        str(md), str(pdf)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert pdf.read_bytes()[:5] == b"%PDF-"


def test_static_publish_excludes_tests_and_notes(tmp_path):
    """Runs the static site's build command from render.yaml in a copy of the site."""
    import re
    import shutil
    repo = os.path.abspath(os.path.join(API, "..", ".."))
    yml = open(os.path.join(repo, "render.yaml")).read()
    block = yml[yml.index("name: apex-checkup\n"):]
    cmd = re.search(r"buildCommand: (.+)", block).group(1).strip()
    publish = re.search(r"staticPublishPath: (\S+)", block).group(1)
    work = tmp_path / "site"
    shutil.copytree(os.path.join(repo, "selfserve", "site"), work)
    env = {"PATH": os.environ["PATH"], "APEX_API_BASE": "https://api.example.test",
           "APEX_SUPPORT_EMAIL": "help@example.com"}
    p = subprocess.run(["bash", "-c", cmd], cwd=work, env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    out = set(os.listdir(work / publish))
    assert {"index.html", "report.html", "find.html", "config.js", "common.js", "style.css"} <= out
    assert not out & {"tests", "README.md", "build_config.py", ".gitignore", "config.example.js"}
