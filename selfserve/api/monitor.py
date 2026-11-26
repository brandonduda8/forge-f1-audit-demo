"""monitor.py - the monthly re-check for buyers on the Monthly Monitoring plan.

run_monitoring(run_checkup, now=None) goes through every current monitoring
subscription that Square reports (square_api.monitored_sites(), which already
leaves out refunded orders and subscriptions that are not paid up) and, for each:
  1. skips it when this month's monitoring email already went out, or when the
     last counted scan is less than 25 days old. "Counted" means the purchase
     report or a monthly scan that was emailed; a buyer re-opening the report
     never moves the clock. The clock and the month come from the Square order's
     own metadata ("state", see square_api.set_monitor_state), because this
     server's disk is temporary; the local store is only a fallback;
  2. confirms the order with Square (refunded or no longer valid: left alone);
  3. runs the full paid check on the website stored on the Square order;
  4. compares with the last EMAILED scan: score change, new problems and fixed
     problems by check id. Honest about gaps: a check that was not measured on
     either side is never called "new" or "fixed";
  5. saves the scan and emails the buyer, at most once per subscription per
     calendar month (UTC). Only after the email went out (or when email is off)
     is the new scan written back to Square as the monthly marker. A failed send
     changes nothing durable, so the next daily run re-checks and tries again.

A site that could not be checked this time (busy, unreachable, timed out) is
counted as "not_checked": nothing is emailed and the next daily run tries again.

Paying buyers come first: the server hands in a run_checkup that waits while any
buyer is waiting for a paid worker, and this module pauses PAUSE_BETWEEN_S
between sites.

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
PAUSE_BETWEEN_S = 2.0
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


def not_measured_of(result):
    """Ids of the checks this run could not measure (same id scheme as issues_of)."""
    rep = (result or {}).get("report") or {}
    out = []
    for n in rep.get("not_measured") or []:
        cid = str((n.get("id") if isinstance(n, dict) else n) or "")
        if cid and cid not in out:
            out.append(cid)
    for page in rep.get("pages") or []:
        path = str(page.get("path") or page.get("url") or "")
        for n in page.get("not_measured") or []:
            cid = str((n.get("id") if isinstance(n, dict) else n) or "")
            if cid and ("page:" + path + ":" + cid) not in out:
                out.append("page:" + path + ":" + cid)
    return out


def _passed_titles(result):
    rep = (result or {}).get("report") or {}
    return {str(p.get("id")): {"id": str(p.get("id")), "title": str(p.get("title") or p.get("id")), "severity": ""}
            for p in rep.get("passed") or [] if isinstance(p, dict) and p.get("id")}


def _key(i):
    return i.get("id") or ("title:" + (i.get("title") or ""))


def ids_of(issues):
    return [_key(i) for i in issues or []]


def diff(prev, cur_issues, cur_grade, cur_score, cur_not_measured=()):
    """What changed since the previous counted scan, or None when there is none to compare with.
    fixed = failing before, not failing now, and measured now.
    new   = failing now, not failing before, and measured before."""
    if not prev:
        return None
    before = {_key(i): i for i in prev.get("issues") or []}
    after = {_key(i): i for i in cur_issues or []}
    nm_before = set(prev.get("not_measured") or [])
    nm_now = set(cur_not_measured or [])
    sb, sa = prev.get("score"), cur_score
    return {"since": prev.get("at"),
            "grade_before": prev.get("grade"), "grade_after": cur_grade,
            "score_before": sb, "score_after": sa,
            "score_change": (sa - sb) if isinstance(sa, int) and isinstance(sb, int) else None,
            "new": [after[k] for k in after if k not in before and k not in nm_before],
            "fixed": [before[k] for k in before if k not in after and k not in nm_now]}


def month_key(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")


def _host(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _state(site):
    s = site.get("state") or {}
    last = s.get("last")
    return {"last": float(last) if isinstance(last, (int, float)) and last > 0 else None,
            "month": s.get("month") if isinstance(s.get("month"), str) else None,
            "failing": [str(x) for x in s.get("failing") or []],
            "not_measured": [str(x) for x in s.get("not_measured") or []]}


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


def _clock(now):
    return time.time() if now is None else now


def _run(run_checkup, now, st, on_result):
    st = st or store_mod.default()
    t0 = _clock(now)
    run_id = st.start_run(at=t0)
    counts = {"sites": 0, "scanned": 0, "skipped_recent": 0, "not_checked": 0,
              "emails_sent": 0, "emails_skipped": 0}
    try:
        sites = pay.monitored_sites()
    except (pay.SquareUnavailable, pay.PaymentError):
        counts["error"] = "square_unavailable"
        st.finish_run(run_id, counts, at=_clock(now))
        sys.stderr.write("monitoring run: could not list subscriptions from Square\n")
        return counts
    seen, scanned_before = set(), False
    for site in sites:
        sub, oid, website = site.get("subscription_id"), site.get("order_id"), site.get("website")
        if not sub or not oid or not website or sub in seen:
            continue
        seen.add(sub)
        counts["sites"] += 1
        at = _clock(now)
        month = month_key(at)
        state = _state(site)
        mail_key = f"monitor:{sub}:{month}"
        if state["month"] == month or st.email_sent(mail_key):
            counts["skipped_recent"] += 1                 # this month's email already went out
            continue
        local = None if state["last"] else st.baseline(oid)
        last = state["last"] or (local["at"] if local else None)
        if last and at - last < RESCAN_AFTER_S:
            counts["skipped_recent"] += 1
            continue
        try:
            paid = pay.get_paid_order(oid)
        except pay.PaymentError:
            counts["sites"] -= 1                          # refunded, expired or not ours: not monitored
            continue
        except Exception:
            counts["not_checked"] += 1
            continue
        if paid.get("refunded"):
            counts["sites"] -= 1
            continue
        created = paid.get("created")
        if not last and isinstance(created, (int, float)) and 0 <= at - created < RESCAN_AFTER_S:
            counts["skipped_recent"] += 1                 # bought recently; the purchase report is the scan
            continue
        if scanned_before and PAUSE_BETWEEN_S:
            time.sleep(PAUSE_BETWEEN_S)                   # leave room for paying buyers
        try:
            res = run_checkup("full", website)
        except Exception:
            res = {"ok": False}
        if not res.get("ok") or res.get("score") is None:
            counts["not_checked"] += 1
            continue
        scanned_before = True
        counts["scanned"] += 1
        at = _clock(now) if now is not None else int(time.time())
        issues, nm = issues_of(res), not_measured_of(res)
        prev = _baseline(st, oid, state, local, res)
        changes = diff(prev, issues, res.get("grade"), res.get("score"), nm)
        scan_id = st.add_scan(oid, res.get("site") or _host(website), res.get("grade"), res.get("score"), issues,
                              source="monitor", changes=changes, at=at, plan="monitor", not_measured=nm,
                              counted=False)
        if on_result:
            try:
                on_result(oid, res)
            except Exception:
                pass
        sent = _email_once(st, site, oid, res, changes, mail_key)
        counts["emails_sent" if sent == "sent" else "emails_skipped"] += 1
        if sent in ("sent", "no_mail"):
            st.mark_counted(scan_id)
            _record_state(paid, at, month, issues, nm)
    st.finish_run(run_id, counts, at=_clock(now))
    sys.stderr.write("monitoring run: " + " ".join(f"{k}={v}" for k, v in counts.items()) + "\n")
    return counts


def _baseline(st, oid, state, local, res):
    """The scan to compare with: the last emailed scan (Square's marker), using the local copy
    of that same scan when this disk still has it; else the local counted scan; else None."""
    if state["last"]:
        same = st.scan_near(oid, state["last"])
        if same:
            return same
        titles = dict(_passed_titles(res))
        titles.update(st.titles(oid))
        return {"at": state["last"], "grade": None, "score": None,
                "issues": [titles.get(i) or {"id": i, "title": "check " + i, "severity": ""}
                           for i in state["failing"]],
                "not_measured": state["not_measured"]}
    return local


def _record_state(paid, at, month, issues, nm):
    """Write the monthly marker to the Square order (best effort, never raises)."""
    f = getattr(pay, "set_monitor_state", None)
    if f is None:
        return False
    try:
        return bool(f(paid, last=int(at), month=month, failing=ids_of(issues), not_measured=list(nm)))
    except Exception:
        return False


def _email_once(st, site, oid, res, changes, key):
    """"sent", "failed" (try again next run), "no_mail" (email is off or the address can't
    take mail: the scan still counts) or "already". Never raises."""
    to = (site.get("email") or "").strip()
    if not mailer.enabled() or not mailer.valid_address(to):
        return "no_mail"
    try:
        if not st.claim_email(key):
            return "already"
        ok = mailer.send("monitor_report", to, order_id=oid, site=res.get("site") or _host(site.get("website")),
                         plan="monitor", grade=res.get("grade"), score=res.get("score"), changes=changes)
        if not ok:
            st.release_email(key)
        return "sent" if ok else "failed"
    except Exception:
        try:
            st.release_email(key)
        except Exception:
            pass
        return "failed"
