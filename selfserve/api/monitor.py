"""monitor.py - the monthly re-check for buyers on the Monthly Monitoring plan.

run_monitoring(run_checkup, now=None) goes through every ACTIVE monitoring
subscription that Square reports (square_api.monitored_sites()) and, for each:
  1. skips it when the site was scanned less than 25 days ago (the purchase
     report counts as a scan, so the first re-check comes about a month later);
  2. runs the full paid check on the website stored on the Square order;
  3. compares with the previous scan: score change, new problems and fixed
     problems (matched by check id, or by title when there is no id);
  4. saves the scan (store.py) and emails the buyer the result, at most once per
     subscription per calendar month (UTC).

A site that could not be checked this time (busy, unreachable, timed out) is
counted as "not_checked": nothing is saved and nothing is emailed, so the next
daily run simply tries again. A problem our own limits cut short is never
reported as a new problem: only findings the checker measured are compared.

One run at a time per process (a module-level lock). start_in_background() is
what the admin route uses; it returns False when a run is already going.
"""
import os
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mailer  # noqa: E402
import square_api as pay  # noqa: E402
import store as store_mod  # noqa: E402

RESCAN_AFTER_S = 25 * 24 * 3600
_run_lock = threading.Lock()


def is_running():
    return _run_lock.locked()


def issues_of(result):
    """The measured problems of a full/pro result, as [{id, title, severity}]."""
    rep = (result or {}).get("report") or {}
    out, seen = [], set()
    groups = [rep.get("findings") or []] + [s.get("findings") or [] for s in rep.get("sections") or []]
    for group in groups:
        for f in group:
            key = str(f.get("id") or f.get("title") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({"id": str(f.get("id") or ""), "title": str(f.get("title") or f.get("id") or ""),
                        "severity": str(f.get("severity") or "")})
    for page in rep.get("pages") or []:            # Pro reports: per-page problems
        path = str(page.get("path") or page.get("url") or "")
        for f in page.get("issues") or []:
            key = "page:" + path + ":" + str(f.get("id") or f.get("title") or "")
            if key in seen:
                continue
            seen.add(key)
            out.append({"id": key, "title": f"{path}: {f.get('title') or f.get('id') or ''}",
                        "severity": str(f.get("severity") or "")})
    return out


def _key(i):
    return i.get("id") or ("title:" + (i.get("title") or ""))


def diff(prev, cur_issues, cur_grade, cur_score):
    """What changed since the previous scan, or None when there is none to compare with."""
    if not prev:
        return None
    before = {_key(i): i for i in prev.get("issues") or []}
    after = {_key(i): i for i in cur_issues or []}
    sb, sa = prev.get("score"), cur_score
    return {"since": prev.get("at"),
            "grade_before": prev.get("grade"), "grade_after": cur_grade,
            "score_before": sb, "score_after": sa,
            "score_change": (sa - sb) if isinstance(sa, int) and isinstance(sb, int) else None,
            "new": [after[k] for k in after if k not in before],
            "fixed": [before[k] for k in before if k not in after]}


def month_key(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")


def _host(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def run_monitoring(run_checkup, now=None, *, store=None, on_result=None):
    """Run once (returns counts), or {"busy": True} when another run is going."""
    if not _run_lock.acquire(blocking=False):
        return {"busy": True}
    try:
        return _run(run_checkup, now, store, on_result)
    finally:
        _run_lock.release()


def start_in_background(run_checkup, *, store=None, on_result=None):
    """Start a run in a daemon thread. False when one is already going."""
    if not _run_lock.acquire(blocking=False):
        return False

    def work():
        try:
            _run(run_checkup, None, store, on_result)
        except Exception as e:        # a background run must never take the server down
            sys.stderr.write(f"monitoring run failed: {type(e).__name__}\n")
        finally:
            _run_lock.release()
    threading.Thread(target=work, daemon=True, name="apex-monitoring").start()
    return True


def _run(run_checkup, now, st, on_result):
    st = st or store_mod.default()
    t0 = time.time() if now is None else now
    run_id = st.start_run(at=t0)
    counts = {"sites": 0, "scanned": 0, "skipped_recent": 0, "not_checked": 0,
              "emails_sent": 0, "emails_skipped": 0}
    try:
        sites = pay.monitored_sites()
    except (pay.SquareUnavailable, pay.PaymentError):
        counts["error"] = "square_unavailable"
        st.finish_run(run_id, counts, at=time.time() if now is None else now)
        sys.stderr.write("monitoring run: could not list subscriptions from Square\n")
        return counts
    seen = set()
    for site in sites:
        sub, oid, website = site.get("subscription_id"), site.get("order_id"), site.get("website")
        if not sub or not oid or not website or sub in seen:
            continue
        seen.add(sub)
        counts["sites"] += 1
        at = time.time() if now is None else now
        prev = st.last_scan(oid)
        if prev and at - prev["at"] < RESCAN_AFTER_S:
            counts["skipped_recent"] += 1
            continue
        try:
            res = run_checkup("full", website)
        except Exception:
            res = {"ok": False}
        if not res.get("ok") or res.get("score") is None:
            counts["not_checked"] += 1
            continue
        counts["scanned"] += 1
        issues = issues_of(res)
        changes = diff(prev, issues, res.get("grade"), res.get("score"))
        st.add_scan(oid, res.get("site") or _host(website), res.get("grade"), res.get("score"), issues,
                    source="monitor", changes=changes, at=at)
        if on_result:
            try:
                on_result(oid, res)
            except Exception:
                pass
        if _email_once(st, site, oid, res, changes, at):
            counts["emails_sent"] += 1
        else:
            counts["emails_skipped"] += 1
    st.finish_run(run_id, counts, at=time.time() if now is None else now)
    sys.stderr.write("monitoring run: " + " ".join(f"{k}={v}" for k, v in counts.items()) + "\n")
    return counts


def _email_once(st, site, oid, res, changes, at):
    """Email the buyer, once per subscription per month. Never raises."""
    to = (site.get("email") or "").strip()
    if not mailer.enabled() or not mailer.valid_address(to):
        return False
    key = f"monitor:{site['subscription_id']}:{month_key(at)}"
    try:
        if not st.claim_email(key, at=at):
            return False
        ok = mailer.send("monitor_report", to, order_id=oid, site=res.get("site") or _host(site.get("website")),
                         plan="monitor", grade=res.get("grade"), score=res.get("score"), changes=changes)
        if not ok:
            st.release_email(key)
        return ok
    except Exception:
        return False
