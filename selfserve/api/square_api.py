"""square_api.py - the Square calls Apex Forge needs, over plain HTTPS.

Card data never reaches this server: the buyer pays on Square's own hosted
checkout page (card, Apple Pay, Google Pay, whatever the Square account has
switched on). This module only talks to Square's API.

Plans (PLANS below; prices come from env, never from the browser):
  checkup  one-time  Website Checkup                       CHECKUP_PRICE_CENTS  (2900)
  pro      one-time  Pro Checkup (homepage + 4 more pages)  PRO_PRICE_CENTS      (7900)
  monitor  monthly   Monthly Monitoring                    MONITOR_PRICE_CENTS  (1900)
  "monitor" is offered only when SQUARE_MONITOR_PLAN_VARIATION_ID is set.

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
  is COMPLETED, covers that plan's price, is under 30 days old (for "monitor" the
  30 days are waived while the subscription is ACTIVE), and the website is on the order.
* subscription_for_order / monitored_sites / cancel_subscription: the Subscriptions API.
  Square does not link a payment-link subscription to its order, so we pair them by
  customer + time (see _pair) and pin the pair on the order's metadata once found.
* sales_summary(days): money numbers read from Square (ListPayments + SearchOrders +
  SearchSubscriptions), so they survive any restart or wiped disk.
* mark_delivered, refund, find_orders, verify_webhook, paid_order_id_from_event,
  event_kind: see their docstrings.
* check_setup() and create_monitor_plan(): used by square_cli.py (doctor, create-monitor-plan).

Configuration comes from environment variables only (never files):
  SQUARE_ACCESS_TOKEN               access token of the Square application (sandbox or production)
  SQUARE_ENV                        "sandbox" (default) or "production"
  SQUARE_LOCATION_ID                the location the sales go to
  SQUARE_MONITOR_PLAN_VARIATION_ID  catalog id of the monthly plan variation (optional)
  SQUARE_WEBHOOK_SIGNATURE_KEY      signature key of the webhook subscription
  SQUARE_WEBHOOK_URL                the exact notification URL registered for it
  CHECKUP_PRICE_CENTS / PRO_PRICE_CENTS / MONITOR_PRICE_CENTS
  SQUARE_VERSION                    Square-Version header (default below)
  SQUARE_API_BASE                   tests only
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
MAX_AGE_S = 30 * 24 * 3600
MARKER = "apex-checkup-v1"
DEFAULT_VERSION = "2025-01-23"
BASES = {"sandbox": "https://connect.squareupsandbox.com", "production": "https://connect.squareup.com"}
# hosts Square's hosted checkout may live on; the site only follows these
CHECKOUT_HOSTS = ("square.link", "checkout.square.site", "squareup.com", "squareupsandbox.com")
PAIR_WINDOW_S = 3 * 24 * 3600          # a subscription starts within this long of its checkout order
MAX_PAGES = 20

PLANS = {
    "checkup": {"label": "Website Checkup", "price_env": "CHECKUP_PRICE_CENTS",
                "default_cents": 2900, "kind": "one_time", "mode": "full"},
    "pro": {"label": "Pro Checkup (homepage + up to 4 more pages)", "price_env": "PRO_PRICE_CENTS",
            "default_cents": 7900, "kind": "one_time", "mode": "pro"},
    "monitor": {"label": "Monthly Monitoring (full report now, re-checked every month, alerts on changes)",
                "price_env": "MONITOR_PRICE_CENTS", "default_cents": 1900, "kind": "subscription",
                "mode": "full"},
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
    """{"id","label","cents","kind","mode","available"} or None for an unknown plan."""
    p = PLANS.get(plan_id) if isinstance(plan_id, str) else None
    if not p:
        return None
    return {"id": plan_id, "label": p["label"], "cents": _cents(p["price_env"], p["default_cents"]),
            "kind": p["kind"], "mode": p["mode"],
            "available": plan_id != "monitor" or bool(monitor_variation_id())}


def public_plans():
    """What the site may show: [{"id","label","cents","kind","available"}] (no ids, no secrets)."""
    out = []
    for pid in PLANS:
        i = plan_info(pid)
        out.append({k: i[k] for k in ("id", "label", "cents", "kind", "available")})
    return out


def _cfg():
    token = os.environ.get("SQUARE_ACCESS_TOKEN", "")
    env = env_name()
    loc = os.environ.get("SQUARE_LOCATION_ID", "")
    if not token or not env or not loc:
        raise SquareUnavailable("payments are not configured on this server")
    base = os.environ.get("SQUARE_API_BASE", "").rstrip("/") or BASES[env]
    if not base.startswith("https://") and not _test_mode():
        raise RuntimeError("SQUARE_API_BASE must be https")
    return token, base, loc


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
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SquareUnavailable(f"could not reach Square: {type(e).__name__}") from None


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
    meta = {"apex": MARKER, "website": website[:255]}
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
    money = {"amount": info["cents"], "currency": "USD"}
    options = {"redirect_url": redirect_url, "ask_for_shipping_address": False}
    body = {"idempotency_key": str(uuid.uuid4()),
            "description": "Apex Forge " + _short(info["label"]),
            "checkout_options": options,
            "payment_note": (_short(info["label"]) + ": " + website)[:500]}
    if plan == "monitor":
        # Square: subscription payment links take quick_pay only; price_money should
        # equal the variation's phase price (a different amount acts as an override).
        body["quick_pay"] = {"name": "Apex Forge Monthly Monitoring", "price_money": money, "location_id": loc}
        options["subscription_plan_id"] = monitor_variation_id()
    else:
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


def _tag_link_order(order_id, website, plan, link_out):
    """UpdateOrder metadata on the order a quick_pay link made. True when Square kept it."""
    version = None
    for o in ((link_out.get("related_resources") or {}).get("orders") or []):
        if o.get("id") == order_id:
            version = o.get("version")
    if version is None:
        code, out = _request("GET", "/v2/orders/" + urllib.parse.quote(order_id))
        if code != 200:
            return False
        version = (out.get("order") or {}).get("version")
    body = {"idempotency_key": ("apex-meta-" + order_id)[:45],
            "order": {"version": version, "metadata": _order_meta(website, plan)}}
    try:
        code, out = _request("PUT", "/v2/orders/" + urllib.parse.quote(order_id), body)
    except SquareUnavailable:
        return False
    meta = ((out.get("order") or {}).get("metadata") or {}) if code == 200 else {}
    return meta.get("apex") == MARKER and meta.get("website") == website[:255] and meta.get("plan") == plan


def _delete_link(link_id):
    if not ORDER_RE.match(link_id or ""):
        return
    try:
        _request("DELETE", "/v2/online-checkout/payment-links/" + urllib.parse.quote(link_id))
    except SquareUnavailable:
        pass


def _parse_time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _plan_of(meta):
    p = (meta or {}).get("plan") or "checkup"
    return p if p in PLANS else None


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
    pay = _paid_payment(order)
    if not pay:
        raise PaymentError("We couldn't find a completed payment for this link.")
    amount = int(((pay.get("amount_money") or {}).get("amount")) or 0)
    if amount < plan_info(plan)["cents"] or (pay.get("amount_money") or {}).get("currency") != "USD":
        raise PaymentError("We couldn't find a completed payment for this link.")
    website = (meta.get("website") or "").strip()
    if not website:
        raise PaymentError("No website was entered at checkout.")
    created = _parse_time(pay.get("created_at"))
    refunded = int(((pay.get("refunded_money") or {}).get("amount")) or 0) > 0 or bool(pay.get("refund_ids"))
    paid = {"order_id": order_id, "website": website, "payment_id": pay.get("id"),
            "amount": amount, "created": created, "buyer_email": pay.get("buyer_email_address") or "",
            "refunded": refunded, "delivered": meta.get("apex_report_delivered") == "1",
            "order_version": order.get("version"), "plan": plan,
            "customer_id": pay.get("customer_id") or order.get("customer_id") or "",
            "order_created": _parse_time(order.get("created_at")) or created,
            "pinned_subscription": meta.get("apex_subscription_id") or ""}
    if (now or time.time()) - created > MAX_AGE_S:
        sub = subscription_for_order(paid) if plan == "monitor" else None
        if not sub or sub["status"] != "ACTIVE":
            raise PaymentError("This report link has expired (links work for 30 days after purchase).")
    return paid


def mark_delivered(paid):
    """Best effort. Returns True when Square recorded it on the order."""
    if paid.get("delivered"):
        return True
    body = {"idempotency_key": "apex-deliv-" + paid["order_id"][:30],
            "order": {"version": paid.get("order_version"),
                      "metadata": {"apex_report_delivered": "1"}}}
    try:
        code, _ = _request("PUT", "/v2/orders/" + urllib.parse.quote(paid["order_id"]), body)
    except SquareUnavailable:
        return False
    return code == 200


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
    errs = {e.get("code") for e in (out.get("errors") or [])}
    if "REFUND_AMOUNT_INVALID" in errs:        # already fully refunded
        return "succeeded"
    raise SquareUnavailable(f"refund failed ({code} {','.join(sorted(x for x in errs if x))})")


# ---------------------------------------------------------------- subscriptions

def _sub_public(s):
    return {"id": s.get("id"), "status": s.get("status") or "",
            "start_date": s.get("start_date"), "charged_through_date": s.get("charged_through_date"),
            "canceled_date": s.get("canceled_date")}


def _search_subscriptions(customer_ids=None):
    """Every subscription of OUR plan variation at OUR location (optionally for some customers)."""
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
    return subs


def _search_orders(location, *, customer_ids=None, created_from=None, created_to=None):
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
    return found


def _pair(orders, subs):
    """{order_id: subscription} for one customer's monitor orders and subscriptions.

    An order that already names its subscription (metadata apex_subscription_id) keeps it.
    The rest go oldest order first to the unclaimed subscription created closest after it
    (within PAIR_WINDOW_S; an hour of clock slack before)."""
    by_id = {s["id"]: s for s in subs}
    pairs, claimed = {}, set()
    for o in orders:
        sid = (o.get("metadata") or {}).get("apex_subscription_id")
        if sid in by_id and sid not in claimed:
            pairs[o["id"]] = by_id[sid]
            claimed.add(sid)
    for o in sorted(orders, key=lambda o: _parse_time(o.get("created_at"))):
        if o["id"] in pairs:
            continue
        t = _parse_time(o.get("created_at"))
        best = None
        for s in subs:
            if s["id"] in claimed:
                continue
            d = _parse_time(s.get("created_at")) - t
            if -3600 <= d <= PAIR_WINDOW_S and (best is None or d < best[0]):
                best = (d, s)
        if best:
            pairs[o["id"]] = best[1]
            claimed.add(best[1]["id"])
    return pairs


def _monitor_orders(orders):
    return [o for o in orders if _plan_of(o.get("metadata")) == "monitor"]


def subscription_for_order(paid):
    """{"id","status","start_date","charged_through_date","canceled_date"} or None.

    Only for "monitor" orders. Square does not tie a payment-link subscription to its
    order, so we pair by customer + time and pin the result on the order (best effort)."""
    if (paid or {}).get("plan") != "monitor" or not monitor_variation_id():
        return None
    cust = paid.get("customer_id") or ""
    if not ORDER_RE.match(cust):
        return None
    _, _, loc = _cfg()
    subs = _search_subscriptions([cust])
    if not subs:
        return None
    pinned = paid.get("pinned_subscription")
    if pinned:
        for s in subs:
            if s["id"] == pinned:
                return _sub_public(s)
    orders = _customer_monitor_orders(loc, cust, subs)
    if not any(o.get("id") == paid["order_id"] for o in orders):
        orders.append({"id": paid["order_id"], "metadata": {"plan": "monitor", "apex": MARKER},
                       "created_at": _iso(paid.get("order_created") or paid.get("created") or 0)})
    s = _pair(orders, subs).get(paid["order_id"])
    if not s:
        return None
    _pin(paid["order_id"], s["id"])
    return _sub_public(s)


def _pin(order_id, sub_id):
    """Best effort: remember the pairing on the order, so later lookups never re-guess."""
    try:
        code, out = _request("GET", "/v2/orders/" + urllib.parse.quote(order_id))
        if code != 200:
            return
        order = out.get("order") or {}
        if (order.get("metadata") or {}).get("apex_subscription_id"):
            return
        _request("PUT", "/v2/orders/" + urllib.parse.quote(order_id),
                 {"idempotency_key": ("apex-pin-" + order_id)[:45],
                  "order": {"version": order.get("version"), "metadata": {"apex_subscription_id": sub_id}}})
    except SquareUnavailable:
        pass


def _email_for(order, customer_id):
    pay = _paid_payment(order)
    email = ((pay or {}).get("buyer_email_address") or "").strip()
    if not email and ORDER_RE.match(customer_id or ""):
        code, out = _request("GET", "/v2/customers/" + urllib.parse.quote(customer_id))
        if code == 200:
            email = ((out.get("customer") or {}).get("email_address") or "").strip()
    return email if EMAIL_RE.match(email) else ""


def _customer_monitor_orders(loc, cust, subs):
    """This customer's monitor orders. Square may leave customer_id off a payment-link
    order (it's on the payment), so when the customer filter finds none we look at our
    monitor orders made around the subscriptions' start and keep those whose payment
    names this customer."""
    orders = _monitor_orders(_search_orders(loc, customer_ids=[cust]))
    if orders:
        return orders
    times = [_parse_time(s.get("created_at")) for s in subs if s.get("created_at")]
    if not times:
        return []
    near = _monitor_orders(_search_orders(loc, created_from=min(times) - PAIR_WINDOW_S,
                                          created_to=max(times) + 3600))
    return [o for o in near if (_paid_payment(o) or {}).get("customer_id") == cust]


def monitored_sites():
    """[{"subscription_id","customer_id","order_id","website","email"}] for every ACTIVE
    subscription of our plan variation at our location that pairs with one of our orders.
    email is "" when Square has none for the buyer."""
    _, _, loc = _cfg()
    by_cust = {}
    for s in _search_subscriptions():
        if s.get("status") == "ACTIVE" and ORDER_RE.match(s.get("customer_id") or ""):
            by_cust.setdefault(s["customer_id"], []).append(s)
    out = []
    for cust, subs in by_cust.items():
        orders = _customer_monitor_orders(loc, cust, subs)
        pairs = _pair(orders, subs)
        for o in orders:
            s = pairs.get(o["id"])
            website = ((o.get("metadata") or {}).get("website") or "").strip()
            if not s or not website:
                continue
            out.append({"subscription_id": s["id"], "customer_id": cust, "order_id": o["id"],
                        "website": website, "email": _email_for(o, cust)})
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

def _list_payments(begin, end):
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
    return rows


def _host(website):
    try:
        return (urllib.parse.urlsplit(website).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def sales_summary(days=30, now=None):
    """Sales of OUR checkout orders over the last `days` days, read from Square.

    Counts each COMPLETED USD payment created in the window whose order carries our marker.
    Monthly renewals are charged by Square's subscription billing on orders we didn't
    make, so they are not in gross; they show up as active_subscriptions / mrr_cents.
    mrr_cents leaves out subscriptions already scheduled to cancel.
    recent: newest 20, site = hostname only (never emails or ids)."""
    days = max(1, min(int(days), 366))
    now = now or time.time()
    _, _, loc = _cfg()
    begin = now - days * 86400
    orders = {o["id"]: o for o in _search_orders(loc, created_from=begin - PAIR_WINDOW_S, created_to=now)}
    by_plan = {p: {"count": 0, "gross_cents": 0} for p in PLANS}
    gross = refunded = count = 0
    recent = []
    for pay in _list_payments(begin, now):
        o = orders.get(pay.get("order_id") or "")
        money = pay.get("amount_money") or {}
        if not o or pay.get("status") != "COMPLETED" or money.get("currency") != "USD":
            continue
        plan = _plan_of(o.get("metadata"))
        if not plan:
            continue
        amt = int(money.get("amount") or 0)
        ref = min(amt, int(((pay.get("refunded_money") or {}).get("amount")) or 0))
        count += 1
        gross += amt
        refunded += ref
        by_plan[plan]["count"] += 1
        by_plan[plan]["gross_cents"] += amt
        recent.append({"created": pay.get("created_at") or "", "plan": plan, "amount_cents": amt,
                       "site": _host((o.get("metadata") or {}).get("website") or ""),
                       "refunded": ref > 0 or bool(pay.get("refund_ids"))})
    recent.sort(key=lambda r: _parse_time(r["created"]), reverse=True)
    active = [s for s in _search_subscriptions() if s.get("status") == "ACTIVE"]
    mcents = plan_info("monitor")["cents"]
    mrr = 0
    for s in active:
        if s.get("canceled_date"):
            continue
        over = (s.get("price_override_money") or {}).get("amount")
        mrr += int(over) if over is not None else mcents
    return {"days": days, "orders": count, "gross_cents": gross, "refunded_cents": refunded,
            "net_cents": gross - refunded, "by_plan": by_plan,
            "active_subscriptions": len(active), "mrr_cents": mrr, "recent": recent[:20]}


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


def _variation_price(obj):
    """(cents, cadence, phase count) of a SUBSCRIPTION_PLAN_VARIATION catalog object."""
    phases = ((obj.get("subscription_plan_variation_data") or {}).get("phases")) or []
    static = [p for p in phases if (p.get("pricing") or {}).get("type") == "STATIC"]
    if not static:
        return None, None, len(phases)
    pricing = static[-1].get("pricing") or {}
    pm = pricing.get("price_money") or pricing.get("price") or {}
    return pm.get("amount"), static[-1].get("cadence"), len(phases)


def check_setup():
    """[(status, message)] with status in OK / WARN / FAIL. Never includes a token or key."""
    res = []
    env = env_name()
    res.append(("OK", f"SQUARE_ENV is {env}") if env else ("FAIL", "SQUARE_ENV must be sandbox or production"))
    tok = os.environ.get("SQUARE_ACCESS_TOKEN", "")
    res.append(("OK", "SQUARE_ACCESS_TOKEN is set") if tok else ("FAIL", "SQUARE_ACCESS_TOKEN is not set"))
    loc = os.environ.get("SQUARE_LOCATION_ID", "")
    res.append(("OK", f"SQUARE_LOCATION_ID is set ({_mask(loc)})") if loc else
               ("FAIL", "SQUARE_LOCATION_ID is not set"))
    for p in PLANS:
        i = plan_info(p)
        raw = os.environ.get(PLANS[p]["price_env"])
        bad = raw is not None and raw.strip() != str(i["cents"])
        res.append(("WARN" if bad else "OK", f"{p} price is ${i['cents'] / 100:.2f}"
                    + (f" ({PLANS[p]['price_env']} is not usable, so the default is used)" if bad else "")))
    for k in ("SQUARE_WEBHOOK_SIGNATURE_KEY", "SQUARE_WEBHOOK_URL"):
        res.append(("OK", f"{k} is set") if os.environ.get(k) else
                   ("WARN", f"{k} is not set (reports then start only when the buyer lands)"))
    if not (tok and env and loc):
        res.append(("FAIL", "skipped the Square checks because settings above are missing"))
        return res
    try:
        code, out = _request("GET", "/v2/locations")
    except SquareUnavailable as e:
        res.append(("FAIL", f"could not reach Square ({e})"))
        return res
    if code == 401:
        other = "production" if env == "sandbox" else "sandbox"
        res.append(("FAIL", f"Square refused the access token (wrong token, or a {other} token?)"))
        return res
    if code != 200:
        res.append(("FAIL", f"Square answered {code} to the locations check"))
        return res
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
        return res
    if not monitor_variation_id():
        res.append(("FAIL", "SQUARE_MONITOR_PLAN_VARIATION_ID doesn't look like a Square catalog id"))
        return res
    code, out = _request("GET", "/v2/catalog/object/" + urllib.parse.quote(var))
    obj = (out.get("object") or {}) if code == 200 else {}
    if obj.get("type") != "SUBSCRIPTION_PLAN_VARIATION" or obj.get("is_deleted"):
        res.append(("FAIL", "SQUARE_MONITOR_PLAN_VARIATION_ID is not a subscription plan variation in this "
                            "account (sandbox and production have different ids)"))
        return res
    cents, cadence, nphase = _variation_price(obj)
    want = plan_info("monitor")["cents"]
    if cadence != "MONTHLY" or nphase != 1:
        res.append(("WARN", "the plan variation is not one monthly phase, but the site says it renews monthly"))
    if cents != want:
        res.append(("FAIL", f"the plan variation charges {cents} cents but MONITOR_PRICE_CENTS is {want}"))
    else:
        res.append(("OK", f"Monthly Monitoring plan found: ${want / 100:.2f} a month"))
    return res


def create_monitor_plan(cents=None):
    """Make the catalog SUBSCRIPTION_PLAN + one monthly STATIC-priced variation (no items).

    {"plan_id","variation_id","cents"}. The caller (square_cli.py) guards production."""
    cents = int(cents or plan_info("monitor")["cents"])
    if not 100 <= cents <= 100000:
        raise PaymentError("the monthly price must be between 100 and 100000 cents")
    seed = str(uuid.uuid4())
    plan_body = {"idempotency_key": "apex-mplan-" + seed, "object": {
        "type": "SUBSCRIPTION_PLAN", "id": "#apex-monitor-plan",
        "subscription_plan_data": {"name": "Apex Forge Monthly Monitoring"}}}
    code, out = _request("POST", "/v2/catalog/object", plan_body)
    plan_id = (out.get("catalog_object") or {}).get("id") or ""
    if code != 200 or not CATALOG_ID_RE.match(plan_id):
        raise SquareUnavailable(f"Square answered {code} when creating the plan")
    var_body = {"idempotency_key": "apex-mvar-" + seed, "object": {
        "type": "SUBSCRIPTION_PLAN_VARIATION", "id": "#apex-monitor-monthly",
        "subscription_plan_variation_data": {
            "name": "Monthly", "subscription_plan_id": plan_id,
            "phases": [{"cadence": "MONTHLY", "ordinal": 0,
                        "pricing": {"type": "STATIC", "price_money": {"amount": cents, "currency": "USD"}}}]}}}
    code, out = _request("POST", "/v2/catalog/object", var_body)
    var_id = (out.get("catalog_object") or {}).get("id") or ""
    if code != 200 or not CATALOG_ID_RE.match(var_id):
        raise SquareUnavailable(f"Square answered {code} when creating the monthly variation")
    return {"plan_id": plan_id, "variation_id": var_id, "cents": cents}
