#!/usr/bin/env python3
"""server.py — Audit Demo private web flow (tailnet-only via Tailscale serve).

One-click "audit this URL": paste a business URL -> real f1_measure.py ->
pinned-rubric scoring -> branded PDF -> download.

Listens on 127.0.0.1:18951 (plain HTTP; TLS terminates at Tailscale serve
port 8445, private tailnet only). --daemon does the classic double fork +
setsid so the server survives its launcher (see AGENTS.md keepalive notes).
"""
import json, os, re, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
# 12-factor overrides for container/PaaS deploys (defaults = tailnet behavior).
PORT = int(os.environ.get("PORT", 18951))
BIND = os.environ.get("BIND", "127.0.0.1")
VENV_PY = os.environ.get("AUDIT_PYBIN",
                         os.path.expanduser("~/workspace/vendor/genesis-tools-venv/bin/python"))
PIPE = os.path.join(BASE, "audit_pipeline.py")
JOBS = os.path.join(BASE, "state", "jobs")
PIDF = os.path.join(BASE, "state", "server.pid")
SAMPLES = os.environ.get("F1_SAMPLES",
                         os.path.expanduser("~/workspace/genesis-os/war-room/empire/forge/f1-audit-product/samples"))

os.makedirs(JOBS, exist_ok=True)

STYLE = """<style>
body{background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;margin:0;padding:0}
.wrap{max-width:760px;margin:0 auto;padding:40px 20px}
.brand{color:#E8632B;font-weight:800;letter-spacing:3px;font-size:13px}
h1{font-size:34px;margin:8px 0 4px}
.sub{color:#94a3b8;margin:0 0 28px}
.card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:24px;margin:20px 0}
label{display:block;font-size:13px;color:#94a3b8;margin:14px 0 6px}
input[type=text]{width:100%;box-sizing:border-box;background:#0f172a;border:1px solid #475569;border-radius:8px;color:#e2e8f0;padding:12px;font-size:16px}
button{background:#E8632B;border:none;color:#fff;font-weight:700;font-size:16px;padding:14px 28px;border-radius:8px;cursor:pointer;margin-top:18px}
button:disabled{background:#475569}
.note{color:#94a3b8;font-size:13px;line-height:1.6}
a{color:#E8632B}
.bar{height:8px;background:#334155;border-radius:4px;margin:16px 0}
.bar>div{height:8px;background:#E8632B;border-radius:4px;transition:width .5s}
.sample{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #334155}
</style>"""

def page(title, body):
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title>{STYLE}</head><body><div class=wrap>
<div class=brand>APEX FORGE</div>{body}</div></body></html>"""

def index():
    samples_html = ""
    for slug, label in (("great-roofing-llc", "Great Roofing LLC — 82/100, Grade B"),
                        ("hobson-plumbing-heating", "Hobson Plumbing & Heating — 65/100, Grade C"),
                        ("anytime-heating-air", "Anytime Heating & Air — 73/100, Grade C")):
        if os.path.exists(os.path.join(SAMPLES, f"sample-{slug}.pdf")):
            samples_html += (f'<div class=sample><span>{label}</span>'
                             f'<a href="/sample/{slug}.pdf">Download PDF</a></div>')
    return page("Apex Forge — Website Audit Demo", f"""
<h1>Website Audit — one click</h1>
<p class=sub>Paste a business URL. The real Forge F1 measurement suite runs against
the live site, scores it on the pinned 0–100 rubric, and hands you the branded
PDF. Measured, not asserted.</p>
<div class=card>
<form method=post action=/audit>
<label>Business name</label>
<input type=text name=name placeholder="e.g. Great Roofing LLC" required>
<label>Website URL</label>
<input type=text name=url placeholder="https://example.com" required>
<button type=submit>Run the audit</button>
</form>
<p class=note>Takes ~1–2 minutes (passive reads of the public site only — no forms
submitted, nothing sent). Render-dependent checks (LCP, CLS, mobile-menu taps)
are honestly excluded, not estimated.</p>
</div>
<div class=card>
<h3 style="margin-top:0">Sample audits (already done)</h3>
{samples_html}
</div>""")

def status_page(jid, st):
    phase = st.get("phase", "starting")
    pct = {"queued": 5, "measuring": 40, "scoring": 70, "rendering": 85, "done": 100, "error": 100}[phase]
    if phase == "done":
        return page("Audit complete", f"""
<h1>Audit complete</h1>
<p class=sub>{st['name']} — <b>{st['score']}/100</b>, Grade {st['grade']}</p>
<div class=card><a href="/job/{jid}/audit.pdf"><button>Download the branded PDF</button></a>
<p class=note><a href="/">Run another audit</a></p></div>""")
    if phase == "error":
        return page("Audit failed", f"""
<h1>Audit hit a wall</h1>
<div class=card><p>{st.get('error', 'unknown error')}</p>
<p class=note><a href="/">Try another URL</a></p></div>""")
    label = {"queued": "Queued…", "measuring": "Measuring the live site (33-check suite)…",
             "scoring": "Scoring against the pinned rubric…",
             "rendering": "Rendering your branded PDF…"}.get(phase, "Working…")
    return page("Auditing…", f"""
<h1>Auditing {st.get('name', 'site')}…</h1>
<p class=sub>{label}</p>
<div class=card><div class=bar><div style="width:{pct}%"></div></div>
<p class=note>This takes ~1–2 minutes. The page refreshes on its own.</p></div>
<script>setTimeout(()=>location.reload(), 5000)</script>""")

def job_state(jid):
    p = os.path.join(JOBS, jid, "job.json")
    return json.load(open(p)) if os.path.exists(p) else None

def save_job(jid, st):
    d = os.path.join(JOBS, jid)
    os.makedirs(d, exist_ok=True)
    json.dump(st, open(os.path.join(d, "job.json"), "w"))

def run_audit(jid, name, url):
    d = os.path.join(JOBS, jid)
    st = job_state(jid); st["phase"] = "measuring"; save_job(jid, st)
    import subprocess
    try:
        r = subprocess.run([VENV_PY, PIPE, name, url, d],
                           capture_output=True, text=True, timeout=900)
        out = (r.stdout or "").strip().splitlines()
        res = json.loads(out[-1]) if out else {}
        st = job_state(jid)
        if res.get("ok"):
            st.update({"phase": "done", "score": res["score"], "grade": res["grade"]})
        else:
            st.update({"phase": "error", "error": (res.get("error") or "pipeline failed")[:400]})
        save_job(jid, st)
    except Exception as e:
        st = job_state(jid); st.update({"phase": "error", "error": str(e)[:400]}); save_job(jid, st)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, ctype, body):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/":
            return self._send(200, "text/html; charset=utf-8", index())
        if p.path == "/health":
            return self._send(200, "application/json", '{"ok":true}')
        m = re.match(r"^/sample/([a-z0-9-]+)\.pdf$", p.path)
        if m:
            fp = os.path.join(SAMPLES, f"sample-{m.group(1)}.pdf")
            if os.path.exists(fp):
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f'attachment; filename="sample-{m.group(1)}.pdf"')
                self.send_header("Content-Length", str(os.path.getsize(fp)))
                self.end_headers()
                with open(fp, "rb") as f: self.wfile.write(f.read())
                return
            return self._send(404, "text/plain", "not found")
        m = re.match(r"^/job/([0-9a-f]{32})$", p.path)
        if m:
            st = job_state(m.group(1))
            if not st: return self._send(404, "text/plain", "unknown job")
            return self._send(200, "text/html; charset=utf-8", status_page(m.group(1), st))
        m = re.match(r"^/job/([0-9a-f]{32})/audit\.pdf$", p.path)
        if m:
            fp = os.path.join(JOBS, m.group(1), "audit.pdf")
            st = job_state(m.group(1))
            if st and st.get("phase") == "done" and os.path.exists(fp):
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'attachment; filename="website-audit.pdf"')
                self.send_header("Content-Length", str(os.path.getsize(fp)))
                self.end_headers()
                with open(fp, "rb") as f: self.wfile.write(f.read())
                return
            return self._send(404, "text/plain", "not ready")
        return self._send(404, "text/plain", "not found")
    def do_POST(self):
        p = urlparse(self.path)
        if p.path != "/audit":
            return self._send(404, "text/plain", "not found")
        ln = int(self.headers.get("Content-Length", 0))
        q = parse_qs(self.rfile.read(ln).decode())
        name = (q.get("name", [""])[0] or "").strip()[:120]
        url = (q.get("url", [""])[0] or "").strip()[:500]
        if not name or not url:
            return self._send(400, "text/plain", "name and url required")
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        if not re.match(r"^https?://[a-z0-9.-]+\.[a-z]{2,}", url, re.I):
            return self._send(400, "text/plain", "that doesn't look like a real URL")
        jid = uuid.uuid4().hex
        save_job(jid, {"phase": "queued", "name": name, "url": url,
                       "created": time.strftime("%FT%TZ", time.gmtime())})
        t = threading.Thread(target=run_audit, args=(jid, name, url), daemon=True)
        t.start()
        self.send_response(303)
        self.send_header("Location", f"/job/{jid}")
        self.end_headers()

def daemonize():
    if os.fork(): os._exit(0)
    os.setsid()
    if os.fork(): os._exit(0)
    sys.stdout.flush(); sys.stderr.flush()
    with open("/dev/null", "rb") as f: os.dup2(f.fileno(), sys.stdin.fileno())
    logf = os.path.join(BASE, "logs", "server.log")
    os.makedirs(os.path.dirname(logf), exist_ok=True)
    lf = open(logf, "ab", 0)
    os.dup2(lf.fileno(), sys.stdout.fileno())
    os.dup2(lf.fileno(), sys.stderr.fileno())
    open(PIDF, "w").write(str(os.getpid()))

def main():
    if "--daemon" in sys.argv:
        daemonize()
    srv = ThreadingHTTPServer((BIND, PORT), H)
    print(f"audit-demo listening on {BIND}:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
