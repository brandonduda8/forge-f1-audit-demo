"""v2 server: store, monitoring, buyer emails, dashboard, cancel, admin, plans.

Everything runs in this process against local servers only: the API's own
Handler on 127.0.0.1, a tiny fake SMTP server (with real STARTTLS using a
throwaway self-signed certificate), and fake square_api functions (the real
Square calls are builder A's and are tested in test_square_kit.py).
"""
import base64
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.path.dirname(HERE)
sys.path.insert(0, API)
import mailer  # noqa: E402
import monitor  # noqa: E402
import server  # noqa: E402
import square_api  # noqa: E402
import store as store_mod  # noqa: E402

ORIGIN = "https://checkup.example"
ADMIN = "a" * 20 + "-admin-token-xyz"          # 36 chars
OID_CHECKUP, OID_PRO, OID_MON, OID_OLD, OID_REFUNDED = ("ORD" + c * 22 for c in "CPMOR")
SUB = "SUBSCRIPTION0000001"
DAY = 24 * 3600
JAN1 = 1767225600.0                              # 2026-01-01T00:00:00Z


# ---------------------------------------------------------------- helpers

def result(score=80, grade="B", findings=(), sections=(), pages=None, site="shop.example"):
    rep = {"findings": [dict(f) for f in findings],
           "sections": [{"name": n, "score": 1, "findings": [dict(f) for f in fs]} for n, fs in sections],
           "categories": [], "passed": [], "not_measured": []}
    if pages is not None:
        rep["pages"] = pages
    return {"ok": True, "site": site, "url": "https://" + site + "/", "score": score, "grade": grade,
            "report": rep, "markdown": "# r\n"}


F1 = {"id": "1.1", "title": "Page loads slowly", "severity": "MAJOR"}
F2 = {"id": "2.3", "title": "No meta description", "severity": "MINOR"}
F3 = {"id": "4.1", "title": "Phone number not clickable", "severity": "MAJOR"}


class FakePay:
    """Stand-ins for builder A's square_api functions (contract shapes)."""

    def __init__(self):
        self.orders = {
            OID_CHECKUP: {"website": "https://shop.example/", "plan": "checkup"},
            OID_PRO: {"website": "https://pro.example/", "plan": "pro"},
            OID_MON: {"website": "https://watch.example/", "plan": "monitor"},
            OID_OLD: {"website": "https://old.example/"},                      # no plan field
            OID_REFUNDED: {"website": "https://gone.example/", "plan": "checkup", "refunded": True},
        }
        self.checkouts, self.cancels, self.summaries, self.refunds, self.events = [], [], [], [], []
        self.state, self.ready, self.state_writes = {}, set(), []
        self.found, self.found_monitor = [], []
        self.cancel_fails = False
        self.sub = {"id": SUB, "status": "ACTIVE", "start_date": "2026-01-01",
                    "charged_through_date": "2026-02-01", "canceled_date": None}
        self.sites = [{"subscription_id": SUB, "customer_id": "CUST1", "order_id": OID_MON,
                       "website": "https://watch.example/", "email": "owner@watch.example"}]
        self.unavailable = False
        self.order_calls = 0

    def get_paid_order(self, oid, now=None):
        self.order_calls += 1
        if self.unavailable:
            raise square_api.SquareUnavailable("down")
        if oid not in self.orders:
            raise square_api.PaymentError("We couldn't find a payment for this link.")
        o = self.orders[oid]
        out = {"order_id": oid, "website": o["website"], "payment_id": "PAY" + oid[-10:], "amount": 2900,
               "created": o.get("created", JAN1 - 40 * DAY),
               "buyer_email": "buyer-" + oid[-3:].lower() + "@example.com",
               "refunded": o.get("refunded", False), "delivered": o.get("delivered", False), "order_version": 3,
               "customer_id": "CUST1", "ready_mailed": oid in self.ready,
               "monitor_state": self._state(oid)}
        if "plan" in o:
            out["plan"] = o["plan"]
        return out

    def _state(self, oid):
        return dict(self.state.get(oid) or {"last": None, "month": None, "failing": [], "not_measured": []})

    def mark_delivered(self, paid):
        self.orders[paid["order_id"]]["delivered"] = True
        return True

    def set_monitor_state(self, paid, *, last, month, failing, not_measured):
        self.state_writes.append((paid["order_id"], last, month))
        self.state[paid["order_id"]] = {"last": float(last), "month": month, "failing": list(failing),
                                        "not_measured": list(not_measured)}
        return True

    def mark_ready_mailed(self, paid):
        self.ready.add(paid["order_id"])
        return True

    def plan_info(self, plan_id):
        if plan_id == "monitor":
            return {"id": "monitor", "label": "Monthly Monitoring", "cents": 1900, "first_cents": 2900,
                    "kind": "subscription", "mode": "full", "available": True}
        return None

    def refund(self, paid):
        self.events.append("refund")
        self.refunds.append(paid["order_id"])
        return "succeeded"

    def find_orders(self, email):
        return [dict(f) for f in self.found if f.get("email") == email]

    def find_monitor_orders(self, email):
        return [dict(f) for f in self.found_monitor if f.get("email") == email]

    def create_checkout(self, website, redirect_url, plan="checkup"):
        if plan not in ("checkup", "pro", "monitor"):
            raise square_api.PaymentError("unknown plan")
        if plan == "monitor" and os.environ.get("FAKE_NO_MONITOR") == "1":
            raise square_api.PaymentError("not available")
        self.checkouts.append((website, redirect_url, plan))
        return "https://square.link/u/fake" + str(len(self.checkouts)), "ORDNEW" + "X" * 20

    def public_plans(self):
        return [{"id": "checkup", "label": "Website Checkup", "cents": 2900, "kind": "one_time", "available": True},
                {"id": "pro", "label": "Pro Checkup", "cents": 7900, "kind": "one_time", "available": True},
                {"id": "monitor", "label": "Monthly Monitoring", "cents": 1900, "first_cents": 2900,
                 "kind": "subscription", "available": os.environ.get("FAKE_NO_MONITOR") != "1"}]

    def subscription_for_order(self, paid):
        return dict(self.sub) if paid.get("plan") == "monitor" else None

    def cancel_subscription(self, sid):
        if self.cancel_fails:
            raise square_api.SquareUnavailable("down")
        self.events.append("cancel")
        self.cancels.append(sid)
        self.sub["canceled_date"] = "2026-02-01"
        return {"status": "ACTIVE", "ends": "2026-02-01"}

    def monitored_sites(self):
        if self.unavailable:
            raise square_api.SquareUnavailable("down")
        return [dict(s, state=self._state(s["order_id"])) for s in self.sites]

    def sales_summary(self, days=30, now=None):
        self.summaries.append(days)
        if self.unavailable:
            raise square_api.SquareUnavailable("down")
        return {"days": days, "orders": 3, "gross_cents": 12700, "refunded_cents": 0, "net_cents": 12700,
                "by_plan": {"checkup": {"count": 1, "gross_cents": 2900}}, "active_subscriptions": 1,
                "mrr_cents": 1900, "recent": []}

    def verify_webhook(self, raw, sig):
        return sig == "good"

    def event_kind(self, event):
        t = event.get("type", "")
        if t == "payment.updated":
            return "payment_completed", event["order_id"]
        if t.startswith("subscription."):
            return "subscription_changed", event.get("subscription_id")
        return None, None

    def install(self, monkeypatch):
        for name in ("get_paid_order", "mark_delivered", "create_checkout", "public_plans",
                     "subscription_for_order", "cancel_subscription", "monitored_sites", "sales_summary",
                     "verify_webhook", "event_kind", "set_monitor_state", "mark_ready_mailed", "plan_info",
                     "refund", "find_orders", "find_monitor_orders"):
            monkeypatch.setattr(square_api, name, getattr(self, name), raising=False)


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    """No 2-second pauses between monitored sites in tests; fresh in-memory server marks."""
    monkeypatch.setattr(monitor, "PAUSE_BETWEEN_S", 0)
    with server._lock:
        for d in (server._refunded, server._not_ours, server._ready_sent):
            d.clear()
        server._paid_waiting = 0
    yield


@pytest.fixture
def db(tmp_path):
    s = store_mod.Store(str(tmp_path / "data" / "apex.sqlite3"))
    store_mod.set_default(s)
    yield s
    store_mod.set_default(None)
    s.close()


@pytest.fixture
def fake(monkeypatch):
    f = FakePay()
    f.install(monkeypatch)
    return f


@pytest.fixture
def capture_mail(monkeypatch):
    monkeypatch.setenv("SELFSERVE_TEST_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("MAIL_BACKEND", "capture")
    monkeypatch.setenv("MAIL_FROM", "checkup@apex.example")
    monkeypatch.setenv("SITE_ORIGIN", ORIGIN)
    mailer.CAPTURED.clear()
    yield mailer.CAPTURED
    mailer.CAPTURED.clear()


@pytest.fixture
def api(monkeypatch, db, fake):
    """The real Handler on a loopback port, with fresh in-memory state."""
    monkeypatch.setenv("SITE_ORIGIN", ORIGIN)
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    with server._lock:
        server._hits.clear()
        server._reports.clear()
        server._pdfs.clear()
        server._fatal.clear()
        for k in server._counters:
            server._counters[k] = 0
    runs = []

    def fake_run(mode, url, pool=None):
        runs.append((mode, url))
        return result(site=url.split("//")[1].strip("/"), findings=[F1])
    monkeypatch.setattr(server, "run_checkup", fake_run)
    monkeypatch.setattr(server, "_run_runner", lambda mode, url: fake_run(mode, url))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"base": "http://127.0.0.1:%d" % srv.server_address[1], "runs": runs, "fake": fake, "db": db}
    srv.shutdown()
    srv.server_close()


def call(base, path, body=None, method=None, headers=None, origin=ORIGIN, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method or ("POST" if data is not None else "GET"))
    if data:
        req.add_header("Content-Type", "application/json")
    if origin:
        req.add_header("Origin", origin)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=20) as r:
            b = r.read()
            return r.status, dict(r.headers), (json.loads(b) if b else None)
    except urllib.error.HTTPError as e:
        b = e.read()
        return e.code, dict(e.headers), (json.loads(b) if b else None)


def bearer(t=ADMIN):
    return {"Authorization": "Bearer " + t}


def wait_for(cond, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- store

def test_store_creates_missing_dir_and_persists(tmp_path, monkeypatch):
    d = tmp_path / "nested" / "dir"
    monkeypatch.setenv("APEX_DATA_DIR", str(d))
    s = store_mod.Store()
    assert os.path.isdir(d) and os.path.exists(d / "apex.sqlite3")
    t = time.time() - 100
    s.add_scan(OID_CHECKUP, "shop.example", "B", 80, [F1], at=t, plan="checkup")
    s.close()
    s2 = store_mod.Store()
    assert s2.history(OID_CHECKUP) == [{"at": t, "grade": "B", "score": 80}]
    assert s2.last_scan(OID_CHECKUP)["issues"] == [F1]
    s2.close()


def test_store_history_newest_first_max_24(db):
    for i in range(30):
        db.add_scan(OID_MON, "watch.example", "C", i, [], at=1000 + i, plan="monitor")
    db.add_scan(OID_PRO, "pro.example", "A", 99, [], at=time.time())
    h = db.history(OID_MON)
    assert len(h) == 24 and h[0]["score"] == 29 and h[-1]["score"] == 6
    assert [x["at"] for x in h] == sorted((x["at"] for x in h), reverse=True)
    assert db.history(OID_MON, limit=500) == h
    assert db.latest_changes(OID_MON) is None
    db.add_scan(OID_MON, "watch.example", "B", 50, [], at=9000, changes={"new": [F1], "fixed": []}, plan="monitor")
    assert db.latest_changes(OID_MON) == {"new": [F1], "fixed": []}
    assert db.last_scan("ORDNOTHING00000000") is None and db.history("ORDNOTHING00000000") == []


def test_store_email_claim_is_once_even_across_threads(db):
    wins = []
    ts = [threading.Thread(target=lambda: wins.append(db.claim_email("report_ready:" + OID_PRO)))
          for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins.count(True) == 1 and db.email_sent("report_ready:" + OID_PRO)
    db.release_email("report_ready:" + OID_PRO)
    assert not db.email_sent("report_ready:" + OID_PRO) and db.claim_email("report_ready:" + OID_PRO)


def test_store_runs(db):
    assert db.last_run() is None
    rid = db.start_run(at=10)
    assert db.last_run() == {"started": 10, "finished": None, "counts": None}
    db.finish_run(rid, {"sites": 2}, at=20)
    assert db.last_run() == {"started": 10, "finished": 20, "counts": {"sites": 2}}


def test_store_keeps_no_email_addresses(db, fake, monkeypatch):
    monitor.run_monitoring(lambda m, u: result(findings=[F1]), now=JAN1, store=db)
    raw = open(db.path, "rb").read()
    for p in (db.path + "-wal",):
        if os.path.exists(p):
            raw += open(p, "rb").read()
    assert b"owner@watch.example" not in raw


# ---------------------------------------------------------------- diffing

def test_issues_of_collects_findings_sections_and_pages():
    res = result(findings=[F1, F2], sections=[("Accessibility", [F3]), ("Security", [F1])],
                 pages=[{"path": "/about", "issues": [{"id": "p.h1", "title": "No H1", "severity": "MINOR"}]}])
    got = monitor.issues_of(res)
    assert [i["id"] for i in got] == ["1.1", "2.3", "4.1", "page:/about:p.h1"]
    assert got[-1]["title"] == "/about: No H1"
    assert monitor.issues_of({"ok": True}) == []


def test_diff_new_fixed_and_score_change():
    prev = {"at": 5, "grade": "C", "score": 70, "issues": [F1, F2, {"id": "", "title": "Old untitled-id"}]}
    cur = [F2, F3, {"id": "", "title": "Old untitled-id"}]
    d = monitor.diff(prev, cur, "B", 78)
    assert d["new"] == [F3] and d["fixed"] == [F1]
    assert d["score_before"] == 70 and d["score_after"] == 78 and d["score_change"] == 8
    assert d["grade_before"] == "C" and d["grade_after"] == "B" and d["since"] == 5
    assert monitor.diff(None, cur, "B", 78) is None
    same = monitor.diff({"score": 70, "issues": [F1]}, [F1], "C", 70)
    assert same["new"] == [] and same["fixed"] == [] and same["score_change"] == 0


# ---------------------------------------------------------------- monitoring

def test_monitoring_first_scan_saves_and_emails_once(db, fake, capture_mail):
    seen = []

    def run(mode, url):
        seen.append((mode, url))
        return result(site="watch.example", findings=[F1])
    got = []
    c = monitor.run_monitoring(run, now=JAN1, store=db, on_result=lambda oid, r: got.append(oid))
    assert c == {"sites": 1, "scanned": 1, "skipped_recent": 0, "not_checked": 0,
                 "emails_sent": 1, "emails_skipped": 0}
    assert seen == [("full", "https://watch.example/")] and got == [OID_MON]
    s = db.last_scan(OID_MON)
    assert s["source"] == "monitor" and s["changes"] is None and s["issues"] == [F1]
    assert len(capture_mail) == 1
    m = capture_mail[0]
    assert m["To"] == "owner@watch.example" and "watch.example" in m["Subject"]
    body = m.get_content()
    assert ORIGIN + "/dashboard.html?order_id=" + OID_MON in body     # where the buyer can cancel
    assert ORIGIN + "/report.html?order_id=" + OID_MON in body
    assert "cancel" in body.lower()
    assert db.last_run()["counts"]["scanned"] == 1


def test_monitoring_skips_recent_and_rescans_after_25_days(db, fake, capture_mail):
    db.add_scan(OID_MON, "watch.example", "C", 70, [F1, F2], at=JAN1 - 10 * DAY)   # the purchase report
    calls = []

    def run(mode, url):
        calls.append(url)
        return result(score=82, grade="B", site="watch.example", findings=[F2, F3])
    c = monitor.run_monitoring(run, now=JAN1, store=db)
    assert c["skipped_recent"] == 1 and c["scanned"] == 0 and calls == [] and not capture_mail
    c = monitor.run_monitoring(run, now=JAN1 + 16 * DAY, store=db)                 # 26 days after the report
    assert c["scanned"] == 1 and len(calls) == 1
    ch = db.latest_changes(OID_MON)
    assert ch["new"] == [F3] and ch["fixed"] == [F1] and ch["score_change"] == 12
    body = capture_mail[0].get_content()
    assert "70 -> 82 (+12)" in body and "Phone number not clickable" in body and "Page loads slowly" in body
    assert "1 new problem, 1 fixed" in capture_mail[0]["Subject"]


def test_monitoring_email_is_once_per_subscription_per_month(db, fake, capture_mail, monkeypatch):
    monkeypatch.setattr(monitor, "RESCAN_AFTER_S", 0)          # even with no 25-day wait
    run = lambda mode, url: result(site="watch.example")      # noqa: E731
    jan5, jan20, feb2 = JAN1 + 4 * DAY, JAN1 + 19 * DAY, JAN1 + 32 * DAY
    assert monitor.run_monitoring(run, now=jan5, store=db)["emails_sent"] == 1
    c = monitor.run_monitoring(run, now=jan20, store=db)       # this month's email already went out
    assert c["scanned"] == 0 and c["emails_sent"] == 0 and c["skipped_recent"] == 1
    assert monitor.run_monitoring(run, now=feb2, store=db)["emails_sent"] == 1
    assert len(capture_mail) == 2
    assert db.email_sent(f"monitor:{SUB}:2026-01") and db.email_sent(f"monitor:{SUB}:2026-02")
    assert [w[2] for w in fake.state_writes] == ["2026-01", "2026-02"]


def test_monitoring_failed_send_can_retry(db, fake, capture_mail, monkeypatch):
    monkeypatch.setattr(monitor, "RESCAN_AFTER_S", 0)
    monkeypatch.setattr(mailer, "send", lambda *a, **k: False)
    run = lambda mode, url: result(site="watch.example")      # noqa: E731
    assert monitor.run_monitoring(run, now=JAN1, store=db)["emails_sent"] == 0
    assert not db.email_sent(f"monitor:{SUB}:2026-01")         # released for the next try
    assert fake.state_writes == []                             # nothing durable changed


def test_monitoring_unreachable_site_is_not_checked_not_a_fail(db, fake, capture_mail):
    for res in ({"ok": False, "fatal": True, "error": "down"}, {"ok": False, "busy": True}):
        c = monitor.run_monitoring(lambda m, u, r=res: r, now=JAN1, store=db)
        assert c["not_checked"] == 1 and c["scanned"] == 0
    def boom(m, u):
        raise RuntimeError("x")
    assert monitor.run_monitoring(boom, now=JAN1, store=db)["not_checked"] == 1
    assert db.last_scan(OID_MON) is None and not capture_mail


def test_monitoring_skips_bad_rows_and_duplicate_subscriptions(db, fake, capture_mail):
    fake.sites = fake.sites * 2 + [{"subscription_id": "S2", "order_id": "", "website": "x", "email": "a@b.example"},
                                   {"subscription_id": "S3", "order_id": OID_PRO, "website": "https://pro.example/",
                                    "email": "not an email"}]
    c = monitor.run_monitoring(lambda m, u: result(), now=JAN1, store=db)
    assert c["sites"] == 2 and c["scanned"] == 2 and c["emails_sent"] == 1 and c["emails_skipped"] == 1


def test_monitoring_square_down_and_mail_off(db, fake, monkeypatch, capsys):
    monkeypatch.delenv("MAIL_BACKEND", raising=False)
    fake.unavailable = True
    c = monitor.run_monitoring(lambda m, u: result(), now=JAN1, store=db)
    assert c["error"] == "square_unavailable" and db.last_run()["counts"]["error"] == "square_unavailable"
    fake.unavailable = False
    c = monitor.run_monitoring(lambda m, u: result(), now=JAN1, store=db)
    assert c["scanned"] == 1 and c["emails_sent"] == 0
    err = capsys.readouterr().err
    assert OID_MON not in err and "owner@watch.example" not in err and SUB not in err


def test_monitoring_one_run_at_a_time(db, fake):
    gate, started = threading.Event(), threading.Event()

    def slow(mode, url):
        started.set()
        gate.wait(10)
        return result()
    assert monitor.start_in_background(slow, store=db) is True
    assert started.wait(5) and monitor.is_running()
    assert monitor.start_in_background(slow, store=db) is False
    assert monitor.run_monitoring(slow, store=db) == {"busy": True}
    gate.set()
    assert wait_for(lambda: not monitor.is_running())
    assert db.last_run()["counts"]["scanned"] == 1


# ---------------------------------------------------------------- mailer

class FakeSMTP:
    """A tiny SMTP server on 127.0.0.1: EHLO, STARTTLS (real TLS), AUTH PLAIN, MAIL, RCPT, DATA, QUIT."""

    def __init__(self, ctx=None, offer_starttls=True):
        self.ctx, self.offer = ctx, offer_starttls
        self.messages, self.commands, self.auth = [], [], []
        self.tls_seen = False
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        state = {"conn": conn, "tls": False}
        f = conn.makefile("rb")

        def w(s):
            state["conn"].sendall(s.encode() + b"\r\n")
        try:
            w("220 fake.smtp ESMTP")
            mail = {}
            while True:
                line = f.readline()
                if not line:
                    return
                cmd = line.decode("utf-8", "replace").rstrip("\r\n")
                up = cmd.upper()
                self.commands.append(cmd if not up.startswith("AUTH") else "AUTH ***")
                if up.startswith("EHLO") or up.startswith("HELO"):
                    lines = ["fake.smtp"]
                    if self.offer and not state["tls"]:
                        lines.append("STARTTLS")
                    if state["tls"]:
                        lines.append("AUTH PLAIN")
                    lines.append("SIZE 100000")
                    for i, x in enumerate(lines):
                        w(("250 " if i == len(lines) - 1 else "250-") + x)
                elif up == "STARTTLS" and self.offer:
                    w("220 go ahead")
                    state["conn"] = self.ctx.wrap_socket(state["conn"], server_side=True)
                    state["tls"] = self.tls_seen = True
                    f = state["conn"].makefile("rb")
                elif up.startswith("AUTH PLAIN"):
                    self.auth.append(base64.b64decode(cmd.split()[2]).split(b"\0"))
                    w("235 ok")
                elif up.startswith("MAIL FROM:"):
                    mail = {"from": cmd[10:], "rcpt": [], "tls": state["tls"]}
                    w("250 ok")
                elif up.startswith("RCPT TO:"):
                    mail["rcpt"].append(cmd[8:])
                    w("250 ok")
                elif up == "DATA":
                    w("354 end with .")
                    data = b""
                    while not data.endswith(b"\r\n.\r\n"):
                        chunk = f.readline()
                        if not chunk:
                            return
                        data += chunk
                    mail["data"] = data[:-5].decode()
                    self.messages.append(mail)
                    w("250 queued")
                elif up == "QUIT":
                    w("221 bye")
                    return
                else:
                    w("502 no")
        except (OSError, ssl.SSLError):
            return
        finally:
            try:
                state["conn"].close()
            except OSError:
                pass

    def close(self):
        self.sock.close()


@pytest.fixture(scope="module")
def tls_pair(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    d = tmp_path_factory.mktemp("tls")
    key, cert = d / "k.pem", d / "c.pem"
    p = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                        "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1",
                        "-addext", "subjectAltName=IP:127.0.0.1"], capture_output=True, timeout=60)
    if p.returncode != 0:
        pytest.skip("openssl could not make a test certificate")
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(cert), str(key))
    return server_ctx, str(cert)


def smtp_env(monkeypatch, port, **extra):
    env = {"MAIL_BACKEND": "smtp", "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(port), "SMTP_USER": "apexuser",
           "SMTP_PASSWORD": "smtp-secret-pw", "MAIL_FROM": "checkup@apex.example",
           "MAIL_REPLY_TO": "help@apex.example", "SITE_ORIGIN": ORIGIN}
    env.update(extra)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_mailer_sends_over_starttls_with_login(monkeypatch, tls_pair, capsys):
    server_ctx, cafile = tls_pair
    smtp = FakeSMTP(server_ctx)
    smtp_env(monkeypatch, smtp.port)
    monkeypatch.setattr(mailer, "_tls_context", lambda: ssl.create_default_context(cafile=cafile))
    try:
        assert mailer.enabled()
        assert mailer.send("report_ready", "buyer@shop.example", order_id=OID_PRO, site="pro.example", plan="pro")
        assert wait_for(lambda: smtp.messages)
    finally:
        smtp.close()
    m = smtp.messages[0]
    assert m["tls"] is True and smtp.commands.index("STARTTLS") < smtp.commands.index("AUTH ***")
    assert smtp.auth == [[b"", b"apexuser", b"smtp-secret-pw"]]
    assert "buyer@shop.example" in m["rcpt"][0] and len(m["rcpt"]) == 1
    data = m["data"]
    assert "Subject: Your website checkup for pro.example is ready" in data
    assert "Reply-To: help@apex.example" in data and "Content-Type: text/plain" in data
    assert ORIGIN + "/report.html?order_id=" + OID_PRO in data
    assert ORIGIN + "/dashboard.html?order_id=" + OID_PRO in data
    assert capsys.readouterr().err == ""


def test_mailer_refuses_without_starttls(monkeypatch, capsys):
    smtp = FakeSMTP(offer_starttls=False)
    smtp_env(monkeypatch, smtp.port)
    try:
        assert mailer.send("report_ready", "buyer@shop.example", order_id=OID_PRO, site="pro.example") is False
        time.sleep(0.2)
    finally:
        smtp.close()
    assert smtp.messages == [] and smtp.auth == []
    assert not any(c.upper().startswith(("MAIL", "AUTH")) for c in smtp.commands)
    err = capsys.readouterr().err
    assert "MailError" in err and "buyer@shop.example" not in err and OID_PRO not in err
    assert "smtp-secret-pw" not in err


def test_mailer_bad_tls_certificate_sends_nothing(monkeypatch, tls_pair):
    server_ctx, _ = tls_pair
    smtp = FakeSMTP(server_ctx)
    smtp_env(monkeypatch, smtp.port)                           # default context: self-signed cert is refused
    try:
        assert mailer.send("report_ready", "buyer@shop.example", order_id=OID_PRO, site="pro.example") is False
    finally:
        smtp.close()
    assert smtp.messages == [] and smtp.auth == []


def test_mailer_unreachable_server_never_raises(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    smtp_env(monkeypatch, port)
    assert mailer.send("monitor_report", "a@b.example", order_id=OID_MON, site="x.example") is False


@pytest.mark.parametrize("to", ["buyer@shop.example\r\nBcc: evil@x.example", "buyer@shop.example\nX: y",
                                "Buyer <buyer@shop.example>", "a@b.example, evil@x.example", "", "no-at-sign",
                                "x" * 70 + "@shop.example"])
def test_mailer_rejects_header_injection_in_recipient(capture_mail, to):
    assert mailer.send("report_ready", to, order_id=OID_PRO, site="pro.example") is False
    assert capture_mail == []


def test_mailer_rejects_bad_sender_and_sanitizes_text(capture_mail, monkeypatch):
    monkeypatch.setenv("MAIL_FROM", "checkup@apex.example\r\nBcc: evil@x.example")
    assert not mailer.enabled() and mailer.send("report_ready", "a@b.example", order_id=OID_PRO, site="x") is False
    monkeypatch.setenv("MAIL_FROM", "checkup@apex.example")
    monkeypatch.setenv("MAIL_REPLY_TO", "help@apex.example\nBcc: evil@x.example")
    assert mailer.send("report_ready", "a@b.example", order_id=OID_PRO, site="x.example") is False
    monkeypatch.delenv("MAIL_REPLY_TO")
    evil = {"new": [{"id": "9", "title": "Bad\r\nBcc: evil@x.example"}], "fixed": [], "score_before": 1, "score_after": 2}
    assert mailer.send("monitor_report", "a@b.example", order_id=OID_MON, site="evil.example\r\nBcc: e@x.example",
                       grade="B\r\nX: y", score=2, changes=evil)
    m = capture_mail[-1]
    raw = m.as_string()
    assert "Bcc" not in [k for k in m.keys()] and set(m.keys()) == {
        "From", "To", "Subject", "Date", "Message-ID", "Auto-Submitted", "Content-Type",
        "Content-Transfer-Encoding", "MIME-Version"}
    assert "\n" not in m["Subject"] and "your website" in m["Subject"]
    assert "Bad Bcc: evil@x.example" in m.get_content() and "\nBcc:" not in raw


def test_mailer_off_by_default_sends_and_logs_nothing(monkeypatch, capsys):
    for k in ("MAIL_BACKEND", "SMTP_HOST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SITE_ORIGIN", ORIGIN)
    monkeypatch.setenv("MAIL_FROM", "checkup@apex.example")
    assert mailer.backend() == "off" and not mailer.enabled()
    assert mailer.send("report_ready", "buyer@shop.example", order_id=OID_PRO, site="pro.example") is False
    assert capsys.readouterr().err == ""
    # the capture backend is test-only: without loopback test mode it is "off"
    monkeypatch.setenv("MAIL_BACKEND", "capture")
    monkeypatch.delenv("SELFSERVE_TEST_ALLOW_LOOPBACK", raising=False)
    assert mailer.backend() == "off"
    # smtp without a host or without SITE_ORIGIN never sends
    monkeypatch.setenv("MAIL_BACKEND", "smtp")
    assert not mailer.enabled()
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SITE_ORIGIN", "")
    assert not mailer.enabled()


def test_mailer_compose_texts():
    s, b = mailer.compose("report_ready", "a@b.example", order_id=OID_MON, site="watch.example", plan="monitor")
    assert "watch.example" in s and "renews every month" in b and "cancel" in b.lower()
    s, b = mailer.compose("monitor_report", "a@b.example", order_id=OID_MON, site="watch.example",
                          grade="B", score=81, changes=None)
    assert "first monitoring check" in b and "Grade now: B (score 81" in b
    many = {"new": [{"id": str(i), "title": "P%d" % i} for i in range(14)], "fixed": [],
            "score_before": 90, "score_after": 60}
    s, b = mailer.compose("monitor_report", "a@b.example", order_id=OID_MON, site="w.example", changes=many)
    assert "14 new problems" in s and "...and 4 more" in b and "90 -> 60 (-30)" in b
    with pytest.raises(mailer.MailError):
        mailer.compose("newsletter", "a@b.example", order_id=OID_MON, site="w.example")


# ---------------------------------------------------------------- routes

def test_plans_route(api):
    code, h, body = call(api["base"], "/api/plans")
    assert code == 200 and [p["id"] for p in body["plans"]] == ["checkup", "pro", "monitor"]
    assert h["Cache-Control"] == "no-store" and h["Access-Control-Allow-Origin"] == ORIGIN


@pytest.mark.parametrize("plan,expect", [(None, "checkup"), ("checkup", "checkup"), ("pro", "pro"),
                                         ("monitor", "monitor")])
def test_checkout_passes_plan(api, capture_mail, plan, expect):
    body = {"url": "shop.example"} if plan is None else {"url": "shop.example", "plan": plan}
    code, _, out = call(api["base"], "/api/checkout", body)
    assert code == 200 and set(out) == {"ok", "checkout_url", "order_id"}
    assert api["fake"].checkouts[-1] == ("https://shop.example/", ORIGIN + "/report.html", expect)
    assert server._counters["checkouts_started"] == 1


@pytest.mark.parametrize("plan", ["gold", "PRO", "../x", "monitor; drop"])
def test_checkout_unknown_plan_never_reaches_square(api, plan):
    code, _, out = call(api["base"], "/api/checkout", {"url": "shop.example", "plan": plan})
    assert code == 400 and not out["ok"] and api["fake"].checkouts == []


def test_checkout_unavailable_plan(api, monkeypatch, capture_mail):
    monkeypatch.setenv("FAKE_NO_MONITOR", "1")
    code, _, out = call(api["base"], "/api/checkout", {"url": "shop.example", "plan": "monitor"})
    assert code == 400 and out["error"] == "That plan isn't available."


def test_monitor_not_sold_when_mail_is_off(api, monkeypatch):
    monkeypatch.delenv("MAIL_BACKEND", raising=False)
    code, _, body = call(api["base"], "/api/plans")
    by = {p["id"]: p for p in body["plans"]}
    assert by["monitor"]["available"] is False and by["checkup"]["available"] is True
    code, _, out = call(api["base"], "/api/checkout", {"url": "shop.example", "plan": "monitor"})
    assert code == 400 and out["error"] == "That plan isn't available." and api["fake"].checkouts == []
    # with buyer emails on it is for sale, with both prices
    for k, v in (("SELFSERVE_TEST_ALLOW_LOOPBACK", "1"), ("MAIL_BACKEND", "capture"),
                 ("MAIL_FROM", "checkup@apex.example")):
        monkeypatch.setenv(k, v)
    by = {p["id"]: p for p in call(api["base"], "/api/plans")[2]["plans"]}
    assert by["monitor"]["available"] is True and (by["monitor"]["first_cents"], by["monitor"]["cents"]) == (2900, 1900)


@pytest.mark.parametrize("oid,mode", [(OID_PRO, "pro"), (OID_CHECKUP, "full"), (OID_MON, "full"), (OID_OLD, "full")])
def test_paid_report_uses_plan_mode_and_records_history(api, oid, mode):
    code, _, body = call(api["base"], "/api/report?order_id=" + oid)
    assert code == 200 and body["ok"]
    assert api["runs"] == [(mode, api["fake"].orders[oid]["website"])]
    assert server.RUN_TIMEOUT["pro"] == 150
    assert len(api["db"].history(oid)) == 1 and server._counters["reports_delivered"] == 1
    # a re-run soon after (e.g. after a restart) does not add a duplicate history row
    server._reports.clear()
    call(api["base"], "/api/report?order_id=" + oid)
    assert len(api["db"].history(oid)) == 1


def test_dashboard_checkup(api):
    db = api["db"]
    t = time.time() - 1000
    db.add_scan(OID_CHECKUP, "shop.example", "C", 70, [F1], at=t, plan="checkup")
    db.add_scan(OID_CHECKUP, "shop.example", "B", 80, [], at=t + 100, changes={"new": [], "fixed": [F1]},
                plan="checkup")
    code, h, body = call(api["base"], "/api/dashboard?order_id=" + OID_CHECKUP)
    assert code == 200 and h["Cache-Control"] == "no-store"
    assert body == {"ok": True, "site": "shop.example", "plan": "checkup", "refunded": False,
                    "report_path": "report.html?order_id=" + OID_CHECKUP, "subscription": None,
                    "history": [{"at": t + 100, "grade": "B", "score": 80}, {"at": t, "grade": "C", "score": 70}],
                    "latest_changes": {"new": [], "fixed": [F1]}}


def test_dashboard_monitor_and_old_orders(api):
    code, _, body = call(api["base"], "/api/dashboard?orderId=" + OID_MON)
    assert code == 200 and body["plan"] == "monitor" and body["history"] == [] and body["latest_changes"] is None
    assert body["subscription"] == {"status": "ACTIVE", "start_date": "2026-01-01",
                                    "charged_through_date": "2026-02-01", "canceled_date": None}
    code, _, body = call(api["base"], "/api/dashboard?order_id=" + OID_OLD)
    assert code == 200 and body["plan"] == "checkup" and body["subscription"] is None


def test_dashboard_refusals(api):
    assert call(api["base"], "/api/dashboard?order_id=bad!")[0] == 400
    assert call(api["base"], "/api/dashboard")[0] == 400
    assert call(api["base"], "/api/dashboard?order_id=ORD" + "Z" * 22)[0] == 402
    assert call(api["base"], "/api/dashboard?order_id=" + OID_REFUNDED)[0] == 402
    api["fake"].unavailable = True
    assert call(api["base"], "/api/dashboard?order_id=" + OID_CHECKUP)[0] == 503


def test_dashboard_rate_limited(api, monkeypatch):
    monkeypatch.setitem(server.LIMITS, "dash-ip", (3, 3600))
    codes = [call(api["base"], "/api/dashboard?order_id=" + OID_CHECKUP)[0] for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_cancel_only_for_monitor_orders(api):
    for oid in (OID_CHECKUP, OID_PRO, OID_OLD):
        code, _, body = call(api["base"], "/api/cancel", {"order_id": oid})
        assert code == 409 and not body["ok"]
    assert api["fake"].cancels == []
    code, h, body = call(api["base"], "/api/cancel", {"order_id": OID_MON})
    assert code == 200 and body == {"ok": True, "status": "ACTIVE", "ends": "2026-02-01"}
    assert api["fake"].cancels == [SUB] and h["Cache-Control"] == "no-store"
    # a second click doesn't cancel again
    code, _, body = call(api["base"], "/api/cancel", {"order_id": OID_MON})
    assert code == 200 and body["ends"] == "2026-02-01" and api["fake"].cancels == [SUB]


def test_cancel_refusals_and_limits(api, monkeypatch):
    assert call(api["base"], "/api/cancel", {"order_id": "../x"})[0] == 400
    assert call(api["base"], "/api/cancel", {})[0] == 400
    assert call(api["base"], "/api/cancel", {"order_id": "ORD" + "Z" * 22})[0] == 402
    api["fake"].sub = None
    api["fake"].subscription_for_order = lambda paid: None
    monkeypatch.setattr(square_api, "subscription_for_order", api["fake"].subscription_for_order)
    assert call(api["base"], "/api/cancel", {"order_id": OID_MON})[0] == 404
    codes = [call(api["base"], "/api/cancel", {"order_id": OID_MON})[0] for _ in range(5)]
    assert codes[-1] == 429                                   # 5 per order per hour
    api["fake"].unavailable = True
    assert call(api["base"], "/api/cancel", {"order_id": OID_CHECKUP})[0] == 503


@pytest.mark.parametrize("token", ["", "short-token", "x" * 23])
def test_admin_off_without_long_token(api, monkeypatch, token):
    monkeypatch.setenv("ADMIN_TOKEN", token)
    assert call(api["base"], "/api/admin/summary", headers=bearer(token))[0] == 404
    assert call(api["base"], "/api/admin/run-monitoring", method="POST", raw=b"", headers=bearer(token))[0] == 404
    assert api["fake"].summaries == []


@pytest.mark.parametrize("hdr", [None, "Bearer wrong-token-wrong-token-wrong", "Basic " + ADMIN, ADMIN,
                                 "Bearer " + ADMIN + "x", "Bearer " + ADMIN[:-1], "Bearer "])
def test_admin_wrong_token(api, hdr):
    h = {"Authorization": hdr} if hdr is not None else {}
    code, headers, body = call(api["base"], "/api/admin/summary", headers=h)
    assert code == 401 and body == {"ok": False, "error": "unauthorized"}
    assert headers["Cache-Control"] == "no-store"
    assert call(api["base"], "/api/admin/run-monitoring", method="POST", raw=b"", headers=h)[0] == 401
    assert api["fake"].summaries == [] and not monitor.is_running()


def test_admin_failures_are_rate_limited(api, monkeypatch):
    monkeypatch.setitem(server.LIMITS, "admin-fail", (3, 3600))
    codes = [call(api["base"], "/api/admin/summary", headers=bearer("nope" * 8))[0] for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    assert call(api["base"], "/api/admin/summary", headers=bearer())[0] == 429   # locked out for this address


def test_admin_summary(api, capture_mail):
    call(api["base"], "/api/report?order_id=" + OID_PRO)
    call(api["base"], "/api/checkout", {"url": "shop.example", "plan": "pro"})
    code, h, body = call(api["base"], "/api/admin/summary?days=7", headers={"Authorization": "bearer " + ADMIN})
    assert code == 200 and h["Cache-Control"] == "no-store"
    assert body["sales"]["mrr_cents"] == 1900 and api["fake"].summaries == [7]
    c = body["counters"]
    assert c["checkouts_started"] == 1 and c["reports_delivered"] == 1 and c["free_checks"] == 0 and c["since"]
    assert body["monitoring"] == {"running": False, "last_run": None}
    for q, want in (("?days=9999", 365), ("?days=0", 1), ("?days=abc", 30), ("", 30)):
        call(api["base"], "/api/admin/summary" + q, headers=bearer())
        assert api["fake"].summaries[-1] == want
    n = len(api["fake"].summaries)
    code, _, body = call(api["base"], "/api/admin/summary?sales=0", headers=bearer())
    assert code == 200 and "sales" not in body and len(api["fake"].summaries) == n
    api["fake"].unavailable = True
    code, _, body = call(api["base"], "/api/admin/summary", headers=bearer())
    assert code == 200 and body["sales"] is None and body["sales_error"]


def test_admin_run_monitoring_202_then_409(api, monkeypatch, capture_mail):
    gate = threading.Event()

    def slow(mode, url):
        gate.wait(10)
        return result(site="watch.example", findings=[F2])
    monkeypatch.setattr(server, "_run_runner", slow)          # monitoring takes paid workers itself
    code, h, body = call(api["base"], "/api/admin/run-monitoring", method="POST", raw=b"", headers=bearer())
    assert code == 202 and body == {"ok": True, "started": True} and h["Cache-Control"] == "no-store"
    code, _, body = call(api["base"], "/api/admin/run-monitoring", method="POST", raw=b"", headers=bearer())
    assert code == 409 and not body["ok"]
    code, _, body = call(api["base"], "/api/admin/summary?sales=0", headers=bearer())
    assert body["monitoring"]["running"] is True
    gate.set()
    assert wait_for(lambda: not monitor.is_running())
    code, _, body = call(api["base"], "/api/admin/summary?sales=0", headers=bearer())
    assert body["monitoring"]["last_run"]["counts"]["scanned"] == 1
    assert len(capture_mail) == 1 and capture_mail[0]["To"] == "owner@watch.example"
    # the report link now shows the monitoring result, without another run
    code, _, rep = call(api["base"], "/api/report?order_id=" + OID_MON)
    assert code == 200 and rep["report"]["findings"] == [F2]


def test_cors_allows_authorization_for_site_origin_only(api):
    code, h, _ = call(api["base"], "/api/admin/summary", method="OPTIONS")
    assert code == 204 and h["Access-Control-Allow-Origin"] == ORIGIN
    assert "Authorization" in h["Access-Control-Allow-Headers"]
    code, h, _ = call(api["base"], "/api/admin/summary", method="OPTIONS", origin="https://evil.example")
    assert "Access-Control-Allow-Origin" not in h and "Authorization" not in h["Access-Control-Allow-Headers"]
    code, h, _ = call(api["base"], "/api/admin/summary", method="OPTIONS", origin=None)
    assert "Authorization" not in h["Access-Control-Allow-Headers"]


def hook(base, event, sig="good"):
    return call(base, "/api/square/webhook", raw=json.dumps(event).encode(), origin=None,
                headers={"x-square-hmacsha256-signature": sig})


def test_webhook_payment_completed_prewarms_and_emails_once(api, capture_mail):
    ev = {"type": "payment.updated", "order_id": OID_PRO}
    assert hook(api["base"], ev, sig="bad")[0] == 401
    assert hook(api["base"], ev)[0] == 200
    assert wait_for(lambda: len(capture_mail) == 1)
    assert api["runs"] == [("pro", "https://pro.example/")]
    m = capture_mail[0]
    assert m["To"] == "buyer-ppp@example.com" and "pro.example" in m["Subject"]
    assert ORIGIN + "/dashboard.html?order_id=" + OID_PRO in m.get_content()
    for _ in range(3):                                         # Square retries the event
        assert hook(api["base"], ev)[0] == 200
    time.sleep(0.5)
    assert len(capture_mail) == 1 and len(api["runs"]) == 1


def test_webhook_other_events_do_nothing(api, capture_mail):
    assert hook(api["base"], {"type": "subscription.updated", "subscription_id": SUB})[0] == 200
    assert hook(api["base"], {"type": "refund.updated"})[0] == 200
    assert hook(api["base"], {"type": "payment.updated", "order_id": "../../x"})[0] == 200
    time.sleep(0.3)
    assert api["runs"] == [] and capture_mail == []


def test_webhook_with_mail_off_still_prewarms(api, monkeypatch):
    monkeypatch.delenv("MAIL_BACKEND", raising=False)
    assert hook(api["base"], {"type": "payment.updated", "order_id": OID_CHECKUP})[0] == 200
    assert wait_for(lambda: api["runs"])
    assert not api["db"].email_sent("report_ready:" + OID_CHECKUP)


def test_no_order_ids_tokens_or_emails_in_logs(api, capture_mail, capsys, monkeypatch):
    monkeypatch.setitem(server.LIMITS, "admin-fail", (50, 3600))
    call(api["base"], "/api/dashboard?order_id=" + OID_MON)
    call(api["base"], "/api/cancel", {"order_id": OID_MON})
    call(api["base"], "/api/admin/summary", headers=bearer())
    call(api["base"], "/api/admin/summary", headers=bearer("guess-" * 6))
    hook(api["base"], {"type": "payment.updated", "order_id": OID_PRO})
    wait_for(lambda: capture_mail)
    err = capsys.readouterr().err
    assert "/api/dashboard" in err                            # paths are logged...
    for secret in (OID_MON, OID_PRO, SUB, ADMIN, "buyer-ppp@example.com", "owner@watch.example"):
        assert secret not in err                              # ...never ids, tokens or emails


def test_startup_warns_on_short_admin_token(monkeypatch, capsys):
    monkeypatch.setenv("SQUARE_ENV", "sandbox")
    monkeypatch.delenv("SELFSERVE_TEST_ALLOW_LOOPBACK", raising=False)
    monkeypatch.setenv("SQUARE_WEBHOOK_SIGNATURE_KEY", "k")
    monkeypatch.setenv("ADMIN_TOKEN", "short")
    server.check_startup()
    err = capsys.readouterr().err
    assert "admin is off" in err and "short" not in err.replace("shorter", "")


def test_monitor_workflow_is_gated_on_secrets():
    repo = os.path.abspath(os.path.join(API, "..", ".."))
    yml = open(os.path.join(repo, ".github", "workflows", "apex-monitor.yml")).read()
    assert "schedule:" in yml and "workflow_dispatch:" in yml
    assert "secrets.APEX_API_BASE" in yml and "secrets.APEX_ADMIN_TOKEN" in yml
    assert yml.count("-X POST") == 1 and "/api/admin/run-monitoring" in yml
    assert "nothing to do" in yml and "exit 0" in yml
    assert "Bearer $APEX_ADMIN_TOKEN\" " not in yml            # token is never on a curl command line


# ---------------------------------------------------------------- review fixes (round 1)

def unreachable(oid):
    """The paid run could not reach the site: the automatic refund is allowed."""
    with server._lock:
        server._fatal[oid] = time.time()


def test_refund_of_monitor_order_cancels_subscription_first(api):
    unreachable(OID_MON)
    code, _, body = call(api["base"], "/api/refund", {"order_id": OID_MON})
    assert code == 200 and body == {"ok": True, "status": "succeeded"}
    assert api["fake"].events == ["cancel", "refund"] and api["fake"].cancels == [SUB]
    # one-time plans: no subscription involved
    unreachable(OID_CHECKUP)
    assert call(api["base"], "/api/refund", {"order_id": OID_CHECKUP})[0] == 200
    assert api["fake"].events == ["cancel", "refund", "refund"]


def test_refund_of_monitor_order_refused_when_cancel_fails(api, monkeypatch):
    unreachable(OID_MON)
    api["fake"].cancel_fails = True
    code, _, body = call(api["base"], "/api/refund", {"order_id": OID_MON})
    assert code == 503 and "renewal" in body["error"] and "nothing was refunded" in body["error"]
    assert api["fake"].refunds == []
    # no subscription found: refused too (a refund must never leave the renewal running)
    api["fake"].cancel_fails = False
    monkeypatch.setattr(square_api, "subscription_for_order", lambda paid: None)
    assert call(api["base"], "/api/refund", {"order_id": OID_MON})[0] == 503 and api["fake"].refunds == []
    # an already-cancelled subscription: the refund goes ahead without cancelling twice
    monkeypatch.setattr(square_api, "subscription_for_order",
                        lambda paid: dict(api["fake"].sub, canceled_date="2026-02-01"))
    assert call(api["base"], "/api/refund", {"order_id": OID_MON})[0] == 200
    assert api["fake"].refunds == [OID_MON] and api["fake"].cancels == []


def test_refunded_monitor_order_still_gets_dashboard_and_cancel(api):
    api["fake"].orders[OID_MON]["refunded"] = True
    code, _, body = call(api["base"], "/api/dashboard?order_id=" + OID_MON)
    assert code == 200 and body["refunded"] is True and body["plan"] == "monitor"
    assert body["report_path"] is None and body["subscription"]["status"] == "ACTIVE"
    code, _, body = call(api["base"], "/api/cancel", {"order_id": OID_MON})
    assert code == 200 and body["ends"] == "2026-02-01" and api["fake"].cancels == [SUB]
    assert call(api["base"], "/api/report?order_id=" + OID_MON)[0] == 402      # the report itself is gone


def test_no_cached_monitoring_result_for_refunded_orders(api, db):
    unreachable(OID_MON)
    assert call(api["base"], "/api/refund", {"order_id": OID_MON})[0] == 200
    server._monitor_result(OID_MON, result(site="watch.example"))     # a run that was already going
    assert server._cached(OID_MON) is None
    # and the monitoring run itself leaves a refunded order alone (Square says refunded)
    api["fake"].orders[OID_MON]["refunded"] = True
    got = []
    c = monitor.run_monitoring(lambda m, u: result(), now=JAN1, store=db, on_result=lambda o, r: got.append(o))
    assert c["sites"] == 0 and c["scanned"] == 0 and got == [] and db.last_scan(OID_MON) is None


def test_monitoring_with_a_fresh_store_every_day_emails_once_a_month(fake, capture_mail, tmp_path):
    """Render's free disk is wiped on every redeploy: the monthly marker lives on the Square order."""
    scans = []

    def run(mode, url):
        scans.append(url)
        return result(site="watch.example", findings=[F1])
    for day in range(75):
        st = store_mod.Store(str(tmp_path / ("day%d" % day) / "apex.sqlite3"))
        try:
            monitor.run_monitoring(run, now=JAN1 + day * DAY + 3600, store=st)
        finally:
            st.close()
    months = [w[2] for w in fake.state_writes]
    assert months == ["2026-01", "2026-02", "2026-03"] and len(capture_mail) == 3 and len(scans) == 3
    assert fake.state[OID_MON]["failing"] == ["1.1"]


def test_failed_monthly_send_is_retried_the_next_day(fake, capture_mail, tmp_path, monkeypatch):
    real_send, tries = mailer.send, []

    def flaky(kind, to, **kw):
        tries.append(kind)
        return False if len(tries) == 1 else real_send(kind, to, **kw)
    monkeypatch.setattr(mailer, "send", flaky)
    counts = []
    for day in range(4):
        st = store_mod.Store(str(tmp_path / ("d%d" % day) / "apex.sqlite3"))     # fresh disk each day
        counts.append(monitor.run_monitoring(lambda m, u: result(site="watch.example"),
                                             now=JAN1 + day * DAY, store=st))
        st.close()
    assert [c["scanned"] for c in counts] == [1, 1, 0, 0]
    assert [c["emails_sent"] for c in counts] == [0, 1, 0, 0]
    assert len(tries) == 2 and len(capture_mail) == 1 and len(fake.state_writes) == 1


def test_diff_never_calls_a_not_measured_check_fixed_or_new():
    prev = {"at": 1, "score": 70, "issues": [F1, F2], "not_measured": ["4.1"]}
    # 1.1 not measured now (not "fixed"); 4.1 failing now but not measured before (not "new")
    d = monitor.diff(prev, [F2, F3], "C", 70, ["1.1"])
    assert d["fixed"] == [] and d["new"] == []
    d = monitor.diff(prev, [F3], "C", 75, [])
    assert d["fixed"] == [F1, F2] and d["new"] == []
    res = result(findings=[F1])
    res["report"]["not_measured"] = [{"id": "3.7", "label": "x", "reason": "limit"}]
    res["report"]["pages"] = [{"path": "/a", "issues": [], "not_measured": [{"id": "p.h1"}]}]
    assert monitor.not_measured_of(res) == ["3.7", "page:/a:p.h1"]


def test_monitoring_diff_uses_the_emailed_baseline_from_square(fake, capture_mail, tmp_path):
    fake.state[OID_MON] = {"last": JAN1 - 30 * DAY, "month": "2025-12", "failing": ["1.1", "2.3"],
                           "not_measured": ["4.1"]}
    st = store_mod.Store(str(tmp_path / "x" / "apex.sqlite3"))              # empty disk: no local copy
    res = result(site="watch.example", findings=[F3])
    res["report"]["not_measured"] = [{"id": "2.3"}]
    res["report"]["passed"] = [{"id": "1.1", "title": "Page loads slowly"}]
    monitor.run_monitoring(lambda m, u: res, now=JAN1, store=st)
    ch = st.latest_changes(OID_MON)
    assert [i["id"] for i in ch["fixed"]] == ["1.1"] and ch["new"] == []    # 2.3 not measured, 4.1 was not
    assert ch["fixed"][0]["title"] == "Page loads slowly"
    st.close()


def test_buyer_report_views_do_not_reset_the_monitoring_clock(api, db, fake):
    code, _, _ = call(api["base"], "/api/report?order_id=" + OID_MON)            # the purchase report
    assert code == 200 and wait_for(lambda: fake.state_writes)
    assert len(fake.state_writes) == 1 and fake.state_writes[0][2] is None
    first_at = fake.state[OID_MON]["last"]
    with db._lock:                                                              # make it a month old
        db._db.execute("UPDATE scans SET at=at-? WHERE order_id=?", (26 * DAY, OID_MON))
    fake.state[OID_MON]["last"] = first_at - 26 * DAY
    server._reports.clear()
    assert call(api["base"], "/api/report?order_id=" + OID_MON)[0] == 200       # the buyer opens it again
    time.sleep(0.3)
    assert len(fake.state_writes) == 1                                          # clock not moved
    assert db.last_scan(OID_MON)["source"] == "view"
    assert db.baseline(OID_MON)["source"] == "report"                           # nor the baseline
    c = monitor.run_monitoring(lambda m, u: result(site="watch.example"), store=db)
    assert c["scanned"] == 1                                                    # due, despite the view


def test_report_ready_from_page_load_when_prewarm_was_busy_and_once_across_a_wipe(api, capture_mail, monkeypatch,
                                                                                     tmp_path):
    busy = {"on": True}

    def run(mode, url, pool=None):
        if busy["on"]:
            return dict(server.BUSY)
        return result(site="watch.example")
    monkeypatch.setattr(server, "run_checkup", run)
    assert hook(api["base"], {"type": "payment.updated", "order_id": OID_MON})[0] == 200
    time.sleep(0.5)
    assert capture_mail == []                                                   # prewarm got "busy"
    busy["on"] = False
    assert call(api["base"], "/api/report?order_id=" + OID_MON)[0] == 200       # the buyer's own page load
    assert wait_for(lambda: len(capture_mail) == 1)
    body = capture_mail[0].get_content()
    assert capture_mail[0]["To"] == "buyer-mmm@example.com"
    assert "$29 for the first month" in body and "then $19 a month" in body
    assert "renews every month until you cancel" in body and "end of the month you have already paid" in body
    assert ORIGIN + "/dashboard.html?order_id=" + OID_MON in body
    assert wait_for(lambda: OID_MON in api["fake"].ready)
    # restart on a wiped disk: no second email (the mark is on the Square order)
    fresh = store_mod.Store(str(tmp_path / "wiped" / "apex.sqlite3"))
    store_mod.set_default(fresh)
    with server._lock:
        server._reports.clear()
        server._ready_sent.clear()
    assert call(api["base"], "/api/report?order_id=" + OID_MON)[0] == 200
    assert hook(api["base"], {"type": "payment.updated", "order_id": OID_MON})[0] == 200
    time.sleep(0.5)
    assert len(capture_mail) == 1
    fresh.close()


def test_report_ready_for_one_time_plan_has_no_renewal_terms(api, capture_mail):
    assert call(api["base"], "/api/report?order_id=" + OID_CHECKUP)[0] == 200
    assert wait_for(lambda: len(capture_mail) == 1)
    assert "renews" not in capture_mail[0].get_content()


def test_a_waiting_buyer_gets_a_worker_before_the_next_monitoring_site(monkeypatch):
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setitem(server.POOLS, "paid", sem)
    order = []

    def runner(mode, url):
        order.append(url)
        time.sleep(0.2)
        return result()
    monkeypatch.setattr(server, "_run_runner", runner)
    assert sem.acquire(timeout=1)                         # a scan is running
    mon = threading.Thread(target=server.background_run_checkup, args=("full", "https://monitor.example/"))
    mon.start()
    time.sleep(0.4)                                       # monitoring is waiting for the worker
    buyer = threading.Thread(target=server.run_checkup, args=("full", "https://buyer.example/"))
    buyer.start()
    assert wait_for(lambda: server._paid_waiting == 1)
    sem.release()
    buyer.join(10)
    mon.join(10)
    assert order == ["https://buyer.example/", "https://monitor.example/"]
    assert server._paid_waiting == 0


def test_monitoring_pauses_between_sites(fake, db, monkeypatch, capture_mail):
    monkeypatch.setattr(monitor, "PAUSE_BETWEEN_S", 0.3)
    fake.sites = fake.sites + [dict(fake.sites[0], subscription_id="SUB2", order_id=OID_PRO,
                                    website="https://pro.example/")]
    times = []

    def run(mode, url):
        times.append(time.time())
        return result()
    assert monitor.run_monitoring(run, now=JAN1, store=db)["scanned"] == 2
    assert times[1] - times[0] >= 0.3


def test_webhook_prewarm_does_not_spend_the_paid_ip_bucket(api, monkeypatch):
    monkeypatch.setitem(server.LIMITS, "paid-ip", (1, 3600))
    for oid in (OID_CHECKUP, OID_PRO, OID_OLD):
        assert hook(api["base"], {"type": "payment.updated", "order_id": oid})[0] == 200
    assert wait_for(lambda: len(api["runs"]) == 3)
    assert call(api["base"], "/api/report?order_id=" + OID_MON)[0] == 200      # the buyer's own bucket is untouched


def test_webhook_for_a_non_apex_order_asks_square_once(api):
    other = "ORDSHOPSALE00000000000001"
    before = api["fake"].order_calls
    for _ in range(4):
        assert hook(api["base"], {"type": "payment.updated", "order_id": other})[0] == 200
        time.sleep(0.2)
    assert api["fake"].order_calls - before == 1 and api["runs"] == []


def test_find_emails_the_links_when_mail_is_on(api, capture_mail):
    fake = api["fake"]
    fake.found = [{"email": "buyer@shop.example", "order_id": OID_CHECKUP, "website": "https://shop.example/",
                   "created": JAN1}]
    fake.found_monitor = [{"email": "buyer@shop.example", "order_id": OID_MON, "website": "https://www.shop.example",
                           "created": JAN1 - 200 * DAY}]                   # older than 30 days: still found
    code, _, body = call(api["base"], "/api/find", {"email": "Buyer@Shop.example", "website": "shop.example"})
    assert code == 200 and body == {"ok": True, "emailed": True}
    assert wait_for(lambda: len(capture_mail) == 1)
    m = capture_mail[0]
    text = m.get_content()
    assert m["To"] == "buyer@shop.example" and m["Subject"] == "Your Apex Forge checkup links"
    for oid in (OID_CHECKUP, OID_MON):
        assert ORIGIN + "/report.html?order_id=" + oid in text and ORIGIN + "/dashboard.html?order_id=" + oid in text
    # nothing found: the very same answer, and no email
    for email, site in (("buyer@shop.example", "other.example"), ("nobody@shop.example", "shop.example")):
        code2, _, body2 = call(api["base"], "/api/find", {"email": email, "website": site})
        assert (code2, body2) == (code, body)
    time.sleep(0.4)
    assert len(capture_mail) == 1
    assert call(api["base"], "/api/find", {"email": "not an email", "website": "shop.example"})[0] == 400


def test_find_returns_links_when_mail_is_off(api, monkeypatch):
    monkeypatch.delenv("MAIL_BACKEND", raising=False)
    api["fake"].found = [{"email": "buyer@shop.example", "order_id": OID_CHECKUP,
                          "website": "https://shop.example/", "created": JAN1}]
    code, _, body = call(api["base"], "/api/find", {"email": "buyer@shop.example", "website": "shop.example"})
    assert code == 200 and body["reports"][0]["path"] == "report.html?order_id=" + OID_CHECKUP
    assert call(api["base"], "/api/find", {"email": "buyer@shop.example", "website": "x.example"})[0] == 404


def test_store_deletes_one_time_scans_after_30_days(db):
    now = time.time()
    db.add_scan(OID_CHECKUP, "shop.example", "B", 80, [], at=now - 31 * DAY, plan="checkup")
    db.add_scan(OID_PRO, "pro.example", "B", 80, [], at=now - 31 * DAY, plan="pro")
    db.add_scan(OID_OLD, "old.example", "B", 80, [], at=now - 31 * DAY)             # old row: no plan
    db.add_scan(OID_MON, "watch.example", "B", 80, [], at=now - 31 * DAY, plan="monitor")
    db.add_scan(OID_CHECKUP, "shop.example", "A", 90, [], at=now - 1 * DAY, plan="checkup")
    assert db.purge_old() == 3
    assert [h["score"] for h in db.history(OID_CHECKUP)] == [90]
    assert db.history(OID_PRO) == [] and db.history(OID_OLD) == [] and len(db.history(OID_MON)) == 1


def test_store_folder_and_file_are_private(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    old = os.umask(0o022)
    try:
        monkeypatch.setenv("APEX_DATA_DIR", str(d))
        s = store_mod.Store()
        s.add_scan(OID_MON, "watch.example", "B", 80, [], plan="monitor")
    finally:
        os.umask(old)
    assert os.stat(d).st_mode & 0o777 == 0o700
    for f in ("apex.sqlite3", "apex.sqlite3-wal", "apex.sqlite3-shm"):
        if os.path.exists(d / f):
            assert os.stat(d / f).st_mode & 0o777 == 0o600, f
    s.close()


def test_renewal_terms_one_price_reads_as_one_price():
    import mailer as _m
    lines = " ".join(_m.renewal_terms({"first_cents": 2900, "cents": 2900}))
    assert "$29.00 a month" in lines or "$29 a month" in lines
    assert "for the first month" not in lines and "then" not in lines.split("renews")[0]
    two = " ".join(_m.renewal_terms({"first_cents": 2900, "cents": 1900}))
    assert "for the first month" in two
