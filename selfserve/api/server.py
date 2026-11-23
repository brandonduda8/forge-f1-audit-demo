#!/usr/bin/env python3
"""server.py - Apex Forge Website Checkup API (stdlib only, one process).

Endpoints (all JSON unless noted; CORS allowed for SITE_ORIGIN only):
  GET  /health                         {"ok": true}  (the page calls this to wake a sleeping free server)
  POST /api/check   {"url": "..."}     free mini-check: grade, score, top 3 problem titles
  GET  /api/report?session_id=cs_...   paid report for the website typed at checkout
  GET  /api/report.pdf?session_id=...  the same report as a PDF (application/pdf)
  POST /api/refund  {"session_id": ..} automatic full refund, only when the site could not be reached

Every audit runs in a separate short-lived process (runner.py) with the
network guard installed and without any secret in its environment.

Environment:
  PORT (default 10000), BIND (default 0.0.0.0)
  SITE_ORIGIN       e.g. https://apex-checkup.onrender.com (the static site); required for CORS
  TRUST_PROXY=1     take the client address from X-Forwarded-For (Render sets it)
  STRIPE_SECRET_KEY, STRIPE_PAYMENT_LINK_ID, STRIPE_WEBSITE_FIELD  see stripe_api.py
"""
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
import stripe_api  # noqa: E402

RUNNER = os.path.join(HERE, "runner.py")
RENDER_PDF = os.path.join(REPO, "f1-audit-product", "tools", "render_pdf.py")
PY = sys.executable

MAX_BODY = 4096
WORKERS = threading.BoundedSemaphore(int(os.environ.get("CHECKUP_WORKERS", "2")))
RUN_TIMEOUT = {"light": 60, "full": 110}
REPORT_TTL = 30 * 24 * 3600

# in-memory state (lost on restart; a paid report is simply re-run)
_lock = threading.Lock()
_reports = OrderedDict()        # session_id -> {"at", "result"}
_pdfs = OrderedDict()           # session_id -> bytes
_fatal = {}                     # session_id -> time the paid run could not reach the site
_hits = defaultdict(deque)      # rate-limit key -> deque of timestamps
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
    "paid-ip": (30, 3600),      # report lookups that miss the cache (each asks Stripe)
    "refund-session": (3, 3600),
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


def _cache_put(store, key, val):
    with _lock:
        store[key] = val
        store.move_to_end(key)
        while len(store) > MAX_CACHE:
            store.popitem(last=False)


def worker_env():
    """Only what the runner needs. No Stripe key, no proxies."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
           "HOME": tempfile.gettempdir(), "PYTHONDONTWRITEBYTECODE": "1"}
    if os.environ.get("SELFSERVE_TEST_ALLOW_LOOPBACK") == "1":
        env["SELFSERVE_TEST_ALLOW_LOOPBACK"] = "1"
    return env


def run_checkup(mode, url):
    """Returns the runner's JSON, or {"ok": False, "busy": True} when full."""
    if not WORKERS.acquire(timeout=30):
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
        WORKERS.release()


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
    keep = ("site", "url", "score", "grade", "measured_at", "report")
    out = {k: res[k] for k in keep if k in res}
    out.update(ok=True, pdf_url="/api/report.pdf?session_id=" + sid)
    return out


def paid_result(sid, ip="-"):
    """(http status, payload) for a report request. Re-runs if not cached."""
    with _lock:
        hit = _reports.get(sid)
    if hit and time.time() - hit["at"] < REPORT_TTL:
        return 200, public_report(sid, hit["result"])
    if not allow("paid-ip", ip):
        return 429, {"ok": False, "error": "Too many report requests. Try again in an hour."}
    try:
        sess = stripe_api.get_paid_session(sid)
    except stripe_api.PaymentError as e:
        return 402, {"ok": False, "error": str(e)}
    except stripe_api.StripeUnavailable:
        return 503, {"ok": False, "error": "We couldn't confirm the payment just now. Try again in a minute."}
    if not allow("paid-session", sid):
        return 429, {"ok": False, "error": "This report was just run several times. Try again in an hour."}
    res = run_checkup("full", sess["website"])
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
    _cache_put(_reports, sid, {"at": time.time(), "result": res})
    return 200, public_report(sid, res)


def do_refund(sid):
    if not allow("refund-session", sid):
        return 429, {"ok": False, "error": "Too many refund attempts. Try again in an hour."}
    try:
        sess = stripe_api.get_paid_session(sid)
    except stripe_api.PaymentError as e:
        return 402, {"ok": False, "error": str(e)}
    except stripe_api.StripeUnavailable:
        return 503, {"ok": False, "error": "We couldn't reach the payment system. Try again in a minute."}
    with _lock:
        failed_at = _fatal.get(sid)
        have_report = sid in _reports
    if have_report:
        return 409, {"ok": False, "error": "Your report was delivered, so there is no automatic refund."}
    if not failed_at:
        # e.g. the server restarted: confirm the site is still unreachable first
        res = run_checkup("light", sess["website"])
        if res.get("busy"):
            return 503, {"ok": False, "error": res["error"]}
        if res.get("ok"):
            return 409, {"ok": False, "error": "Your website can be reached now. Reload this page to get your report."}
        if not (res.get("fatal") or res.get("input_error")):
            return 503, {"ok": False, "error": "We couldn't confirm the problem just now. Try again in a minute."}
    try:
        status = stripe_api.refund(sess["payment_intent"], sid)
    except stripe_api.PaymentError as e:
        return 402, {"ok": False, "error": str(e)}
    except stripe_api.StripeUnavailable:
        return 503, {"ok": False, "error": "The refund didn't go through just now. Try again in a minute."}
    return 200, {"ok": True, "status": status}


def client_ip(handler):
    if os.environ.get("TRUST_PROXY") == "1":
        xff = handler.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return handler.client_address[0]


class Handler(BaseHTTPRequestHandler):
    server_version = "ApexCheckup/1"
    sys_version = ""

    def log_message(self, fmt, *args):  # path only, never the query (session ids)
        sys.stderr.write("%s %s %s\n" % (self.command, urlparse(self.path).path, args[1] if len(args) > 1 else ""))

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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)
        sid = (q.get("session_id") or [""])[0]
        if p.path in ("/", "/health"):
            return self._send(200, {"ok": True, "service": "apex-checkup-api"})
        if p.path == "/api/report":
            code, payload = paid_result(sid, client_ip(self))
            return self._send(code, payload)
        if p.path == "/api/report.pdf":
            if not stripe_api.SESSION_RE.match(sid):
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
        body = self._json_body()
        if body is None:
            return self._send(400, {"ok": False, "error": "bad request"})
        if p.path == "/api/check":
            url = str(body.get("url") or "")[:300]
            ip = client_ip(self)
            host = re.sub(r"^https?://", "", url.strip().lower()).split("/")[0].split(":")[0]
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
            keep = ("site", "score", "grade", "top", "failing", "passed", "not_measured", "measured_at")
            return self._send(200, dict({k: res[k] for k in keep if k in res}, ok=True))
        if p.path == "/api/refund":
            sid = str(body.get("session_id") or "")
            if not stripe_api.SESSION_RE.match(sid):
                return self._send(400, {"ok": False, "error": "missing payment reference"})
            code, payload = do_refund(sid)
            return self._send(code, payload)
        return self._send(404, {"ok": False, "error": "not found"})


def check_startup():
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if os.environ.get("SELFSERVE_TEST_ALLOW_LOOPBACK") == "1" and stripe_api.key_mode(key) == "live":
        raise SystemExit("refusing to start: test loopback mode with a live Stripe key")
    if key.startswith(("sk_live_", "sk_test_")):
        sys.stderr.write("warning: use a restricted key (rk_...) with only Checkout Sessions read and Refunds write\n")


def main():
    check_startup()
    port = int(os.environ.get("PORT", "10000"))
    srv = ThreadingHTTPServer((os.environ.get("BIND", "0.0.0.0"), port), Handler)
    srv.daemon_threads = True
    sys.stderr.write(f"apex checkup api on :{port}\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
