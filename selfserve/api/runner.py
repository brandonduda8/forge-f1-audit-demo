#!/usr/bin/env python3
"""runner.py - one guarded checkup run, in its own short-lived process.

Usage: python3 -I runner.py <light|full> <url>
Prints one JSON object on stdout and exits 0, or exits 2 on bad usage.

* light: the free mini-check. f1_measure --light: exactly three requests
  (homepage, /robots.txt, /sitemap.xml). Returns only the grade, the score and
  the titles of the top 3 problems.
* full: the paid report. Homepage, robots, sitemap, up to 40 same-site
  resources (HEAD), up to 5 internal pages, the first contact page, and up to
  25 internal links (HEAD), with a hard cap of 90 requests and 75 seconds.
  Returns the structured report and the Markdown used for the PDF.

Measurement is f1_measure.py and scoring is audit_pipeline.score(), imported
from this repo unchanged in spirit: nothing is re-implemented here. This file
only (1) installs netguard so the run cannot reach private addresses, (2) caps
the run, and (3) turns anything a cap or the guard cut short into
"not measured" so no failure is invented by our own limits.
"""
import json
import os
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
SEV_RANK = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}


def normalize_url(raw):
    """Accept 'example.com', 'www.example.com/page' or a full http(s) URL."""
    s = (raw or "").strip()
    if not s or len(s) > 300 or any(c.isspace() for c in s):
        raise ValueError("Enter a website address like example.com")
    if "://" not in s:
        s = "https://" + s
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
    if mode == "light" and "3.7" in checks:
        v = checks["3.7"].get("value") or {}
        if v.get("https"):
            _mark_not_measured(ev, "3.7", "the free check confirms HTTPS but does not inspect the certificate")
    if blocked and ev.get("fatal"):
        ev["fatal"] = "that address does not point to a public website"
    elif "too_large" in events and ev.get("fatal"):
        ev["fatal"] = "the homepage is larger than 5 MB, too large to check"
    return ev


def structured(ev, sc, pipe):
    """The paid report as data for the report page (all text, no HTML)."""
    checks = sc["check_scores"]
    fails = [(cid, d) for cid, d in checks.items() if d["score"] is not None and d["score"] < 1]
    fails.sort(key=lambda x: (SEV_RANK[pipe.fix_for(x[0], x[1]["score"])[0]], x[1]["score"], x[0]))
    findings = []
    for cid, d in fails:
        sev, title, effort, fix = pipe.fix_for(cid, d["score"])
        findings.append({"id": cid, "severity": sev, "title": title, "measured": d["why"],
                         "method": ev["checks"].get(cid, {}).get("method", ""),
                         "why": pipe.IMPACT.get(cid, ""), "fix": fix, "effort": effort})
    passed = [{"id": cid, "title": pipe.LABELS.get(cid, cid), "measured": d["why"]}
              for cid, d in sorted(checks.items()) if d["score"] == 1]
    not_measured = [{"id": cid, "label": pipe.LABELS.get(cid, cid), "reason": d["why"]}
                    for cid, d in sorted(checks.items()) if d["score"] is None]
    not_measured += [{"id": cid, "label": label, "reason": why} for cid, label, why in pipe.NOT_MEASURED]
    cats = [{"name": n, "score": sc["categories"][n]["score"], "weight": w}
            for n, w, _ids in pipe.CATEGORIES]
    return {"categories": cats, "findings": findings, "passed": passed, "not_measured": not_measured}


def run(mode, raw_url):
    if mode not in LIMITS:
        raise ValueError("mode must be light or full")
    url = normalize_url(raw_url)
    lim = LIMITS[mode]
    netguard.install()
    import f1_measure
    import audit_pipeline as pipe
    client = netguard.GuardedRequests(lim["max_requests"], lim["deadline_s"])
    f1_measure.requests = client            # every get/head goes through the guard
    f1_measure.MAX_RESOURCES = lim["max_resources"] or f1_measure.MAX_RESOURCES
    host = urlparse(url).hostname
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        import io
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            ev = f1_measure.measure(host, url, tmp, light=(mode == "light"))
    ev = sanitize_evidence(ev, client.events, mode)
    base = {"site": host, "url": url, "mode": mode, "measured_at": ev.get("measured_at"),
            "requests_made": client.count}
    if ev.get("fatal"):
        return dict(base, ok=False, fatal=True,
                    error=f"We couldn't load {host}: {ev['fatal']}.")
    sc = pipe.score(ev)
    rep = structured(ev, sc, pipe)
    out = dict(base, ok=True, score=sc["total"], grade=sc["grade"])
    if mode == "light":
        out["top"] = [{"severity": f["severity"], "title": f["title"]} for f in rep["findings"][:3]]
        out["failing"] = len(rep["findings"])
        out["passed"] = len(rep["passed"])
        out["not_measured"] = len(rep["not_measured"])
        return out
    from zoneinfo import ZoneInfo
    date_str = datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    ev["evidence_file"] = "kept on the Apex Forge server for 30 days"
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
