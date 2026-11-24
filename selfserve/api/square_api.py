"""square_api.py - the Square calls the checkup needs, over plain HTTPS.

Card data never reaches this server: the buyer pays on Square's own hosted
checkout page (card, Cash App Pay, Apple Pay, Google Pay, whatever the Square
account has switched on). This module only talks to Square's API:

* create_checkout(website): POST /v2/online-checkout/payment-links with an
  order WE price ($29 from CHECKUP_PRICE_CENTS, never from the browser) and the
  website stored on the order's metadata. Returns the hosted checkout URL and
  the order id. The order id is the buyer's report reference.
* get_paid_order(order_id): GET /v2/orders/{id}, then GET /v2/payments/{id}
  for its tender. Refuses unless the order is ours (location + metadata
  marker), the payment is COMPLETED, covers the price, is under 30 days old,
  and the website is on the order. Also reports refunded / delivered state,
  read from Square, so it survives a server restart.
* mark_delivered(order): best effort, PUT /v2/orders/{id} metadata
  apex_report_delivered=1 (no automatic refund after delivery).
* refund(payment, order_id): POST /v2/refunds for the full amount with an
  idempotency key derived from the order, so a double click can't refund twice.
* find_orders(email): completed payments in the last 30 days whose buyer email
  matches, for "Find my report".
* verify_webhook(body, signature_header): HMAC-SHA256 over notification URL +
  raw body with the subscription's signature key, base64, constant-time compare.

Configuration comes from environment variables only (never files):
  SQUARE_ACCESS_TOKEN            access token of the Square application (sandbox or production)
  SQUARE_ENV                     "sandbox" (default) or "production"
  SQUARE_LOCATION_ID             the location the sales go to
  SQUARE_WEBHOOK_SIGNATURE_KEY   signature key of the webhook subscription
  SQUARE_WEBHOOK_URL             the exact notification URL registered for it
  CHECKUP_PRICE_CENTS            default 2900 ($29.00 USD)
  SQUARE_VERSION                 Square-Version header (default below)
  SQUARE_API_BASE                tests only
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
MAX_AGE_S = 30 * 24 * 3600
MARKER = "apex-checkup-v1"
DEFAULT_VERSION = "2025-01-23"
BASES = {"sandbox": "https://connect.squareupsandbox.com", "production": "https://connect.squareup.com"}
# hosts Square's hosted checkout may live on; the site only follows these
CHECKOUT_HOSTS = ("square.link", "checkout.square.site", "squareup.com", "squareupsandbox.com")


class PaymentError(Exception):
    """No usable paid order for this link. Message is safe to show."""


class SquareUnavailable(Exception):
    """Square could not be reached, or the server is not configured."""


def _test_mode():
    return os.environ.get("SELFSERVE_TEST_ALLOW_LOOPBACK") == "1"


def env_name():
    e = os.environ.get("SQUARE_ENV", "sandbox").strip().lower()
    return e if e in BASES else None


def price_cents():
    try:
        p = int(os.environ.get("CHECKUP_PRICE_CENTS", "2900"))
    except ValueError:
        p = 2900
    return p if 100 <= p <= 100000 else 2900


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
        raise SquareUnavailable(f"could not reach Square: {e}") from None


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


def create_checkout(website, redirect_url):
    """(checkout_url, order_id). website must already be normalized."""
    _, _, loc = _cfg()
    cents = price_cents()
    body = {
        "idempotency_key": str(uuid.uuid4()),
        "description": "Apex Forge Website Checkup",
        "order": {
            "location_id": loc,
            "line_items": [{"name": "Website Checkup", "quantity": "1",
                            "note": website[:500],
                            "base_price_money": {"amount": cents, "currency": "USD"}}],
            "metadata": {"apex": MARKER, "website": website[:255]},
        },
        "checkout_options": {"redirect_url": redirect_url, "ask_for_shipping_address": False},
        "payment_note": ("Website Checkup: " + website)[:500],
    }
    code, out = _request("POST", "/v2/online-checkout/payment-links", body)
    if code != 200:
        raise SquareUnavailable(f"Square answered {code}")
    link = out.get("payment_link") or {}
    url, order_id = link.get("url") or "", link.get("order_id") or ""
    if not checkout_url_ok(url) or not ORDER_RE.match(order_id):
        raise SquareUnavailable("Square returned an unexpected checkout link")
    return url, order_id


def _parse_time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


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
    if order.get("location_id") != loc or meta.get("apex") != MARKER:
        raise PaymentError("This payment reference is not for this site.")
    pay = _paid_payment(order)
    if not pay:
        raise PaymentError("We couldn't find a completed payment for this link.")
    amount = int(((pay.get("amount_money") or {}).get("amount")) or 0)
    if amount < price_cents() or (pay.get("amount_money") or {}).get("currency") != "USD":
        raise PaymentError("We couldn't find a completed payment for this link.")
    created = _parse_time(pay.get("created_at"))
    if (now or time.time()) - created > MAX_AGE_S:
        raise PaymentError("This report link has expired (links work for 30 days after purchase).")
    website = (meta.get("website") or "").strip()
    if not website:
        raise PaymentError("No website was entered at checkout.")
    refunded = int(((pay.get("refunded_money") or {}).get("amount")) or 0) > 0 or bool(pay.get("refund_ids"))
    return {"order_id": order_id, "website": website, "payment_id": pay.get("id"),
            "amount": amount, "created": created, "buyer_email": pay.get("buyer_email_address") or "",
            "refunded": refunded, "delivered": meta.get("apex_report_delivered") == "1",
            "order_version": order.get("version")}


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

