"""square_api.py - the Square calls Apex Forge needs, over plain HTTPS.

Card data never reaches this server: the buyer pays on Square's own hosted
checkout page (card, Apple Pay, Google Pay, whatever the Square account has
switched on). This module only talks to Square's API.

Plans (PLANS below; prices come from env, never from the browser):
  checkup  one-time  Website Checkup                       CHECKUP_PRICE_CENTS  (2900)
  pro      one-time  Pro Checkup (homepage + 4 more pages)  PRO_PRICE_CENTS      (7900)
  monitor  monthly   Monthly Monitoring: first month MONITOR_FIRST_CENTS (2900, includes the
                     full report), then MONITOR_PRICE_CENTS (1900) every month
  "monitor" is offered only when SQUARE_MONITOR_PLAN_VARIATION_ID is set (the server also
  hides it unless buyer emails are on).

* create_checkout(website, redirect_url, plan): POST /v2/online-checkout/payment-links.
  One-time plans send an order WE price, with the website and plan on the order's
  metadata. "monitor" sends quick_pay + checkout_options.subscription_plan_id (the
  plan VARIATION id); Square documents only quick_pay for subscription links, and
  quick_pay carries no metadata, so we write the metadata onto the link's order
  with UpdateOrder right after. If that write fails the link is deleted and the
  buyer is never sent to pay for something we couldn't tie to their website.
  Returns the hosted checkout URL and the order id (the buyer's report reference).
* get_paid_order(order_id): GET /v2/orders/{id}, then GET /v2/payments/{id} for its
  tender. Refuses unless the order is ours (location + metadata marker), the payment
  is COMPLETED, covers that plan's price (for "monitor": the first-month price), is
  under 30 days old (for "monitor" the 30 days are waived while the subscription is
  current: ACTIVE and charged through today), and the website is on the order.
  If a monitor order has no payment of its own, the paired subscription's first
  invoice may stand in for it (see _invoice_payment); anything ambiguous is refused.
* subscription_for_order / monitored_sites / find_monitor_orders / cancel_subscription:
  the Subscriptions API. Square does not link a payment-link subscription to its
  order, so we pair them by customer + payment time (see _pair) and pin the pair on
  the order's metadata once found. Ambiguous pairs are never guessed or pinned.
* Durable state on the order's metadata (Square keeps it; our disk may be wiped):
  apex_report_delivered=1, apex_ready_mailed=1, apex_subscription_id, and apex_mon
  (the monthly monitoring state, <= 255 chars). Writes retry once on VERSION_MISMATCH.
* sales_summary(days): money numbers read from Square (ListPayments + SearchOrders +
  SearchSubscriptions + the plan's catalog price), so they survive any restart or wiped disk.
* mark_delivered, mark_ready_mailed, set_monitor_state, refund, find_orders,
  verify_webhook, paid_order_id_from_event, event_kind: see their docstrings.
* check_setup() and create_monitor_plan(): used by square_cli.py (doctor, create-monitor-plan).

Configuration comes from environment variables only (never files):
  SQUARE_ACCESS_TOKEN               access token of the Square application (sandbox or production)
  SQUARE_ENV                        "sandbox" (default) or "production"
  SQUARE_LOCATION_ID                the location the sales go to
  SQUARE_MONITOR_PLAN_VARIATION_ID  catalog id of the monitoring plan variation (optional)
  SQUARE_WEBHOOK_SIGNATURE_KEY      signature key of the webhook subscription
  SQUARE_WEBHOOK_URL                the exact notification URL registered for it
  CHECKUP_PRICE_CENTS / PRO_PRICE_CENTS / MONITOR_FIRST_CENTS / MONITOR_PRICE_CENTS
  SQUARE_VERSION                    Square-Version header (default below)
  SQUARE_API_BASE                   tests only: honoured only when SELFSERVE_TEST_ALLOW_LOOPBACK=1
                                    and it points at loopback; otherwise SQUARE_ENV picks the host
Logs never contain ids, tokens or emails.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

ORDER_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
CATALOG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
INVOICE_RE = re.compile(r"^[A-Za-z0-9_:.-]{8,255}$")      # Square invoice ids look like inv:0-ChC...
MAX_AGE_S = 30 * 24 * 3600
MARKER = "apex-checkup-v1"
DEFAULT_VERSION = "2025-01-23"
BASES = {"sandbox": "https://connect.squareupsandbox.com", "production": "https://connect.squareup.com"}
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# hosts Square's hosted checkout may live on; the site only follows these
CHECKOUT_HOSTS = ("square.link", "checkout.square.site", "squareup.com", "squareupsandbox.com")
PAIR_WINDOW_S = 3 * 24 * 3600          # how far back we LOOK for a subscription's checkout order
PAIR_SLACK_S = 30 * 60                 # a subscription is created within this long of its payment
MAX_PAGES = 20
META_VALUE_MAX = 255                   # Square: metadata values are at most 255 characters
MON_KEY = "apex_mon"
PAID_INVOICE = ("PAID", "PARTIALLY_REFUNDED", "REFUNDED")

PLANS = {
    "checkup": {"label": "Website Checkup", "price_env": "CHECKUP_PRICE_CENTS",
                "default_cents": 2900, "kind": "one_time", "mode": "full"},
    "pro": {"label": "Pro Checkup (homepage + up to 4 more pages)", "price_env": "PRO_PRICE_CENTS",
            "default_cents": 7900, "kind": "one_time", "mode": "pro"},
    "monitor": {"label": "Monthly Monitoring (full report now, re-checked every month, alerts on changes)",
                "price_env": "MONITOR_PRICE_CENTS", "default_cents": 1900, "kind": "subscription",
                "mode": "full", "first_env": "MONITOR_FIRST_CENTS", "default_first_cents": 2900},
}


class PaymentError(Exception):
    """No usable paid order for this link. Message is safe to show."""


class SquareUnavailable(Exception):
    """Square could not be reached, or the server is not configured."""


def _test_mode():
    return os.environ.get("SELFSERVE_TEST_ALLOW_LOOPBACK") == "1"


def env_name():
    e = os.environ.get("SQUARE_ENV", "sandbox").strip().lower()
    return e if e in BASES else None


def _cents(env_key, default):
    try:
        p = int(os.environ.get(env_key, str(default)))
    except ValueError:
        p = default
    return p if 100 <= p <= 100000 else default


def price_cents():
    """The Website Checkup price (kept for older callers)."""
    return _cents("CHECKUP_PRICE_CENTS", 2900)


def monitor_variation_id():
    v = os.environ.get("SQUARE_MONITOR_PLAN_VARIATION_ID", "").strip()
    return v if CATALOG_ID_RE.match(v) else ""


def plan_info(plan_id):
    """{"id","label","cents","first_cents","kind","mode","available"} or None for an unknown plan.

    cents is the plan's price (for "monitor": every month after the first); first_cents is
    what the first payment must be at least (equal to cents for one-time plans)."""
    p = PLANS.get(plan_id) if isinstance(plan_id, str) else None
    if not p:
        return None
    cents = _cents(p["price_env"], p["default_cents"])
    first = _cents(p["first_env"], p["default_first_cents"]) if p.get("first_env") else cents
    return {"id": plan_id, "label": p["label"], "cents": cents, "first_cents": first,
            "kind": p["kind"], "mode": p["mode"],
            "available": plan_id != "monitor" or bool(monitor_variation_id())}


def public_plans():
    """What the site may show: [{"id","label","cents","first_cents","kind","available"}] (no ids, no secrets)."""
    out = []
    for pid in PLANS:
        i = plan_info(pid)
        out.append({k: i[k] for k in ("id", "label", "cents", "first_cents", "kind", "available")})
    return out


def _api_base(env):
    """Square's host for SQUARE_ENV. SQUARE_API_BASE is a test hook: it counts only in test
    mode and only when it points at loopback, so it can never redirect a sandbox run to
    production (or the other way round) and get around create-monitor-plan's guard.
    Outside test mode an https value is ignored; a plain-http value is refused outright
    (RuntimeError), so nothing is ever sent over plain http."""
    over = os.environ.get("SQUARE_API_BASE", "").strip().rstrip("/")
    if not over:
        return BASES[env]
    if not _test_mode():
        if not over.lower().startswith("https://"):
            raise RuntimeError("SQUARE_API_BASE must be https (and is only used by tests)")
        return BASES[env]
    try:
        host = (urllib.parse.urlsplit(over).hostname or "").lower()
    except ValueError:
        host = ""
    return over if host in LOOPBACK_HOSTS else BASES[env]


def _cfg():
    token = os.environ.get("SQUARE_ACCESS_TOKEN", "")
    env = env_name()
    loc = os.environ.get("SQUARE_LOCATION_ID", "")
    if not token or not env or not loc:
        raise SquareUnavailable("payments are not configured on this server")
    return token, _api_base(env), loc


def _request(method, path, body=None):
    token, base, _ = _cfg()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Square-Version", os.environ.get("SQUARE_VERSION", DEFAULT_VERSION))
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # no proxies
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read().decode() or "{}")
        except Exception:
            out = {}
        kind = "/".join(path.split("?")[0].split("/")[:3])      # e.g. /v2/orders, never an id
        codes = ",".join(str(x.get("code", "")) for x in (out.get("errors") or [])[:3])
        sys.stderr.write(f"square {method} {kind} -> {e.code} {codes}\n")
        if e.code >= 500 or e.code == 429:
            raise SquareUnavailable(f"Square answered {e.code}") from None
        return e.code, out
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise SquareUnavailable(f"could not reach Square: {type(e).__name__}") from None


def _error_codes(out):
    return {str(e.get("code") or "") for e in ((out or {}).get("errors") or [])}


def checkout_url_ok(url):
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    if p.scheme != "https" and not _test_mode():
        return False
    if _test_mode() and host == "127.0.0.1":
        return True
    return any(host == h or host.endswith("." + h) for h in CHECKOUT_HOSTS)


def _order_meta(website, plan):
    meta = {"apex": MARKER, "website": website[:META_VALUE_MAX]}
    if plan != "checkup":          # an order without "plan" is a checkup (older orders have none)
        meta["plan"] = plan
    return meta


def _short(label):
    return label.split(" (")[0]


def create_checkout(website, redirect_url, plan="checkup"):
    """(checkout_url, order_id). website must already be normalized.

    Unknown or unavailable plan -> PaymentError (safe to show)."""
    info = plan_info(plan)
    if not info or not info["available"]:
        raise PaymentError("That plan isn't available.")
    _, _, loc = _cfg()
    options = {"redirect_url": redirect_url, "ask_for_shipping_address": False}
    body = {"idempotency_key": str(uuid.uuid4()),
            "description": "Apex Forge " + _short(info["label"]),
            "checkout_options": options,
            "payment_note": (_short(info["label"]) + ": " + website)[:500]}
    if plan == "monitor":
        # Square: subscription payment links take quick_pay only, and price_money should
        # equal the price of the plan's first priced phase (here the first month).
        money = {"amount": info["first_cents"], "currency": "USD"}
        body["quick_pay"] = {"name": "Apex Forge Monthly Monitoring", "price_money": money, "location_id": loc}
        options["subscription_plan_id"] = monitor_variation_id()
    else:
        money = {"amount": info["cents"], "currency": "USD"}
        body["order"] = {
            "location_id": loc,
            "line_items": [{"name": _short(info["label"]), "quantity": "1",
                            "note": website[:500], "base_price_money": money}],
            "metadata": _order_meta(website, plan),
        }
    code, out = _request("POST", "/v2/online-checkout/payment-links", body)
    if code != 200:
        raise SquareUnavailable(f"Square answered {code}")
    link = out.get("payment_link") or {}
    url, order_id = link.get("url") or "", link.get("order_id") or ""
    if not checkout_url_ok(url) or not ORDER_RE.match(order_id):
        raise SquareUnavailable("Square returned an unexpected checkout link")
    if plan == "monitor" and not _tag_link_order(order_id, website, plan, out):
        _delete_link(link.get("id") or "")
        raise SquareUnavailable("could not record the website on the subscription order")
    return url, order_id


# ---------------------------------------------------------------- order metadata writes

def _write_meta(order_id, meta, version=None):
    """UpdateOrder with these metadata keys. Returns the updated order, or None.

    Never raises. On VERSION_MISMATCH (someone else wrote the order since we read it) the
    order is read again and the write tried once more. The idempotency key names the order,
    the version and the exact keys written, so a retried request is recognised as the same
    one and a request for a newer version is never mistaken for an older one."""
    if not ORDER_RE.match(order_id or ""):
        return None
    path = "/v2/orders/" + urllib.parse.quote(order_id)
    digest = hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest()[:16]
    for attempt in range(2):
        try:
            if version is None:
                code, out = _request("GET", path)
                if code != 200:
                    return None
                version = (out.get("order") or {}).get("version")
            body = {"idempotency_key": f"apex-{order_id}-v{version}-{digest}"[:192],
                    "order": {"version": version, "metadata": dict(meta)}}
            code, out = _request("PUT", path, body)
        except SquareUnavailable:
            return None
        if code == 200:
            return out.get("order") or {}
        if attempt == 0 and "VERSION_MISMATCH" in _error_codes(out):
            version = None
            continue
        return None
    return None


def _tag_link_order(order_id, website, plan, link_out):
    """UpdateOrder metadata on the order a quick_pay link made. True when Square kept it."""
    version = None
    for o in ((link_out.get("related_resources") or {}).get("orders") or []):
        if o.get("id") == order_id:
            version = o.get("version")
    want = _order_meta(website, plan)
    order = _write_meta(order_id, want, version)
    meta = (order or {}).get("metadata") or {}
    return all(meta.get(k) == v for k, v in want.items())


def _delete_link(link_id):
    if not ORDER_RE.match(link_id or ""):
        return
    try:
        _request("DELETE", "/v2/online-checkout/payment-links/" + urllib.parse.quote(link_id))
    except SquareUnavailable:
        pass


def _after_write(paid, order, **fields):
    if order is None:
        return False
    if order.get("version") is not None:
        paid["order_version"] = order.get("version")
    paid.update(fields)
    return True


def mark_delivered(paid):
    """Best effort. Returns True when Square recorded it on the order. Never raises."""
    if paid.get("delivered"):
        return True
    order = _write_meta(paid.get("order_id") or "", {"apex_report_delivered": "1"}, paid.get("order_version"))
    return _after_write(paid, order, delivered=True)


def mark_ready_mailed(paid):
    """Record on the order that the report-ready email went out (apex_ready_mailed=1).

    Best effort, retries once on a version mismatch, never raises. True when recorded."""
    if paid.get("ready_mailed"):
        return True
    order = _write_meta(paid.get("order_id") or "", {"apex_ready_mailed": "1"}, paid.get("order_version"))
    return _after_write(paid, order, ready_mailed=True)


# ---------------------------------------------------------------- monitoring state (apex_mon)

_CHECK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def empty_monitor_state():
    return {"last": None, "month": None, "failing": [], "not_measured": [], "truncated": False}


def _clean_ids(ids):
    out = []
    for i in ids or []:
        s = str(i)
        if _CHECK_ID_RE.match(s) and s not in out:
            out.append(s)
    return sorted(out)


def encode_monitor_state(*, last=None, month=None, failing=(), not_measured=()):
    """The compact apex_mon value, e.g. "t=1795731516;m=2026-12;f=1.2,3.4;n=6.6".

    t = when the last monthly scan was recorded (epoch seconds), m = the month last emailed,
    f = failing check ids, n = not-measured check ids. Ids Square can't hold safely are
    dropped; if the lists don't fit in 255 characters the last ids are cut (failing ids are
    kept first) and "x=1" says so."""
    head = []
    try:
        if last is not None and float(last) > 0:
            head.append("t=%d" % int(float(last)))
    except (TypeError, ValueError):
        pass
    if isinstance(month, str) and _MONTH_RE.match(month):
        head.append("m=" + month)
    lists = {"f": _clean_ids(failing), "n": _clean_ids(not_measured)}

    def build(fl, nl, cut):
        parts = head + ["f=" + ",".join(fl), "n=" + ",".join(nl)] + (["x=1"] if cut else [])
        return ";".join(parts)

    full = build(lists["f"], lists["n"], False)
    if len(full) <= META_VALUE_MAX:
        return full
    kept = {"f": [], "n": []}
    for key in ("f", "n"):
        for i in lists[key]:
            trial = dict(kept)
            trial[key] = kept[key] + [i]
            if len(build(trial["f"], trial["n"], True)) <= META_VALUE_MAX:
                kept = trial
            else:
                break
    return build(kept["f"], kept["n"], True)


def decode_monitor_state(value):
    """{"last": float|None, "month": str|None, "failing": [str], "not_measured": [str],
    "truncated": bool} from an apex_mon value. Missing or unreadable parts come back empty."""
    st = empty_monitor_state()
    if not isinstance(value, str) or len(value) > 1000:
        return st
    for part in value.split(";"):
        k, sep, v = part.partition("=")
        if not sep:
            continue
        if k == "t" and v.isdigit() and 0 < len(v) <= 12:
            st["last"] = float(int(v))
        elif k == "m" and _MONTH_RE.match(v):
            st["month"] = v
        elif k == "f":
            st["failing"] = _clean_ids(x for x in v.split(",") if x)
        elif k == "n":
            st["not_measured"] = _clean_ids(x for x in v.split(",") if x)
        elif k == "x" and v == "1":
            st["truncated"] = True
    return st


def set_monitor_state(paid, *, last, month, failing, not_measured):
    """Save the monitoring state on the order (apex_mon). paid needs "order_id" (a
    get_paid_order result or a monitored_sites row both work). Best effort: retries once on
    a version mismatch, never raises. True when Square recorded it."""
    try:
        value = encode_monitor_state(last=last, month=month, failing=failing, not_measured=not_measured)
        order = _write_meta((paid or {}).get("order_id") or "", {MON_KEY: value}, (paid or {}).get("order_version"))
        fields = {"monitor_state": decode_monitor_state(value)}
        if "state" in paid:                      # a monitored_sites row keeps it under "state"
            fields["state"] = decode_monitor_state(value)
        return _after_write(paid, order, **fields)
    except Exception as e:                       # never let bookkeeping break a monitoring run
        sys.stderr.write(f"could not save monitoring state: {type(e).__name__}\n")
        return False


# ---------------------------------------------------------------- paid orders

def _parse_time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today(now=None):
    return datetime.fromtimestamp(now or time.time(), timezone.utc).strftime("%Y-%m-%d")


def subscription_is_current(sub, now=None):
    """ACTIVE is not the same as paid: a subscription counts as current only while its
    status is ACTIVE and Square has charged it through today (UTC) or later."""
    if not sub or str(sub.get("status") or "").upper() != "ACTIVE":
        return False
    ctd = str(sub.get("charged_through_date") or "")
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", ctd)) and ctd >= _today(now)


def _paid_payment(order):
    """The COMPLETED payment that paid this order, or None."""
    for t in order.get("tenders") or []:
        pid = t.get("payment_id") or t.get("id")
        if not pid or not ORDER_RE.match(pid):
            continue
        code, out = _request("GET", "/v2/payments/" + urllib.parse.quote(pid))
        if code != 200:
            continue
        pay = out.get("payment") or {}
        if pay.get("status") == "COMPLETED" and pay.get("order_id") == order.get("id"):
            return pay
    return None


def _refunded(pay):
    return int(((pay.get("refunded_money") or {}).get("amount")) or 0) > 0 or bool(pay.get("refund_ids"))


def _amount_ok(pay, plan):
    money = pay.get("amount_money") or {}
    return money.get("currency") == "USD" and int(money.get("amount") or 0) >= plan_info(plan)["first_cents"]


def _plan_of(meta):
    p = (meta or {}).get("plan") or "checkup"
    return p if p in PLANS else None


def _invoice_payment(sub, cust, loc):
    """The COMPLETED payment of a subscription's FIRST invoice, when that invoice is plainly
    this subscription's and was paid; else None.

    Square lists a subscription's invoice_ids newest first, so the first invoice is the last
    id. Checked: the invoice names this subscription, our location and this customer, its
    status is PAID (or since refunded), and its order's payment is COMPLETED at our location."""
    ids = sub.get("invoice_ids") or []
    if not isinstance(ids, list) or not ids or not INVOICE_RE.match(str(ids[-1])):
        return None
    code, out = _request("GET", "/v2/invoices/" + urllib.parse.quote(str(ids[-1]), safe=""))
    inv = (out.get("invoice") or {}) if code == 200 else {}
    rec = (inv.get("primary_recipient") or {}).get("customer_id")
    if (inv.get("subscription_id") != sub.get("id") or inv.get("location_id") != loc
            or str(inv.get("status") or "").upper() not in PAID_INVOICE or rec != cust
            or not ORDER_RE.match(inv.get("order_id") or "")):
        return None
    code, out = _request("GET", "/v2/orders/" + urllib.parse.quote(inv["order_id"]))
    order = (out.get("order") or {}) if code == 200 else {}
    if order.get("location_id") != loc:
        return None
    pay = _paid_payment(order)
    if not pay or pay.get("location_id", loc) != loc or pay.get("customer_id", cust) != cust:
        return None
    return pay


def get_paid_order(order_id, now=None):
    if not ORDER_RE.match(order_id or ""):
        raise PaymentError("This link is missing a valid payment reference.")
    _, _, loc = _cfg()
    code, out = _request("GET", "/v2/orders/" + urllib.parse.quote(order_id))
    if code == 404:
        raise PaymentError("We couldn't find a payment for this link.")
    if code != 200:
        raise SquareUnavailable(f"Square answered {code}")
    order = out.get("order") or {}
    meta = order.get("metadata") or {}
    plan = _plan_of(meta)
    if order.get("location_id") != loc or meta.get("apex") != MARKER or not plan:
        raise PaymentError("This payment reference is not for this site.")
    pay, via, found_sub = _paid_payment(order), "checkout", None
    if not pay and plan == "monitor":
        # Unconfirmed: Square may charge the first month on an invoice of its own rather
        # than on the link's order. Accept that only when everything lines up.
        cust = order.get("customer_id") or ""
        if ORDER_RE.match(cust) and monitor_variation_id():
            found_sub = _pair_for(loc, cust, _search_subscriptions([cust]), order, {})
            if found_sub and subscription_is_current(found_sub, now):
                pay, via = _invoice_payment(found_sub, cust, loc), "invoice"
    if not pay:
        raise PaymentError("We couldn't find a completed payment for this link.")
    if not _amount_ok(pay, plan):
        raise PaymentError("We couldn't find a completed payment for this link.")
    website = (meta.get("website") or "").strip()
    if not website:
        raise PaymentError("No website was entered at checkout.")
    amount = int(((pay.get("amount_money") or {}).get("amount")) or 0)
    created = _parse_time(pay.get("created_at"))
    pinned = meta.get("apex_subscription_id") or ""
    if via == "invoice" and not pinned:
        upd = _write_meta(order_id, {"apex_subscription_id": found_sub["id"]}, order.get("version"))
        if upd is not None:
            pinned = found_sub["id"]
            order["version"] = upd.get("version", order.get("version"))
    paid = {"order_id": order_id, "website": website, "payment_id": pay.get("id"),
            "amount": amount, "created": created, "buyer_email": pay.get("buyer_email_address") or "",
            "refunded": _refunded(pay), "delivered": meta.get("apex_report_delivered") == "1",
            "order_version": order.get("version"), "plan": plan,
            "customer_id": pay.get("customer_id") or order.get("customer_id") or "",
            "order_created": _parse_time(order.get("created_at")) or created,
            "pinned_subscription": pinned,
            "ready_mailed": meta.get("apex_ready_mailed") == "1",
            "monitor_state": decode_monitor_state(meta.get(MON_KEY)),
            "paid_via": via}
    if (now or time.time()) - created > MAX_AGE_S:
        sub = subscription_for_order(paid, now=now) if plan == "monitor" else None
        if not sub or not sub["current"]:
            raise PaymentError("This report link has expired (links work for 30 days after purchase).")
    return paid


def refund(paid):
    """Full refund. Returns 'succeeded', 'pending' or raises."""
    pid = paid.get("payment_id")
    if not pid or not ORDER_RE.match(pid):
        raise PaymentError("This payment can't be refunded automatically.")
    body = {"idempotency_key": ("apex-refund-" + paid["order_id"])[:45],
            "payment_id": pid,
            "amount_money": {"amount": int(paid["amount"]), "currency": "USD"},
            "reason": "Website could not be checked (automatic refund)"}
    code, out = _request("POST", "/v2/refunds", body)
    if code == 200:
        st = ((out.get("refund") or {}).get("status") or "").upper()
        if st == "COMPLETED":
            return "succeeded"
        if st in ("FAILED", "REJECTED"):
            raise SquareUnavailable(f"refund {st.lower()}")
        return "pending"
    errs = _error_codes(out)
    if "REFUND_AMOUNT_INVALID" in errs:        # already fully refunded
        return "succeeded"
    raise SquareUnavailable(f"refund failed ({code} {','.join(sorted(x for x in errs if x))})")


# ---------------------------------------------------------------- subscriptions

def _sub_public(s, now=None):
    return {"id": s.get("id"), "status": s.get("status") or "",
            "start_date": s.get("start_date"), "charged_through_date": s.get("charged_through_date"),
            "canceled_date": s.get("canceled_date"), "current": subscription_is_current(s, now)}


def _search_subscriptions(customer_ids=None, info=None):
    """Every subscription of OUR plan variation at OUR location (optionally for some
    customers), whatever its status. info["truncated"] is set when the page cap was hit."""
    _, _, loc = _cfg()
    var = monitor_variation_id()
    if not var:
        return []
    filt = {"location_ids": [loc]}
    if customer_ids:
        filt["customer_ids"] = list(customer_ids)[:100]
    cursor, subs = None, []
    for _ in range(MAX_PAGES):
        body = {"limit": 200, "query": {"filter": filt}}
        if cursor:
            body["cursor"] = cursor
        code, out = _request("POST", "/v2/subscriptions/search", body)
        if code != 200:
            raise SquareUnavailable(f"Square answered {code}")
        for s in out.get("subscriptions") or []:
            if s.get("plan_variation_id") == var and s.get("location_id") == loc and ORDER_RE.match(s.get("id") or ""):
                subs.append(s)
        cursor = out.get("cursor")
        if not cursor:
            break
    else:
        if cursor and info is not None:
            info["truncated"] = True
    return subs


def _search_orders(location, *, customer_ids=None, created_from=None, created_to=None, info=None):
    """Orders at our location carrying our marker (SearchOrders, newest first)."""
    filt = {}
    if customer_ids:
        filt["customer_filter"] = {"customer_ids": list(customer_ids)[:10]}
    if created_from is not None:
        filt["date_time_filter"] = {"created_at": {"start_at": _iso(created_from),
                                                   "end_at": _iso(created_to or time.time())}}
    cursor, found = None, []
    for _ in range(MAX_PAGES):
        body = {"location_ids": [location], "limit": 500,
                "query": {"filter": filt, "sort": {"sort_field": "CREATED_AT", "sort_order": "DESC"}}}
        if cursor:
            body["cursor"] = cursor
        code, out = _request("POST", "/v2/orders/search", body)
        if code != 200:
            raise SquareUnavailable(f"Square answered {code}")
        for o in out.get("orders") or []:
            if (o.get("metadata") or {}).get("apex") == MARKER and ORDER_RE.match(o.get("id") or ""):
                found.append(o)
        cursor = out.get("cursor")
        if not cursor:
            break
    else:
        if cursor and info is not None:
            info["truncated"] = True
    return found


def _pay_of(order, cache):
    oid = order.get("id")
    if oid not in cache:
        cache[oid] = _paid_payment(order)
    return cache[oid]


def _order_time(order, pay):
    """When the buyer paid: the payment's created_at. An order with no payment of its own
    (see _invoice_payment) falls back to when Square last touched the order."""
    if pay:
        return _parse_time(pay.get("created_at"))
    for k in ("closed_at", "updated_at", "created_at"):
        t = _parse_time(order.get(k)) if order.get(k) else 0
        if t:
            return t
    return 0


def _pair(orders, subs, times):
    """{order_id: subscription} for one customer's monitor orders and subscriptions.

    Pass ALL the customer's subscriptions (any status), so an old cancelled one keeps its
    own order and can't leave its order free to grab a newer subscription.
    An order that already names its subscription (metadata apex_subscription_id) keeps it.
    The rest pair only when exactly one free subscription was created within PAIR_SLACK_S
    of the order's payment AND that subscription fits no other free order. Anything else is
    ambiguous: no pair (and so nothing is pinned)."""
    by_id = {s["id"]: s for s in subs}
    pairs, claimed = {}, set()
    for o in orders:
        sid = (o.get("metadata") or {}).get("apex_subscription_id")
        if sid in by_id and sid not in claimed:
            pairs[o["id"]] = by_id[sid]
            claimed.add(sid)
    free_orders = [o for o in orders if o["id"] not in pairs]
    free_subs = [s for s in subs if s["id"] not in claimed]

    def fits(o, s):
        t = times.get(o["id"]) or 0
        st = _parse_time(s.get("created_at"))
        return bool(t and st) and abs(st - t) <= PAIR_SLACK_S

    for o in free_orders:
        cands = [s for s in free_subs if fits(o, s)]
        if len(cands) != 1:
            continue
        if sum(1 for o2 in free_orders if fits(o2, cands[0])) != 1:
            continue
        pairs[o["id"]] = cands[0]
    return pairs


def _monitor_orders(orders):
    return [o for o in orders if _plan_of(o.get("metadata")) == "monitor"]


def _customer_monitor_orders(loc, cust, subs, cache):
    """This customer's monitor orders. Square may leave customer_id off a payment-link
    order (it's on the payment), so when the customer filter finds none we look at our
    monitor orders made in the days before the subscriptions started and keep those that
    name this customer on the order or on its payment."""
    orders = _monitor_orders(_search_orders(loc, customer_ids=[cust]))
    if orders:
        return orders
    times = [_parse_time(s.get("created_at")) for s in subs if s.get("created_at")]
    if not times:
        return []
    near = _monitor_orders(_search_orders(loc, created_from=min(times) - PAIR_WINDOW_S,
                                          created_to=max(times) + 3600))
    return [o for o in near if o.get("customer_id") == cust or (_pay_of(o, cache) or {}).get("customer_id") == cust]


def _pair_for(loc, cust, subs, order, cache):
    """The subscription that unambiguously belongs to this order, or None."""
    if not subs:
        return None
    orders = _customer_monitor_orders(loc, cust, subs, cache)
    if not any(o.get("id") == order.get("id") for o in orders):
        orders.append(order)
    times = {o["id"]: _order_time(o, _pay_of(o, cache)) for o in orders}
    return _pair(orders, subs, times).get(order.get("id"))


def subscription_for_order(paid, now=None):
    """{"id","status","start_date","charged_through_date","canceled_date","current"} or None.

    Only for "monitor" orders. Square does not tie a payment-link subscription to its
    order, so we pair by customer + payment time and pin the result on the order (best
    effort). An ambiguous pairing returns None and pins nothing. current: see
    subscription_is_current."""
    if (paid or {}).get("plan") != "monitor" or not monitor_variation_id():
        return None
    cust = paid.get("customer_id") or ""
    if not ORDER_RE.match(cust) or not ORDER_RE.match(paid.get("order_id") or ""):
        return None
    _, _, loc = _cfg()
    subs = _search_subscriptions([cust])
    if not subs:
        return None
    pinned = paid.get("pinned_subscription")
    if pinned:
        for s in subs:
            if s["id"] == pinned:
                return _sub_public(s, now)
    code, out = _request("GET", "/v2/orders/" + urllib.parse.quote(paid["order_id"]))
    if code != 200:
        return None
    order = out.get("order") or {}
    s = _pair_for(loc, cust, subs, order, {})
    if not s:
        return None
    _pin(order, s["id"])
    return _sub_public(s, now)


def _pin(order, sub_id):
    """Best effort: remember the pairing on the order, so later lookups never re-guess."""
    if (order.get("metadata") or {}).get("apex_subscription_id"):
        return
    _write_meta(order.get("id") or "", {"apex_subscription_id": sub_id}, order.get("version"))


def _email_for(pay, customer_id):
    email = ((pay or {}).get("buyer_email_address") or "").strip()
    if not email and ORDER_RE.match(customer_id or ""):
        code, out = _request("GET", "/v2/customers/" + urllib.parse.quote(customer_id))
        if code == 200:
            email = ((out.get("customer") or {}).get("email_address") or "").strip()
    return email if EMAIL_RE.match(email) else ""


def monitored_sites(now=None):
    """[{"subscription_id","customer_id","order_id","website","email","state"}] for every
    CURRENT subscription (ACTIVE and charged through today) of our plan variation at our
    location that pairs unambiguously with one of our monitor orders whose first payment
    covered the first-month price and was not refunded.

    Pairing uses ALL of a customer's subscriptions, the same way subscription_for_order
    does, so both always agree. email is "" when Square has none for the buyer.
    state is the order's monitoring state (see decode_monitor_state)."""
    _, _, loc = _cfg()
    by_cust = {}
    for s in _search_subscriptions():
        if ORDER_RE.match(s.get("customer_id") or ""):
            by_cust.setdefault(s["customer_id"], []).append(s)
    out = []
    for cust, subs in by_cust.items():
        if not any(subscription_is_current(s, now) for s in subs):
            continue
        cache = {}
        orders = _customer_monitor_orders(loc, cust, subs, cache)
        times = {o["id"]: _order_time(o, _pay_of(o, cache)) for o in orders}
        pairs = _pair(orders, subs, times)
        for o in orders:
            s = pairs.get(o["id"])
            meta = o.get("metadata") or {}
            website = (meta.get("website") or "").strip()
            if not s or not website or not subscription_is_current(s, now):
                continue
            pay = _pay_of(o, cache) or _invoice_payment(s, cust, loc)
            if not pay or not _amount_ok(pay, "monitor") or _refunded(pay):
                continue
            out.append({"subscription_id": s["id"], "customer_id": cust, "order_id": o["id"],
                        "website": website, "email": _email_for(pay, cust),
                        "state": decode_monitor_state(meta.get(MON_KEY))})
    return out


def cancel_subscription(subscription_id):
    """{"status": ..., "ends": "YYYY-MM-DD" | None}. Square cancels at the end of the paid
    period: the status stays ACTIVE until then and canceled_date says when it ends.
    Refuses (PaymentError) a subscription that isn't our plan at our location."""
    if not ORDER_RE.match(subscription_id or ""):
        raise PaymentError("This subscription reference isn't valid.")
    _, _, loc = _cfg()
    path = "/v2/subscriptions/" + urllib.parse.quote(subscription_id)
    code, out = _request("GET", path)
    if code == 404:
        raise PaymentError("We couldn't find that subscription.")
    if code != 200:
        raise SquareUnavailable(f"Square answered {code}")
    s = out.get("subscription") or {}
    var = monitor_variation_id()
    if s.get("location_id") != loc or not var or s.get("plan_variation_id") != var:
        raise PaymentError("This subscription is not for this site.")
    if s.get("canceled_date") or s.get("status") not in ("ACTIVE", "PENDING", "PAUSED"):
        return {"status": s.get("status") or "", "ends": s.get("canceled_date")}
    code, out = _request("POST", path + "/cancel", {})
    if code != 200:
        raise SquareUnavailable(f"Square answered {code}")
    s = out.get("subscription") or {}
    return {"status": s.get("status") or "", "ends": s.get("canceled_date")}


# ---------------------------------------------------------------- numbers for the owner

def _list_payments(begin, end, info=None):
    _, _, loc = _cfg()
    cursor, rows = None, []
    for _ in range(MAX_PAGES * 5):
        q = {"begin_time": _iso(begin), "end_time": _iso(end), "location_id": loc,
             "limit": "100", "sort_order": "DESC"}
        if cursor:
            q["cursor"] = cursor
        code, out = _request("GET", "/v2/payments?" + urllib.parse.urlencode(q))
        if code != 200:
            raise SquareUnavailable(f"Square answered {code}")
        rows.extend(out.get("payments") or [])
        cursor = out.get("cursor")
        if not cursor:
            break
    else:
        if cursor and info is not None:
            info["truncated"] = True
    return rows


def _host(website):
    try:
        return (urllib.parse.urlsplit(website).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _variation_phases(obj):
    """[{"ordinal","cadence","periods","type","cents"}] of a SUBSCRIPTION_PLAN_VARIATION, in order."""
    phases = ((obj.get("subscription_plan_variation_data") or {}).get("phases")) or []
    out = []
    for i, p in enumerate(phases):
        pricing = p.get("pricing") or {}
        pm = pricing.get("price_money") or pricing.get("price") or {}
        if not pm and p.get("recurring_price_money"):          # older plans keep the price here
            pm = p["recurring_price_money"]
        out.append({"ordinal": p.get("ordinal", i), "cadence": p.get("cadence"), "periods": p.get("periods"),
                    "type": pricing.get("type") or ("STATIC" if pm else None), "cents": pm.get("amount")})
    out.sort(key=lambda x: x["ordinal"] if isinstance(x["ordinal"], int) else 0)
    return out


def _renewal_cents(variation_id):
    """What our plan charges every month once it settles: the price of its last phase, read
    from Square's catalog (not from env). Raises SquareUnavailable when it can't be read."""
    code, out = _request("GET", "/v2/catalog/object/" + urllib.parse.quote(variation_id))
    phases = _variation_phases((out.get("object") or {}) if code == 200 else {})
    cents = phases[-1]["cents"] if phases else None
    if not isinstance(cents, int) or cents <= 0:
        raise SquareUnavailable("could not read the monitoring plan's price from Square")
    return cents


def sales_summary(days=30, now=None):
    """Sales of OUR checkout orders over the last `days` days, read from Square.

    Counts each COMPLETED USD payment created in the window whose order carries our marker.
    Monthly renewals are charged by Square's subscription billing on orders we didn't
    make, so they are not in gross; they show up as active_subscriptions / mrr_cents.
    active_subscriptions: current subscriptions (ACTIVE, charged through today) that will
    renew; cancelling: current ones already scheduled to end. mrr_cents: what the active
    ones renew at, from each one's price override or the plan's catalog price.
    truncated: True when a page cap was hit, so the numbers may be low.
    recent: newest 20, site = hostname only (never emails or ids); refunded = money was
    actually refunded (refunded_money > 0)."""
    days = max(1, min(int(days), 366))
    now = now or time.time()
    _, _, loc = _cfg()
    info = {"truncated": False}
    begin = now - days * 86400
    orders = {o["id"]: o for o in _search_orders(loc, created_from=begin - PAIR_WINDOW_S, created_to=now, info=info)}
    by_plan = {p: {"count": 0, "gross_cents": 0} for p in PLANS}
    gross = refunded = count = 0
    recent = []
    for pay in _list_payments(begin, now, info=info):
        o = orders.get(pay.get("order_id") or "")
        money = pay.get("amount_money") or {}
        if not o or pay.get("status") != "COMPLETED" or money.get("currency") != "USD":
            continue
        plan = _plan_of(o.get("metadata"))
        if not plan:
            continue
        amt = int(money.get("amount") or 0)
        ref = max(0, min(amt, int(((pay.get("refunded_money") or {}).get("amount")) or 0)))
        count += 1
        gross += amt
        refunded += ref
        by_plan[plan]["count"] += 1
        by_plan[plan]["gross_cents"] += amt
        recent.append({"created": pay.get("created_at") or "", "plan": plan, "amount_cents": amt,
                       "site": _host((o.get("metadata") or {}).get("website") or ""),
                       "refunded": ref > 0})
    recent.sort(key=lambda r: _parse_time(r["created"]), reverse=True)
    current = [s for s in _search_subscriptions(info=info) if subscription_is_current(s, now)]
    active = [s for s in current if not s.get("canceled_date")]
    mrr, renew = 0, None
    for s in active:
        over = (s.get("price_override_money") or {}).get("amount")
        if over is not None:
            mrr += int(over)
            continue
        if renew is None:
            renew = _renewal_cents(monitor_variation_id())
        mrr += renew
    return {"days": days, "orders": count, "gross_cents": gross, "refunded_cents": refunded,
            "net_cents": gross - refunded, "by_plan": by_plan,
            "active_subscriptions": len(active), "cancelling": len(current) - len(active),
            "mrr_cents": mrr, "truncated": info["truncated"], "recent": recent[:20]}


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")


def find_orders(email, now=None, max_pages=5):
    """[{order_id, website, created}] paid by this email in the last 30 days, newest first."""
    if not EMAIL_RE.match(email or ""):
        raise PaymentError("Enter the email address you paid with.")
    _, _, loc = _cfg()
    begin = datetime.fromtimestamp((now or time.time()) - MAX_AGE_S, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor, hits = None, []
    for _ in range(max_pages):
        q = {"begin_time": begin, "location_id": loc, "limit": "100", "sort_order": "DESC"}
        if cursor:
            q["cursor"] = cursor
        code, out = _request("GET", "/v2/payments?" + urllib.parse.urlencode(q))
        if code != 200:
            raise SquareUnavailable(f"Square answered {code}")
        for pay in out.get("payments") or []:
            if (pay.get("buyer_email_address") or "").strip().lower() != email.lower():
                continue
            if pay.get("status") != "COMPLETED" or not ORDER_RE.match(pay.get("order_id") or ""):
                continue
            hits.append(pay["order_id"])
        cursor = out.get("cursor")
        if not cursor:
            break
    found = []
    for oid in hits[:10]:
        try:
            p = get_paid_order(oid, now=now)
        except PaymentError:
            continue
        if not p["refunded"]:
            found.append({"order_id": oid, "website": p["website"], "created": p["created"]})
    found.sort(key=lambda x: -x["created"])
    return found


def _customers_by_email(email):
    """Customer ids whose profile email is this address (SearchCustomers, exact match).
    Square doesn't say whether "exact" ignores case, so a lower-case search runs too and
    every hit is re-checked here without regard to case."""
    ids = []
    for q in dict.fromkeys([email, email.lower()]):
        cursor = None
        for _ in range(3):
            body = {"limit": 100, "query": {"filter": {"email_address": {"exact": q}}}}
            if cursor:
                body["cursor"] = cursor
            code, out = _request("POST", "/v2/customers/search", body)
            if code != 200:
                raise SquareUnavailable(f"Square answered {code}")
            for c in out.get("customers") or []:
                cid = c.get("id") or ""
                if (ORDER_RE.match(cid) and (c.get("email_address") or "").strip().lower() == email.lower()
                        and cid not in ids):
                    ids.append(cid)
            cursor = out.get("cursor")
            if not cursor:
                break
    return ids[:20]


def find_monitor_orders(email, now=None):
    """[{order_id, website, created}] of Monthly Monitoring purchases by this email, newest
    first, with no 30-day limit: customer profiles with this email -> their subscriptions of
    our plan variation -> the order each one pairs with. Only orders whose report link works
    (get_paid_order accepts them: within 30 days, or the subscription is current) are listed.
    Refunded ones are kept, so the buyer can still reach the dashboard and cancel."""
    if not EMAIL_RE.match(email or ""):
        raise PaymentError("Enter the email address you paid with.")
    _, _, loc = _cfg()
    if not monitor_variation_id():
        return []
    custs = _customers_by_email(email)
    if not custs:
        return []
    subs = _search_subscriptions(custs)
    found, seen = [], set()
    for cust in custs:
        mine = [s for s in subs if s.get("customer_id") == cust]
        if not mine:
            continue
        cache = {}
        orders = _customer_monitor_orders(loc, cust, mine, cache)
        times = {o["id"]: _order_time(o, _pay_of(o, cache)) for o in orders}
        pairs = _pair(orders, mine, times)
        for o in orders:
            if o["id"] not in pairs or o["id"] in seen:
                continue
            try:
                p = get_paid_order(o["id"], now=now)
            except PaymentError:
                continue
            seen.add(o["id"])
            found.append({"order_id": o["id"], "website": p["website"], "created": p["created"]})
    found.sort(key=lambda x: -x["created"])
    return found[:10]


def verify_webhook(raw_body, signature_header):
    """True when Square signed this exact body for our notification URL."""
    key = os.environ.get("SQUARE_WEBHOOK_SIGNATURE_KEY", "")
    url = os.environ.get("SQUARE_WEBHOOK_URL", "")
    if not key or not url or not signature_header:
        return False
    if isinstance(raw_body, str):
        raw_body = raw_body.encode()
    mac = hmac.new(key.encode(), url.encode() + raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature_header.strip())


def paid_order_id_from_event(event):
    """The order id of a payment event whose payment is COMPLETED, else None."""
    if not isinstance(event, dict) or not str(event.get("type", "")).startswith("payment."):
        return None
    pay = (((event.get("data") or {}).get("object") or {}).get("payment")) or {}
    oid = pay.get("order_id") or ""
    return oid if pay.get("status") == "COMPLETED" and ORDER_RE.match(oid) else None


def event_kind(event):
    """(kind, id): ("payment_completed", order_id), ("subscription_changed", subscription_id)
    or (None, None). Square sends subscription.created / subscription.updated, but may send
    nothing when a subscription simply reaches its canceled_date, so always re-read status."""
    oid = paid_order_id_from_event(event)
    if oid:
        return "payment_completed", oid
    if isinstance(event, dict) and event.get("type") in ("subscription.created", "subscription.updated"):
        data = event.get("data") or {}
        sid = (((data.get("object") or {}).get("subscription")) or {}).get("id") or data.get("id") or ""
        if isinstance(sid, str) and ORDER_RE.match(sid):
            return "subscription_changed", sid
    return None, None


# ---------------------------------------------------------------- setup (square_cli.py)

def _mask(v):
    v = str(v or "")
    return ("..." + v[-4:]) if len(v) > 8 else "set"


def _dollars(c):
    return f"${c / 100:,.2f}" if isinstance(c, int) else "an unknown price"


def check_setup():
    """[(status, message)] with status in OK / WARN / FAIL. Never includes a token or key,
    and never raises: anything Square says (or fails to say) becomes a FAIL line."""
    res = []
    try:
        _check_setup(res)
    except SquareUnavailable as e:
        res.append(("FAIL", f"could not reach Square ({e})"))
    except Exception as e:                       # a surprising answer must not end in a traceback
        res.append(("FAIL", f"Square gave an answer the check couldn't read ({type(e).__name__})"))
    return res


def _check_setup(res):
    env = env_name()
    res.append(("OK", f"SQUARE_ENV is {env}") if env else ("FAIL", "SQUARE_ENV must be sandbox or production"))
    tok = os.environ.get("SQUARE_ACCESS_TOKEN", "")
    res.append(("OK", "SQUARE_ACCESS_TOKEN is set") if tok else ("FAIL", "SQUARE_ACCESS_TOKEN is not set"))
    loc = os.environ.get("SQUARE_LOCATION_ID", "")
    res.append(("OK", f"SQUARE_LOCATION_ID is set ({_mask(loc)})") if loc else
               ("FAIL", "SQUARE_LOCATION_ID is not set"))
    over = os.environ.get("SQUARE_API_BASE", "").strip()
    if over and not _test_mode():
        if not over.lower().startswith("https://"):
            res.append(("FAIL", "SQUARE_API_BASE is set to a plain-http address; remove it (it's only for tests)"))
            return
        res.append(("WARN", "SQUARE_API_BASE is set but ignored (it's for tests); SQUARE_ENV picks Square's address"))
    prices = [(PLANS[p]["price_env"], plan_info(p)["cents"], p) for p in PLANS]
    prices.insert(2, ("MONITOR_FIRST_CENTS", plan_info("monitor")["first_cents"], "monitor first month"))
    for key, cents, what in prices:
        raw = os.environ.get(key)
        bad = raw is not None and raw.strip() != str(cents)
        res.append(("WARN" if bad else "OK", f"{what} price is {_dollars(cents)}"
                    + (f" ({key} is not usable, so the default is used)" if bad else "")))
    mon = plan_info("monitor")
    if mon["first_cents"] < plan_info("checkup")["cents"]:
        res.append(("WARN", "the first month of monitoring costs less than a Website Checkup, "
                            "so monitoring undercuts the Checkup"))
    for k in ("SQUARE_WEBHOOK_SIGNATURE_KEY", "SQUARE_WEBHOOK_URL"):
        res.append(("OK", f"{k} is set") if os.environ.get(k) else
                   ("WARN", f"{k} is not set (reports then start only when the buyer lands)"))
    if not (tok and env and loc):
        res.append(("FAIL", "skipped the Square checks because settings above are missing"))
        return
    try:
        code, out = _request("GET", "/v2/locations")
    except SquareUnavailable as e:
        res.append(("FAIL", f"could not reach Square ({e})"))
        return
    if code == 401:
        other = "production" if env == "sandbox" else "sandbox"
        res.append(("FAIL", f"Square refused the access token (wrong token, or a {other} token?)"))
        return
    if code != 200:
        res.append(("FAIL", f"Square answered {code} to the locations check"))
        return
    res.append(("OK", f"the access token works with Square {env}"))
    match = [x for x in out.get("locations") or [] if x.get("id") == loc]
    if not match:
        res.append(("FAIL", "SQUARE_LOCATION_ID is not one of this account's locations"))
    else:
        L = match[0]
        if L.get("status") != "ACTIVE":
            res.append(("FAIL", "the location is not ACTIVE"))
        else:
            res.append(("OK", "location found: " + str(L.get("name") or "")[:60]))
        if L.get("currency") and L.get("currency") != "USD":
            res.append(("FAIL", f"the location's currency is {L.get('currency')}, but the prices are in USD"))
    var = os.environ.get("SQUARE_MONITOR_PLAN_VARIATION_ID", "").strip()
    if not var:
        res.append(("WARN", "SQUARE_MONITOR_PLAN_VARIATION_ID is not set, so Monthly Monitoring is hidden "
                            "(run create-monitor-plan)"))
        return
    if not monitor_variation_id():
        res.append(("FAIL", "SQUARE_MONITOR_PLAN_VARIATION_ID doesn't look like a Square catalog id"))
        return
    try:
        code, out = _request("GET", "/v2/catalog/object/" + urllib.parse.quote(var))
    except SquareUnavailable as e:
        res.append(("FAIL", f"could not read the monitoring plan from Square ({e})"))
        return
    obj = (out.get("object") or {}) if code == 200 else {}
    if obj.get("type") != "SUBSCRIPTION_PLAN_VARIATION" or obj.get("is_deleted"):
        res.append(("FAIL", "SQUARE_MONITOR_PLAN_VARIATION_ID is not a subscription plan variation in this "
                            "account (sandbox and production have different ids)"))
        return
    res.extend(_check_phases(_variation_phases(obj), mon["first_cents"], mon["cents"]))


def _check_phases(phases, first, monthly):
    """The plan must be: phase 0 monthly for 1 period at `first`, then phase 1 monthly with
    no end at `monthly`, both STATIC."""
    shape_ok = (len(phases) == 2
                and all(p["cadence"] == "MONTHLY" and p["type"] == "STATIC" for p in phases)
                and phases[0]["periods"] == 1 and not phases[1]["periods"])
    if not shape_ok:
        return [("FAIL", "the plan variation isn't this version's two-phase plan (first month "
                         f"{_dollars(first)}, then {_dollars(monthly)} every month). Run "
                         "create-monitor-plan --force and paste the new variation id")]
    out = []
    if phases[0]["cents"] != first:
        out.append(("FAIL", f"the plan variation charges {phases[0]['cents']} cents for the first month "
                            f"but MONITOR_FIRST_CENTS is {first}"))
    if phases[1]["cents"] != monthly:
        out.append(("FAIL", f"the plan variation charges {phases[1]['cents']} cents a month after the first "
                            f"but MONITOR_PRICE_CENTS is {monthly}"))
    if not out:
        out.append(("OK", f"Monthly Monitoring plan found: {_dollars(first)} for the first month, "
                          f"then {_dollars(monthly)} a month"))
    return out


def create_monitor_plan(cents=None, first_cents=None):
    """Make the catalog SUBSCRIPTION_PLAN and its two-phase variation (no items):
    phase 0 MONTHLY for 1 period at first_cents (MONITOR_FIRST_CENTS), then phase 1 MONTHLY
    with no end at cents (MONITOR_PRICE_CENTS). Both STATIC.

    {"plan_id","variation_id","cents","first_cents"}. The caller (square_cli.py) guards production."""
    info = plan_info("monitor")
    cents = int(cents or info["cents"])
    first_cents = int(first_cents or info["first_cents"])
    for c in (cents, first_cents):
        if not 100 <= c <= 100000:
            raise PaymentError("prices must be between 100 and 100000 cents")
    seed = str(uuid.uuid4())
    plan_body = {"idempotency_key": "apex-mplan-" + seed, "object": {
        "type": "SUBSCRIPTION_PLAN", "id": "#apex-monitor-plan",
        "subscription_plan_data": {"name": "Apex Forge Monthly Monitoring"}}}
    code, out = _request("POST", "/v2/catalog/object", plan_body)
    plan_id = (out.get("catalog_object") or {}).get("id") or ""
    if code != 200 or not CATALOG_ID_RE.match(plan_id):
        raise SquareUnavailable(f"Square answered {code} when creating the plan")

    def phase(ordinal, amount, periods=None):
        p = {"cadence": "MONTHLY", "ordinal": ordinal,
             "pricing": {"type": "STATIC", "price_money": {"amount": amount, "currency": "USD"}}}
        if periods:
            p["periods"] = periods
        return p

    var_body = {"idempotency_key": "apex-mvar-" + seed, "object": {
        "type": "SUBSCRIPTION_PLAN_VARIATION", "id": "#apex-monitor-monthly",
        "subscription_plan_variation_data": {
            "name": "Monthly", "subscription_plan_id": plan_id,
            "phases": [phase(0, first_cents, periods=1), phase(1, cents)]}}}
    code, out = _request("POST", "/v2/catalog/object", var_body)
    var_id = (out.get("catalog_object") or {}).get("id") or ""
    if code != 200 or not CATALOG_ID_RE.match(var_id):
        raise SquareUnavailable(f"Square answered {code} when creating the monthly variation")
    return {"plan_id": plan_id, "variation_id": var_id, "cents": cents, "first_cents": first_cents}
