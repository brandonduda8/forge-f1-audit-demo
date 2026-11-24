#!/usr/bin/env python3
"""runner.py - one guarded checkup run, in its own short-lived process.

Usage: python3 -I runner.py <light|full> <url>
Prints one JSON object on stdout and exits 0, or exits 2 on bad usage.

* light: the free mini-check. f1_measure --light: exactly three requests
  (homepage, /robots.txt, /sitemap.xml), plus two DNS TXT lookups (SPF, DMARC)
  that never touch the site. Returns only the grade, the score, the titles of
  the top 3 problems, and the top problem in each separate section
  (accessibility; security and email trust).
* full: the paid report. Homepage, robots, sitemap, up to 40 same-site
  resources (HEAD), up to 5 internal pages, the first contact page, and up to
  25 internal links (HEAD), one HEAD each to /.env and /.git/HEAD (never
  downloaded), with a hard cap of 90 requests and 75 seconds; plus DNS TXT
  lookups for SPF, DMARC and common DKIM names.
  Returns the structured report and the Markdown used for the PDF.

Measurement is f1_measure.py and scoring is audit_pipeline.score(), imported
from this repo unchanged in spirit: nothing is re-implemented here. This file
only (1) installs netguard so the run cannot reach private addresses, (2) caps
the run, and (3) turns anything a cap or the guard cut short into
"not measured" so no failure is invented by our own limits.
"""
import json
import os
import re
import resource
import signal
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "f1-audit-product", "tools"))
sys.path.insert(0, os.path.join(REPO, "audit-demo"))

import netguard  # noqa: E402

LIMITS = {
    "light": {"max_requests": 3, "deadline_s": 40, "max_resources": 0},
    "full": {"max_requests": 90, "deadline_s": 75, "max_resources": 40},
}
# checks whose value can be partial if a cap or the guard cut the run short
CAP_SENSITIVE = ("1.5", "1.6", "4.3", "4.4", "6.6")
# memory ceiling for one run (Render's free instance has 512 MB for everything)
MEM_LIMIT_BYTES = int(os.environ.get("CHECKUP_MEM_MB", "300")) * 1024 * 1024
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
SEV_RANK = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}


def normalize_url(raw):
    """Accept 'example.com', 'www.example.com/page' or a full http(s) URL."""
    s = (raw or "").strip()
    if not s or len(s) > 300 or any(c.isspace() for c in s):
        raise ValueError("Enter a website address like example.com")
    if not SCHEME_RE.match(s) or re.match(r"^[^/]+:\d+(/|$)", s):
        s = "https://" + s     # "example.com" or "example.com:443/x" (host:port, no scheme)
    p = urlparse(s)
    if p.scheme not in ("http", "https"):
        raise ValueError("Only http and https websites can be checked")
    if p.username or p.password:
        raise ValueError("Website addresses with a username or password are not accepted")
    host = (p.hostname or "").rstrip(".").lower()
    if not host or "." not in host:
        raise ValueError("Enter a full website address like example.com")
    try:
        port = p.port
    except ValueError:
        raise ValueError("That website address has an invalid port") from None
    if port not in (None, 80, 443) and os.environ.get("SELFSERVE_TEST_ALLOW_LOOPBACK") != "1":
        raise ValueError("Only websites on the standard ports can be checked")
    netloc = host if port is None else f"{host}:{port}"
    path = p.path or "/"
    return f"{p.scheme}://{netloc}{path}" + (f"?{p.query}" if p.query else "")


def _mark_not_measured(ev, cid, why):
    c = ev["checks"].get(cid)
    if c is None:
        return
    c["status"] = "NOT_MEASURED"
    c["value"] = None
    c["note"] = why


def sanitize_evidence(ev, events, mode):
    """Turn cap/guard side effects into honest 'not measured' rows."""
    checks = ev.get("checks", {})
    capped = any(e.startswith("budget") or e == "too_large" for e in events)
    blocked = "blocked" in events
    if capped:
        for cid in CAP_SENSITIVE:
            if cid in checks and checks[cid].get("status") != "NOT_MEASURED":
                _mark_not_measured(ev, cid, "the run reached its time or request limit before this check finished")
    # 6.6: an internal link we refused to follow (non-public address) or could
    # not fetch because of our own limits is not a broken link.
    c66 = checks.get("6.6")
    if c66 and isinstance(c66.get("value"), dict):
        kept, dropped = [], 0
        for b in c66["value"].get("broken", []):
            st = str(b.get("status", ""))
            if "BLOCKED" in st or "BUDGET" in st:
                dropped += 1
            else:
                kept.append(b)
        if dropped:
            c66["value"]["broken"] = kept
            c66["note"] = (c66.get("note") or "") + f" ({dropped} link(s) not checkable from here were left out)"
    # 3.7 in light mode: no direct TLS check is made, so HTTPS is confirmed but
    # the certificate is not. Say so instead of scoring it as a problem.
    if "3.7" in checks:
        v = checks["3.7"].get("value") or {}
        cert = v.get("cert") or {}
        if v.get("https") and not cert.get("valid"):
            # the page itself loaded over verified HTTPS; only the separate
            # certificate inspection is missing, which is not a site problem
            why = ("the free check confirms HTTPS but does not inspect the certificate" if mode == "light"
                   else "HTTPS loaded fine; the separate certificate inspection did not complete")
            _mark_not_measured(ev, "3.7", why)
    c38 = checks.get("3.8")
    if c38 and isinstance(c38.get("value"), dict):
        v = c38["value"]
        if v.get("robots") is None or v.get("sitemap_xml") is None:
            _mark_not_measured(ev, "3.8", "robots.txt or sitemap.xml could not be fetched in this run")
    if blocked and ev.get("fatal"):
        ev["fatal"] = "that address does not point to a public website"
    elif "too_large" in events and ev.get("fatal"):
        ev["fatal"] = "the homepage is too large to check (over %d MB)" % (netguard.MAX_BODY_BYTES // (1024 * 1024))
    elif "budget:time" in events and ev.get("fatal"):
        ev["fatal"] = "the homepage took too long to load"
    return ev


def structured(ev, sc, pipe):
    """The paid report as data for the report page (all text, no HTML)."""
    checks = sc["check_scores"]
    fails = [(cid, d) for cid, d in checks.items() if d["score"] is not None and d["score"] < 1]
    fails.sort(key=lambda x: (SEV_RANK[pipe.fix_for(x[0], x[1]["score"])[0]], x[1]["score"], x[0]))

    def finding(cid, d):
        sev, title, effort, fix = pipe.fix_for(cid, d["score"])
        return {"id": cid, "severity": sev, "title": title, "measured": d["why"],
                "method": ev["checks"].get(cid, {}).get("method", ""),
                "why": pipe.IMPACT.get(cid, ""), "fix": fix, "effort": effort}
    # the graded checks; accessibility and security have their own sections
    findings = [finding(cid, d) for cid, d in fails if cid not in pipe.SECTION_IDS]
    sections = [{"name": name, "score": (sc.get("sections") or {}).get(name, {}).get("score"),
                 "findings": [finding(cid, d) for cid, d in fails if cid in ids]}
                for name, ids in pipe.SECTIONS if any(cid in checks for cid in ids)]
    passed = [{"id": cid, "title": pipe.LABELS.get(cid, cid), "measured": d["why"]}
              for cid, d in sorted(checks.items()) if d["score"] == 1]
    not_measured = [{"id": cid, "label": pipe.LABELS.get(cid, cid), "reason": d["why"]}
                    for cid, d in sorted(checks.items()) if d["score"] is None]
    not_measured += [{"id": cid, "label": label, "reason": why} for cid, label, why in pipe.NOT_MEASURED]
    cats = [{"name": n, "score": sc["categories"][n]["score"], "weight": w}
            for n, w, _ids in pipe.CATEGORIES]
    return {"categories": cats, "findings": findings, "sections": sections, "passed": passed,
            "not_measured": not_measured}


def run(mode, raw_url):
    if mode not in LIMITS:
        raise ValueError("mode must be light or full")
    url = normalize_url(raw_url)
    lim = dict(LIMITS[mode])
    if os.environ.get("SELFSERVE_TEST_ALLOW_LOOPBACK") == "1" and os.environ.get("RUNNER_TEST_DEADLINE"):
        lim["deadline_s"] = int(os.environ["RUNNER_TEST_DEADLINE"])   # tests only
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MEM_LIMIT_BYTES, MEM_LIMIT_BYTES))
    except (ValueError, OSError):
        pass
    netguard.install()
    import f1_measure
    import audit_pipeline as pipe
    client = netguard.GuardedRequests(lim["max_requests"], lim["deadline_s"])
    f1_measure.requests = client            # every get/head goes through the guard
    f1_measure.MAX_RESOURCES = lim["max_resources"] or f1_measure.MAX_RESOURCES
    host = urlparse(url).hostname

    def _too_slow(_sig, _frm):
        # a site that drips bytes slowly can outlast per-read timeouts; this
        # fires at the deadline and then every second until the run ends
        if "budget:time" not in client.events:
            client.events.append("budget:time")
        raise netguard.BudgetExceeded("BUDGET time limit reached")

    signal.signal(signal.SIGALRM, _too_slow)
    signal.setitimer(signal.ITIMER_REAL, lim["deadline_s"], 1.0)
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            import io
            import contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    ev = f1_measure.measure(host, url, tmp, light=(mode == "light"))
                except MemoryError:
                    ev = {"fatal": "the homepage is too large to check", "checks": {}}
                except netguard.BudgetExceeded:
                    ev = {"fatal": "the homepage took too long to load", "checks": {}}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    ev = sanitize_evidence(ev, client.events, mode)
    base = {"site": host, "url": url, "mode": mode, "measured_at": ev.get("measured_at"),
            "requests_made": client.count}
    if ev.get("fatal"):
        return dict(base, ok=False, fatal=True,
                    error=f"We couldn't check {host}: {ev['fatal']}.")
    final_host = urlparse(ev.get("final_url") or url).hostname
    if final_host and final_host != host:
        base["final_url"] = ev["final_url"]          # say which site was really checked
    base["redirects"] = ev.get("redirects", 0)
    sc = pipe.score(ev)
    rep = structured(ev, sc, pipe)
    out = dict(base, ok=True, score=sc["total"], grade=sc["grade"])
    if mode == "light":
        out["top"] = [{"severity": f["severity"], "title": f["title"]} for f in rep["findings"][:3]]
        out["failing"] = len(rep["findings"])
        out["passed"] = len(rep["passed"])
        out["not_measured"] = len(rep["not_measured"])
        # the top finding from each separate section (accessibility, security and email trust)
        out["sections"] = [{"name": s["name"], "score": s["score"], "failing": len(s["findings"]),
                            "top": ({"severity": s["findings"][0]["severity"], "title": s["findings"][0]["title"]}
                                    if s["findings"] else None)} for s in rep["sections"]]
        return out
    from zoneinfo import ZoneInfo
    date_str = datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    out["report"] = rep
    out["markdown"] = pipe.build_report(host, url, ev, sc, date_str, host, variant="selfserve")
    return out


def main(argv):
    if len(argv) != 3:
        print(__doc__.splitlines()[2], file=sys.stderr)
        return 2
    try:
        res = run(argv[1], argv[2])
    except ValueError as e:
        res = {"ok": False, "fatal": False, "error": str(e), "input_error": True}
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
