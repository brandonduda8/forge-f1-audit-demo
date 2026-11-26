"""Integration: the real server, the real payments kit (square_api) and the real runner,
against FakeSquare and a local fixture site. Nothing here is faked between the parts the
four v2 pieces own: plans -> checkout -> pay -> signed webhook -> report email -> dashboard ->
admin sales -> monthly monitoring with a change email -> cancel at period end.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, API)
from helpers import LOC, FakeSquare, serve_site  # noqa: E402

ORIGIN = "https://checkup.example"
TOKEN = "EAAAtest_fake_token_for_tests"
SIG_KEY = "test_signature_key_123"
HOOK_URL = "https://api.checkup.example/api/square/webhook"
ADMIN = "integration-admin-token-0123456789"
DAY = 24 * 3600


def call(base, path, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Origin", ORIGIN)
    if data:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=200) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def signed_hook(base, oid):
    event = {"merchant_id": "M1", "type": "payment.updated", "event_id": "e-" + oid,
             "data": {"type": "payment", "id": "p", "object": {"payment": {"order_id": oid, "status": "COMPLETED"}}}}
    raw = json.dumps(event).encode()
    sig = base64.b64encode(hmac.new(SIG_KEY.encode(), HOOK_URL.encode() + raw, hashlib.sha256).digest()).decode()
    req = urllib.request.Request(base + "/api/square/webhook", data=raw, method="POST")
    req.add_header("x-square-hmacsha256-signature", sig)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as r:
        return r.status


def kind(m):
    subj = m["Subject"] or ""
    return "report_ready" if subj.startswith("Your website checkup") else (
        "monitor_report" if subj.startswith("Monthly check") else "other")


def text(m):
    return m["To"] + "\n" + m.get_content()


def wait_for(cond, timeout=150):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError("timed out")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    sq = FakeSquare()
    var = sq.add_plan_variation(1900)
    site, site_url = serve_site("example-roofing")
    env = {"SITE_ORIGIN": ORIGIN, "SQUARE_ACCESS_TOKEN": TOKEN, "SQUARE_ENV": "sandbox",
           "SQUARE_LOCATION_ID": LOC, "SQUARE_API_BASE": sq.base, "SQUARE_WEBHOOK_SIGNATURE_KEY": SIG_KEY,
           "SQUARE_WEBHOOK_URL": HOOK_URL, "SQUARE_MONITOR_PLAN_VARIATION_ID": var,
           "SELFSERVE_TEST_ALLOW_LOOPBACK": "1", "ADMIN_TOKEN": ADMIN, "PAID_WORKERS": "2",
           "MAIL_BACKEND": "capture", "MAIL_FROM": "checkup@apex.example",
           "APEX_DATA_DIR": str(tmp_path_factory.mktemp("apexdata"))}
    for k, v in env.items():
        mp.setenv(k, v)
    import importlib
    import mailer
    import monitor
    import server
    import square_api
    import store
    for m in (square_api, store, mailer, monitor, server):
        importlib.reload(m)
    mailer.CAPTURED.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    yield {"base": base, "sq": sq, "var": var, "site": site_url + "/", "server": server,
           "monitor": monitor, "mailer": mailer}
    srv.shutdown()
    srv.server_close()
    site.shutdown()
    mp.undo()


def buy(w, plan, amount, customer=None, email="owner@roof.example"):
    code, out = call(w["base"], "/api/checkout", {"url": w["site"], "plan": plan})
    assert code == 200, out
    oid = out["order_id"]
    w["sq"].pay(oid, amount=amount, customer_id=customer, email=email)
    return oid


def test_plans_show_all_three_with_real_prices(world):
    code, out = call(world["base"], "/api/plans")
    assert code == 200
    by = {p["id"]: p for p in out["plans"]}
    assert (by["checkup"]["cents"], by["pro"]["cents"], by["monitor"]["cents"]) == (2900, 7900, 1900)
    assert all(p["available"] for p in out["plans"])
    assert world["var"] not in json.dumps(out)          # never exposes the catalog id


def test_pro_purchase_runs_pro_mode_with_pages_and_pdf(world):
    oid = buy(world, "pro", 7900)
    code, rep = call(world["base"], "/api/report?order_id=" + oid)
    assert code == 200, rep
    assert "pages" in rep["report"] and isinstance(rep["report"]["pages"], list)
    code, pdf = call(world["base"], "/api/report.pdf?order_id=" + oid)
    assert code == 200 and pdf[:4] == b"%PDF"


def test_pro_price_is_enforced(world):
    code, out = call(world["base"], "/api/checkout", {"url": world["site"], "plan": "pro"})
    oid = out["order_id"]
    world["sq"].pay(oid, amount=2900)                   # paid the checkup price for a pro order
    code, rep = call(world["base"], "/api/report?order_id=" + oid)
    assert code == 402


def test_monitor_full_lifecycle(world):
    w, sq, mailer, monitor = world, world["sq"], world["mailer"], world["monitor"]
    sq.add_customer("CUST0000000001", "owner@roof.example")
    oid = buy(w, "monitor", 1900, customer="CUST0000000001")
    sq.subscribe("SUBINT000000001", "CUST0000000001", variation=w["var"])

    # signed webhook builds the report early and sends exactly one "report ready" email
    assert signed_hook(w["base"], oid) == 200
    assert signed_hook(w["base"], oid) == 200
    wait_for(lambda: any(kind(m) == "report_ready" for m in mailer.CAPTURED))
    time.sleep(1)
    ready = [m for m in mailer.CAPTURED if kind(m) == "report_ready"]
    assert len(ready) == 1 and ready[0]["To"] == "owner@roof.example"
    assert "dashboard.html?order_id=" + oid in text(ready[0])

    # dashboard: plan, active subscription, one history row
    code, d = call(w["base"], "/api/dashboard?order_id=" + oid)
    assert code == 200, d
    assert d["plan"] == "monitor" and d["subscription"]["status"] == "ACTIVE"
    assert len(d["history"]) == 1

    # admin: sales summary from Square, bearer token required
    assert call(w["base"], "/api/admin/summary")[0] in (401, 403)
    code, s = call(w["base"], "/api/admin/summary?days=30", headers={"Authorization": "Bearer " + ADMIN})
    assert code == 200, s
    sales = s.get("sales") or s
    assert sales["active_subscriptions"] >= 1 and sales["mrr_cents"] >= 1900
    assert sales["by_plan"]["monitor"]["count"] >= 1
    assert "owner@roof.example" not in json.dumps(s)    # no buyer emails in the owner view

    # monitoring now: just scanned, so skipped; 26 days later: re-scanned and emailed once
    first = monitor.run_monitoring(w["server"].run_checkup, store=w["server"].data())
    assert first["sites"] >= 1 and first["scanned"] == 0
    later = monitor.run_monitoring(w["server"].run_checkup, now=time.time() + 26 * DAY, store=w["server"].data())
    assert later["scanned"] == 1 and later["emails_sent"] == 1
    again = monitor.run_monitoring(w["server"].run_checkup, now=time.time() + 26 * DAY + 60, store=w["server"].data())
    assert again["scanned"] == 0 and again["emails_sent"] == 0
    monthly = [m for m in mailer.CAPTURED if kind(m) == "monitor_report"]
    assert len(monthly) == 1 and "dashboard.html?order_id=" + oid in text(monthly[0])
    code, d = call(w["base"], "/api/dashboard?order_id=" + oid)
    assert len(d["history"]) == 2

    # cancel: at period end, and a second press doesn't cancel twice
    code, c = call(w["base"], "/api/cancel", {"order_id": oid})
    assert code == 200, c
    code, c2 = call(w["base"], "/api/cancel", {"order_id": oid})
    assert code == 200, c2
    assert sq.cancels == ["SUBINT000000001"]
    code, d = call(w["base"], "/api/dashboard?order_id=" + oid)
    assert d["subscription"]["canceled_date"]


def test_cancel_refused_for_one_time_orders(world):
    oid = buy(world, "checkup", 2900)
    code, out = call(world["base"], "/api/cancel", {"order_id": oid})
    assert code == 409
