#!/usr/bin/env python3
"""server.py - Apex Forge Website Checkup API (stdlib only, one process).

Endpoints (all JSON unless noted; CORS allowed for SITE_ORIGIN only):
  GET  /health                         {"ok": true}  (the page calls this to wake a sleeping free server)
  POST /api/check   {"url": "..."}     free mini-check: grade, score, top 3 problem titles
  GET  /api/plans                      the plans on sale: [{"id","label","cents","kind","available"}]
  POST /api/checkout {"url", "plan"}   makes a Square hosted checkout for that website and plan
                                       (plan: checkup (default) | pro | monitor); returns its URL
  GET  /api/report?order_id=...        paid report for the website on that Square order
  GET  /api/report.pdf?order_id=...    the same report as a PDF (application/pdf)
  POST /api/refund  {"order_id": ...}  automatic full refund, only when the site could not be reached
  POST /api/find    {"email", "website"} find a lost report link (paid orders for that email and site)
  GET  /api/dashboard?order_id=...     the buyer's dashboard: plan, report link, scan history,
                                       latest changes, monitoring subscription
  POST /api/cancel  {"order_id": ...}  cancel Monthly Monitoring (Square ends it after the paid month)
  POST /api/square/webhook             Square payment events (signature checked); starts the report early
                                       and sends the buyer one "report ready" email
  GET  /api/admin/summary?days=30      owner only (Authorization: Bearer ADMIN_TOKEN): sales from Square,
                                       counters since start, the last monitoring run (sales=0: skip Square)
  POST /api/admin/run-monitoring       owner only: start the monthly re-checks in the background

Card data never reaches this server: buyers pay on Square's hosted checkout page.

Every audit runs in a separate short-lived process (runner.py) with the
network guard installed and without any secret in its environment.

Environment:
  PORT (default 10000), BIND (default 0.0.0.0)
  SITE_ORIGIN       e.g. https://apex-checkup.onrender.com (the static site); required for CORS
  TRUST_PROXY=1     take the client address from X-Forwarded-For (Render sets it). The first
                    entry is used: a client can fake it, which only weakens the per-address
                    free-check limit; the per-site and overall limits still cap the cost.
  CLIENT_IP_HEADER  optional: a header the host sets to the real client address (used first)
  FREE_WORKERS / PAID_WORKERS  concurrent checks of each kind (default 1 each)
  SQUARE_ACCESS_TOKEN, SQUARE_ENV, SQUARE_LOCATION_ID, SQUARE_WEBHOOK_SIGNATURE_KEY,
  SQUARE_WEBHOOK_URL, CHECKUP_PRICE_CENTS, PRO_PRICE_CENTS, MONITOR_PRICE_CENTS,
  SQUARE_MONITOR_PLAN_VARIATION_ID   see square_api.py
  ADMIN_TOKEN       owner token for /api/admin/*; admin is OFF unless it is at least 24 characters
  APEX_DATA_DIR     where scan history lives (store.py; best effort, wiped on a Render free redeploy)
  MAIL_BACKEND, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, MAIL_REPLY_TO   see mailer.py
"""
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict, defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import square_api as pay  # noqa: E402
import mailer  # noqa: E402
import monitor  # noqa: E402
import store as store_mod  # noqa: E402

RUNNER = os.path.join(HERE, "runner.py")
RENDER_PDF = os.path.join(REPO, "f1-audit-product", "tools", "render_pdf.py")
PY = sys.executable

MAX_BODY = 4096
POOLS = {"free": threading.BoundedSemaphore(int(os.environ.get("FREE_WORKERS", "1"))),
         "paid": threading.BoundedSemaphore(int(os.environ.get("PAID_WORKERS", "1")))}
RUN_TIMEOUT = {"light": 60, "full": 110, "pro": 150}
PLAN_MODE = {"checkup": "full", "pro": "pro", "monitor": "full"}    # runner mode per plan
PLAN_RE = re.compile(r"^[a-z_]{1,20}$")
ADMIN_MIN_LEN = 24
STARTED = time.time()
REPORT_TTL = 30 * 24 * 3600

# in-memory state (lost on restart; a paid report is simply re-run)
_lock = threading.Lock()
_reports = OrderedDict()        # order_id -> {"at", "result"}
_pdfs = OrderedDict()           # order_id -> bytes
_fatal = {}                     # order_id -> time the paid run could not reach the site
_hits = defaultdict(deque)      # rate-limit key -> deque of timestamps
_inflight = {}                  # order_id -> lock, so parallel loads share one run
_counters = {"free_checks": 0, "checkouts_started": 0, "reports_delivered": 0}   # since this process started
MAX_CACHE = 500

def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


LIMITS = {                      # key prefix -> (max events, window seconds)
    "free-ip": (_env_int("LIMIT_FREE_PER_IP", 5), 3600),
    "free-host": (_env_int("LIMIT_FREE_PER_SITE", 3), 3600),
    "free-all": (_env_int("LIMIT_FREE_TOTAL", 60), 3600),
    "paid-session": (4, 3600),
    "paid-ip": (30, 3600),      # report lookups that miss the cache (each asks Square)
    "refund-session": (3, 3600),
    "find-ip": (10, 3600),
    "find-email": (5, 3600),
    "checkout-ip": (_env_int("LIMIT_CHECKOUT_PER_IP", 10), 3600),
    "checkout-all": (_env_int("LIMIT_CHECKOUT_TOTAL", 200), 3600),
    "dash-ip": (60, 3600),      # dashboard loads (each asks Square)
    "cancel-ip": (10, 3600),
    "cancel-session": (5, 3600),
    "admin-fail": (_env_int("LIMIT_ADMIN_FAILS", 10), 3600),   # wrong admin tokens per address
}


def allow(kind, key, now=None):
    now = now or time.time()
    n, win = LIMITS[kind]
    with _lock:
        q = _hits[(kind, key)]
        while q and now - q[0] > win:
            q.popleft()
        if len(q) >= n:
            return False
        q.append(now)
        return True


def blocked(kind, key, now=None):
    """True when this key already used up its limit (does not count as an event)."""
    now = now or time.time()
    n, win = LIMITS[kind]
    with _lock:
        q = _hits[(kind, key)]
        while q and now - q[0] > win:
            q.popleft()
        return len(q) >= n


def bump(name):
    with _lock:
        _counters[name] += 1


def data():
    """The scan-history store, or None when the disk can't be used (never breaks a request)."""
    try:
        return store_mod.default()
    except Exception as e:
        sys.stderr.write(f"store unavailable: {type(e).__name__}\n")
        return None


def plan_of(paid):
    """Old orders have no plan field: they are Website Checkups."""
    p = paid.get("plan") or "checkup"
    return p if p in PLAN_MODE else "checkup"


def _cache_put(store, key, val):
    with _lock:
        store[key] = val
        store.move_to_end(key)
        while len(store) > MAX_CACHE:
            store.popitem(last=False)


def worker_env():
    """Only what the runner needs. No Square token, no proxies."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
           "HOME": tempfile.gettempdir(), "PYTHONDONTWRITEBYTECODE": "1"}
    if os.environ.get("SELFSERVE_TEST_ALLOW_LOOPBACK") == "1":
        env["SELFSERVE_TEST_ALLOW_LOOPBACK"] = "1"
    return env


def run_checkup(mode, url, pool=None):
    """Returns the runner's JSON, or {"ok": False, "busy": True} when full.
    Free checks and paid reports have separate worker pools, so a burst of
    free checks can never keep a paying buyer waiting."""
    sem = POOLS[pool or ("free" if mode == "light" else "paid")]
    if not sem.acquire(timeout=30 if mode == "light" else 90):
        return {"ok": False, "busy": True, "error": "The checker is busy. Try again in a minute."}
    try:
        p = subprocess.run([PY, "-I", RUNNER, mode, url], capture_output=True, text=True,
                           timeout=RUN_TIMEOUT[mode], env=worker_env(), cwd=HERE)
        line = (p.stdout or "").strip().splitlines()
        if p.returncode != 0 or not line:
            return {"ok": False, "error": "The check failed unexpectedly. Try again in a minute."}
        return json.loads(line[-1])
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "The website took too long to answer. Try again in a minute."}
    finally:
        sem.release()


def render_pdf(markdown):
    with tempfile.TemporaryDirectory() as d:
        md, pdf = os.path.join(d, "report.md"), os.path.join(d, "report.pdf")
        with open(md, "w") as f:
            f.write(markdown)
        p = subprocess.run([PY, "-I", RENDER_PDF, md, pdf], capture_output=True, text=True,
                           timeout=60, env=worker_env())
        if p.returncode != 0 or not os.path.exists(pdf) or os.path.getsize(pdf) < 2000:
            return None
        with open(pdf, "rb") as f:
            return f.read()


def public_report(sid, res):
    keep = ("site", "url", "final_url", "redirects", "requests_made", "score", "grade", "measured_at", "report")
    out = {k: res[k] for k in keep if k in res}
    out.update(ok=True, pdf_url="/api/report.pdf?order_id=" + sid)
    return out


def _cached(sid):
    with _lock:
        hit = _reports.get(sid)
    if hit and time.time() - hit["at"] < REPORT_TTL:
        return hit
    return None


def paid_result(sid, ip="-"):
    """(http status, payload) for a report request. Re-runs if not cached."""
    hit = _cached(sid)
    if hit:
        return 200, public_report(sid, hit["result"])
    with _lock:
        lk = _inflight.setdefault(sid, threading.Lock())
    with lk:                     # a second tab waits for the first run instead of starting another
        hit = _cached(sid)
        if hit:
            return 200, public_report(sid, hit["result"])
        return _paid_run(sid, ip)


def _paid_run(sid, ip):
    if not allow("paid-ip", ip):
        return 429, {"ok": False, "error": "Too many report requests. Try again in an hour."}
    try:
        sess = pay.get_paid_order(sid)
    except pay.PaymentError as e:
        return 402, {"ok": False, "error": str(e)}
    except pay.SquareUnavailable:
        return 503, {"ok": False, "error": "We couldn't confirm the payment just now. Try again in a minute."}
    if sess.get("refunded"):
        return 402, {"ok": False, "error": "This payment was refunded, so this report link no longer works."}
    if not allow("paid-session", sid):
        return 429, {"ok": False, "error": "This report was just run several times. Try again in an hour."}
    res = run_checkup(PLAN_MODE[plan_of(sess)], sess["website"])
    if res.get("busy"):
        return 503, {"ok": False, "error": res["error"]}
    if res.get("input_error") or res.get("fatal"):
        with _lock:
            _fatal[sid] = time.time()
        msg = res.get("error") or "We couldn't load that website."
        return 422, {"ok": False, "error": msg + " You can try again, or refund yourself automatically below.",
                     "refundable": True, "site": res.get("site")}
    if not res.get("ok"):
        return 503, {"ok": False, "error": res.get("error", "The check failed. Try again in a minute.")}
    with _lock:
        _fatal.pop(sid, None)
    if not sess.get("delivered") and not pay.mark_delivered(sess):
        sys.stderr.write("warning: could not record report delivery on the payment\n")
    _cache_put(_reports, sid, {"at": time.time(), "result": res})
    bump("reports_delivered")
    record_scan(sid, res, "report")
    return 200, public_report(sid, res)


def record_scan(sid, res, source):
    """Best effort: add this report to the order's history (skipped if one was saved in the last 6 hours,
    e.g. the same report re-run after a restart)."""
    st = data()
    if st is None:
        return
    try:
        last = st.last_scan(sid)
        if last and time.time() - last["at"] < 6 * 3600:
            return
        st.add_scan(sid, res.get("site") or "", res.get("grade"), res.get("score"), monitor.issues_of(res),
                    source=source)
    except Exception as e:
        sys.stderr.write(f"could not save scan history: {type(e).__name__}\n")


def do_refund(sid):
    if not allow("refund-session", sid):
        return 429, {"ok": False, "error": "Too many refund attempts. Try again in an hour."}
    try:
        sess = pay.get_paid_order(sid)
    except pay.PaymentError as e:
        return 402, {"ok": False, "error": str(e)}
    except pay.SquareUnavailable:
        return 503, {"ok": False, "error": "We couldn't reach the payment system. Try again in a minute."}
    if sess.get("refunded"):
        _forget(sid)
        return 200, {"ok": True, "status": "succeeded"}
    with _lock:
        failed_at = _fatal.get(sid)
        have_report = sid in _reports
    if have_report or sess.get("delivered"):
        return 409, {"ok": False, "error": "Your report was delivered, so there is no automatic refund."}
    if not failed_at:
        # e.g. the server restarted: confirm the site is still unreachable first
        res = run_checkup("light", sess["website"], pool="paid")
        if res.get("busy"):
            return 503, {"ok": False, "error": res["error"]}
        if res.get("ok"):
            return 409, {"ok": False, "error": "Your website can be reached now. Reload this page to get your report."}
        if not (res.get("fatal") or res.get("input_error")):
            return 503, {"ok": False, "error": "We couldn't confirm the problem just now. Try again in a minute."}
    try:
        status = pay.refund(sess)
    except pay.PaymentError as e:
        return 402, {"ok": False, "error": str(e)}
    except pay.SquareUnavailable:
        return 503, {"ok": False, "error": "The refund didn't go through just now. Try again in a minute."}
    _forget(sid)
    return 200, {"ok": True, "status": status}


def _forget(sid):
    with _lock:
        _reports.pop(sid, None)
        _pdfs.pop(sid, None)
        _fatal.pop(sid, None)


def site_key(raw):
    """The site a check is for, as the runner will see it (for per-site limits)."""
    try:
        return (urlparse(runner_normalize(raw)).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def runner_normalize(raw):
    import runner
    return runner.normalize_url(raw)


def find_reports(email, website):
    try:
        found = pay.find_orders(email)
    except pay.PaymentError as e:
        return 400, {"ok": False, "error": str(e)}
    except pay.SquareUnavailable:
        return 503, {"ok": False, "error": "We couldn't reach the payment system. Try again in a minute."}
    want = site_key(website)
    hits = [f for f in found if want and site_key(f["website"]) == want]
    if not hits:
        return 404, {"ok": False, "error": "We couldn't find a report for that email and website in the last 30 days. "
                                           "Use the email you typed at checkout."}
    return 200, {"ok": True, "reports": [
        {"site": want, "created": h["created"], "path": "report.html?order_id=" + h["order_id"]}
        for h in hits[:5]]}


def start_checkout(raw_url, plan="checkup"):
    """Normalize the website first, so a buyer only ever pays for an address we can check."""
    if not PLAN_RE.match(plan or "") or plan not in PLAN_MODE:
        return 400, {"ok": False, "error": "That plan isn't available."}
    try:
        url = runner_normalize(raw_url)
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}
    origin = os.environ.get("SITE_ORIGIN", "").rstrip("/")
    if not origin:
        return 503, {"ok": False, "error": "Checkout isn't switched on yet."}
    # "checkup" is create_checkout's default plan, so it is not passed (keeps older square_api working)
    kw = {} if plan == "checkup" else {"plan": plan}
    try:
        link, order_id = pay.create_checkout(url, origin + "/report.html", **kw)
    except pay.PaymentError:
        return 400, {"ok": False, "error": "That plan isn't available."}
    except pay.SquareUnavailable:
        return 503, {"ok": False, "error": "We couldn't open the checkout just now. Try again in a minute."}
    bump("checkouts_started")
    return 200, {"ok": True, "checkout_url": link, "order_id": order_id}


def site_host(website):
    try:
        return (urlparse(website).hostname or "").lower()
    except ValueError:
        return ""


def public_subscription(sub):
    if not sub:
        return None
    keep = ("status", "start_date", "charged_through_date", "canceled_date")
    return {k: sub.get(k) for k in keep}


def dashboard(sid):
    if not pay.ORDER_RE.match(sid or ""):
        return 400, {"ok": False, "error": "missing payment reference"}
    try:
        sess = pay.get_paid_order(sid)
        if sess.get("refunded"):
            return 402, {"ok": False, "error": "This payment was refunded, so this link no longer works."}
        plan = plan_of(sess)
        sub = pay.subscription_for_order(sess) if plan == "monitor" else None
    except pay.PaymentError as e:
        return 402, {"ok": False, "error": str(e)}
    except pay.SquareUnavailable:
        return 503, {"ok": False, "error": "We couldn't reach the payment system. Try again in a minute."}
    history, changes = [], None
    st = data()
    if st is not None:
        try:
            history, changes = st.history(sid), st.latest_changes(sid)
        except Exception as e:
            sys.stderr.write(f"could not read scan history: {type(e).__name__}\n")
    return 200, {"ok": True, "site": site_host(sess["website"]), "plan": plan,
                 "report_path": "report.html?order_id=" + sid,
                 "subscription": public_subscription(sub), "history": history, "latest_changes": changes}


def cancel(sid):
    if not allow("cancel-session", sid):
        return 429, {"ok": False, "error": "Too many attempts. Try again in an hour."}
    try:
        sess = pay.get_paid_order(sid)
        if plan_of(sess) != "monitor":
            return 409, {"ok": False, "error": "This purchase isn't a monitoring plan, so there is nothing to cancel."}
        sub = pay.subscription_for_order(sess)
        if not sub or not sub.get("id"):
            return 404, {"ok": False, "error": "We couldn't find the monitoring subscription for this purchase."}
        if sub.get("canceled_date") or str(sub.get("status") or "").upper() in ("CANCELED", "DEACTIVATED"):
            return 200, {"ok": True, "status": sub.get("status") or "CANCELED",
                         "ends": sub.get("canceled_date") or sub.get("charged_through_date")}
        out = pay.cancel_subscription(sub["id"])
    except pay.PaymentError as e:
        return 402, {"ok": False, "error": str(e)}
    except pay.SquareUnavailable:
        return 503, {"ok": False, "error": "We couldn't reach the payment system. Try again in a minute."}
    return 200, {"ok": True, "status": out.get("status"), "ends": out.get("ends")}


def admin_token():
    t = os.environ.get("ADMIN_TOKEN", "")
    return t if len(t) >= ADMIN_MIN_LEN else None


def admin_check(auth_header, ip):
    """None when allowed, else (status, payload). Admin is off unless ADMIN_TOKEN is long enough."""
    token = admin_token()
    if token is None:
        return 404, {"ok": False, "error": "not found"}
    if blocked("admin-fail", ip):
        return 429, {"ok": False, "error": "Too many attempts. Try again later."}
    given = auth_header or ""
    given = given[7:].strip() if given[:7].lower() == "bearer " else ""
    if given and hmac.compare_digest(given.encode(), token.encode()):
        return None
    allow("admin-fail", ip)               # count the failure
    return 401, {"ok": False, "error": "unauthorized"}


def monitoring_state():
    st = data()
    last = None
    if st is not None:
        try:
            last = st.last_run()
        except Exception:
            last = None
    return {"running": monitor.is_running(), "last_run": last}


def admin_summary(days_raw, with_sales=True):
    try:
        days = max(1, min(int(days_raw or 30), 365))
    except ValueError:
        days = 30
    with _lock:
        counters = dict(_counters, since=STARTED)
    out = {"ok": True, "counters": counters, "monitoring": monitoring_state()}
    if with_sales:
        try:
            out["sales"] = pay.sales_summary(days=days)
        except (pay.SquareUnavailable, pay.PaymentError):
            out["sales"] = None
            out["sales_error"] = "We couldn't reach Square just now."
    return 200, out


def _monitor_result(sid, res):
    """A monitoring re-check finished: the report link now shows the new report."""
    _cache_put(_reports, sid, {"at": time.time(), "result": res})
    with _lock:
        _pdfs.pop(sid, None)


def start_monitoring():
    if monitor.start_in_background(run_checkup, store=data(), on_result=_monitor_result):
        return 202, {"ok": True, "started": True}
    return 409, {"ok": False, "error": "A monitoring run is already going."}


def send_report_ready(order_id):
    """Email the buyer that the report is ready, once per order. Never raises."""
    if not mailer.enabled():
        return False
    st = data()
    if st is None:
        return False
    key = "report_ready:" + order_id
    try:
        if st.email_sent(key):
            return False
        sess = pay.get_paid_order(order_id)
        to = (sess.get("buyer_email") or "").strip()
        if sess.get("refunded") or not mailer.valid_address(to) or not st.claim_email(key):
            return False
        ok = mailer.send("report_ready", to, order_id=order_id, site=site_host(sess["website"]),
                         plan=plan_of(sess))
        if not ok:
            st.release_email(key)
        return ok
    except Exception as e:
        sys.stderr.write(f"report email skipped: {type(e).__name__}\n")
        return False


def prewarm(order_id):
    """A payment completed: build the report now, so it's ready when the buyer lands,
    then email the buyer once that it is ready."""
    try:
        code, _ = paid_result(order_id, "webhook")
        if code == 200:
            send_report_ready(order_id)
    except Exception as e:  # a background run must never take the server down
        sys.stderr.write(f"prewarm failed: {type(e).__name__}\n")


def event_kind(event):
    f = getattr(pay, "event_kind", None)
    if f is not None:
        return f(event)
    oid = pay.paid_order_id_from_event(event)       # older square_api
    return ("payment_completed", oid) if oid else (None, None)


def handle_webhook(raw, signature):
    if not pay.verify_webhook(raw, signature):
        return 401, {"ok": False}
    try:
        event = json.loads(raw.decode())
    except Exception:
        return 400, {"ok": False}
    kind, oid = event_kind(event)
    if kind == "payment_completed" and oid and pay.ORDER_RE.match(oid):
        if _cached(oid) is None or (mailer.enabled() and not _emailed(oid)):
            threading.Thread(target=prewarm, args=(oid,), daemon=True).start()
    return 200, {"ok": True}


def _emailed(oid):
    st = data()
    try:
        return st is None or st.email_sent("report_ready:" + oid)
    except Exception:
        return True


def client_ip(handler):
    hdr = os.environ.get("CLIENT_IP_HEADER", "")
    if hdr and handler.headers.get(hdr):
        return handler.headers.get(hdr).strip()[:64]
    if os.environ.get("TRUST_PROXY") == "1":
        xff = handler.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return handler.client_address[0]


class Handler(BaseHTTPRequestHandler):
    server_version = "ApexCheckup/1"
    sys_version = ""

    def log_message(self, fmt, *args):  # path only, never the query (order ids)
        try:
            path = urlparse(getattr(self, "path", "") or "").path[:80]
        except Exception:
            path = "?"
        code = args[1] if len(args) > 1 and isinstance(args[1], (str, int)) else ""
        sys.stderr.write("%s %s %s\n" % (getattr(self, "command", None) or "-", path, code))

    def _cors(self):
        origin = os.environ.get("SITE_ORIGIN", "")
        if origin and self.headers.get("Origin") == origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send(self, code, payload, ctype="application/json", extra=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return None
        try:
            d = json.loads(self.rfile.read(n).decode())
        except Exception:
            return None
        return d if isinstance(d, dict) else None

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        origin = os.environ.get("SITE_ORIGIN", "")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        # the owner's admin page sends its token in Authorization; only our own site may
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization" if origin and self.headers.get("Origin") == origin
                         else "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)
        sid = (q.get("order_id") or q.get("orderId") or [""])[0]
        if p.path in ("/", "/health"):
            return self._send(200, {"ok": True, "service": "apex-checkup-api"})
        if p.path == "/api/plans":
            try:
                plans = pay.public_plans()
            except (pay.SquareUnavailable, pay.PaymentError):
                return self._send(503, {"ok": False, "error": "Plans are not available just now."})
            return self._send(200, {"ok": True, "plans": plans})
        if p.path == "/api/dashboard":
            if not pay.ORDER_RE.match(sid):
                return self._send(400, {"ok": False, "error": "missing payment reference"})
            if not allow("dash-ip", client_ip(self)):
                return self._send(429, {"ok": False, "error": "Too many requests. Try again in an hour."})
            code, payload = dashboard(sid)
            return self._send(code, payload)
        if p.path == "/api/admin/summary":
            denied = admin_check(self.headers.get("Authorization"), client_ip(self))
            if denied:
                return self._send(*denied, extra={"Vary": "Authorization"})
            code, payload = admin_summary((q.get("days") or ["30"])[0],
                                          with_sales=(q.get("sales") or ["1"])[0] != "0")
            return self._send(code, payload, extra={"Vary": "Authorization"})
        if p.path == "/api/report":
            code, payload = paid_result(sid, client_ip(self))
            return self._send(code, payload)
        if p.path == "/api/report.pdf":
            if not pay.ORDER_RE.match(sid):
                return self._send(400, {"ok": False, "error": "missing payment reference"})
            with _lock:
                pdf = _pdfs.get(sid)
            if pdf is None:
                code, payload = paid_result(sid, client_ip(self))
                if code != 200:
                    return self._send(code, payload)
                with _lock:
                    md = _reports[sid]["result"]["markdown"]
                pdf = render_pdf(md)
                if pdf is None:
                    return self._send(503, {"ok": False, "error": "The PDF couldn't be made just now. Use Print on the report page."})
                _cache_put(_pdfs, sid, pdf)
            fname = re.sub(r"[^a-z0-9.-]+", "-", _reports.get(sid, {}).get("result", {}).get("site", "website"))
            return self._send(200, pdf, "application/pdf",
                              {"Content-Disposition": f'attachment; filename="apex-checkup-{fname}.pdf"'})
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        p = urlparse(self.path)
        if p.path == "/api/square/webhook":
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0 or n > 65536:
                return self._send(400, {"ok": False})
            code, payload = handle_webhook(self.rfile.read(n),
                                           self.headers.get("x-square-hmacsha256-signature", ""))
            return self._send(code, payload)
        if p.path == "/api/admin/run-monitoring":          # no body needed
            denied = admin_check(self.headers.get("Authorization"), client_ip(self))
            if denied:
                return self._send(*denied, extra={"Vary": "Authorization"})
            code, payload = start_monitoring()
            return self._send(code, payload, extra={"Vary": "Authorization"})
        body = self._json_body()
        if body is None:
            return self._send(400, {"ok": False, "error": "bad request"})
        if p.path == "/api/check":
            url = str(body.get("url") or "")[:300]
            ip = client_ip(self)
            host = site_key(url)
            if not allow("free-ip", ip):
                return self._send(429, {"ok": False, "error": "You've run several free checks this hour. Try again later."})
            if host and not allow("free-host", host):
                return self._send(429, {"ok": False, "error": "That website was checked several times this hour. Try again later."})
            if not allow("free-all", "*"):
                return self._send(503, {"ok": False, "error": "The free checker is busy. Try again later."})
            res = run_checkup("light", url)
            if res.get("busy"):
                return self._send(503, {"ok": False, "error": res["error"]})
            if res.get("input_error"):
                return self._send(400, {"ok": False, "error": res["error"]})
            if not res.get("ok"):
                return self._send(422, {"ok": False, "error": res.get("error", "We couldn't load that website.")})
            bump("free_checks")
            keep = ("site", "score", "grade", "top", "failing", "passed", "not_measured", "sections", "measured_at")
            return self._send(200, dict({k: res[k] for k in keep if k in res}, ok=True))
        if p.path == "/api/find":
            email = str(body.get("email") or "").strip().lower()[:254]
            website = str(body.get("website") or "")[:300]
            if not allow("find-ip", client_ip(self)) or not allow("find-email", email):
                return self._send(429, {"ok": False, "error": "Too many lookups. Try again in an hour."})
            code, payload = find_reports(email, website)
            return self._send(code, payload)
        if p.path == "/api/checkout":
            if not allow("checkout-ip", client_ip(self)) or not allow("checkout-all", "*"):
                return self._send(429, {"ok": False, "error": "Too many checkouts started. Try again later."})
            code, payload = start_checkout(str(body.get("url") or "")[:300], str(body.get("plan") or "checkup")[:20])
            return self._send(code, payload)
        if p.path == "/api/cancel":
            sid = str(body.get("order_id") or "")
            if not pay.ORDER_RE.match(sid):
                return self._send(400, {"ok": False, "error": "missing payment reference"})
            if not allow("cancel-ip", client_ip(self)):
                return self._send(429, {"ok": False, "error": "Too many attempts. Try again in an hour."})
            code, payload = cancel(sid)
            return self._send(code, payload)
        if p.path == "/api/refund":
            sid = str(body.get("order_id") or "")
            if not pay.ORDER_RE.match(sid):
                return self._send(400, {"ok": False, "error": "missing payment reference"})
            code, payload = do_refund(sid)
            return self._send(code, payload)
        return self._send(404, {"ok": False, "error": "not found"})


def check_startup():
    if pay.env_name() is None:
        raise SystemExit("refusing to start: SQUARE_ENV must be sandbox or production")
    if os.environ.get("SELFSERVE_TEST_ALLOW_LOOPBACK") == "1" and pay.env_name() == "production":
        raise SystemExit("refusing to start: test loopback mode with production Square")
    if not os.environ.get("SQUARE_WEBHOOK_SIGNATURE_KEY"):
        sys.stderr.write("warning: no webhook signature key, so reports start when the buyer lands\n")
    if os.environ.get("ADMIN_TOKEN") and admin_token() is None:
        sys.stderr.write(f"warning: ADMIN_TOKEN is shorter than {ADMIN_MIN_LEN} characters, so admin is off\n")
    if mailer.backend() != "off" and not mailer.enabled():
        sys.stderr.write("warning: MAIL_BACKEND is on but SMTP_HOST, MAIL_FROM or SITE_ORIGIN is missing; no email is sent\n")


def main():
    check_startup()
    port = int(os.environ.get("PORT", "10000"))
    srv = ThreadingHTTPServer((os.environ.get("BIND", "0.0.0.0"), port), Handler)
    srv.daemon_threads = True
    sys.stderr.write(f"apex checkup api on :{port}\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
