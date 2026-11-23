"""stripe_api.py - the two Stripe calls the checkup needs, over plain HTTPS.

* get_paid_session(session_id): GET /v1/checkout/sessions/{id}, then refuse
  unless the session is complete and paid, came from OUR payment link, is in
  the same mode (test/live) as the key, and is under 30 days old. Returns the
  website the buyer typed into the payment link's custom field.
* refund(payment_intent, session_id): POST /v1/refunds with an idempotency key
  derived from the session, so a double click can never refund twice.

Configuration comes from environment variables only (never from files):
  STRIPE_SECRET_KEY        a restricted key (rk_...) with Checkout Sessions: Read
                           and Refunds: Write. A full secret key works but is not
                           recommended.
  STRIPE_PAYMENT_LINK_ID   plink_... of the $29 payment link (required)
  STRIPE_WEBSITE_FIELD     custom field key on the payment link (default "website")
  STRIPE_API_BASE          only for tests (default https://api.stripe.com)
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

SESSION_RE = re.compile(r"^cs_(test|live)_[A-Za-z0-9]{10,200}$")
MAX_AGE_S = 30 * 24 * 3600


class PaymentError(Exception):
    """No usable paid session for this link. Message is safe to show."""


class StripeUnavailable(Exception):
    """Stripe could not be reached or answered with a server error."""


def _cfg():
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    base = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com").rstrip("/")
    if not base.startswith("https://") and os.environ.get("SELFSERVE_TEST_ALLOW_LOOPBACK") != "1":
        raise RuntimeError("STRIPE_API_BASE must be https")
    return key, base


def key_mode(key):
    if key.startswith(("sk_live_", "rk_live_")):
        return "live"
    if key.startswith(("sk_test_", "rk_test_")):
        return "test"
    return None


def _request(method, path, form=None, idempotency_key=None):
    key, base = _cfg()
    if not key_mode(key):
        raise StripeUnavailable("payments are not configured on this server")
    data = urllib.parse.urlencode(form).encode() if form else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Stripe-Version", "2024-06-20")
    if idempotency_key:
        req.add_header("Idempotency-Key", idempotency_key)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # no proxies
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {}
        if e.code >= 500 or e.code == 429:
            raise StripeUnavailable(f"Stripe answered {e.code}") from None
        return e.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise StripeUnavailable(f"could not reach Stripe: {e}") from None


def website_from_session(sess):
    """The website typed at checkout: the text field whose key is
    STRIPE_WEBSITE_FIELD (default "website"); failing that, the only text field
    on the link (the Stripe dashboard may generate the key from the label)."""
    want = os.environ.get("STRIPE_WEBSITE_FIELD", "website")
    texts = [f for f in (sess.get("custom_fields") or []) if f.get("type") == "text"]
    pick = [f for f in texts if f.get("key") == want] or (texts if len(texts) == 1 else [])
    for f in pick:
        v = ((f.get("text") or {}).get("value") or "").strip()
        if v:
            return v
    return None


def get_paid_session(session_id, now=None):
    if not SESSION_RE.match(session_id or ""):
        raise PaymentError("This link is missing a valid payment reference.")
    key, _ = _cfg()
    mode = key_mode(key)
    if session_id.startswith("cs_live_") != (mode == "live"):
        raise PaymentError("This payment reference is not for this site.")
    want_link = os.environ.get("STRIPE_PAYMENT_LINK_ID", "")
    if not want_link.startswith("plink_"):
        raise StripeUnavailable("payments are not configured on this server")
    code, sess = _request("GET", "/v1/checkout/sessions/" + urllib.parse.quote(session_id))
    if code == 404:
        raise PaymentError("We couldn't find a payment for this link.")
    if code != 200:
        raise StripeUnavailable(f"Stripe answered {code}")
    if sess.get("payment_link") != want_link:
        raise PaymentError("This payment reference is not for this site.")
    if sess.get("status") != "complete" or sess.get("payment_status") != "paid":
        raise PaymentError("We couldn't find a completed payment for this link.")
    if bool(sess.get("livemode")) != (mode == "live"):
        raise PaymentError("This payment reference is not for this site.")
    created = int(sess.get("created") or 0)
    if (now or time.time()) - created > MAX_AGE_S:
        raise PaymentError("This report link has expired (links work for 30 days after purchase).")
    site = website_from_session(sess)
    if not site:
        raise PaymentError("No website was entered at checkout.")
    return {"session_id": session_id, "website": site,
            "payment_intent": sess.get("payment_intent"), "created": created}


def refund(payment_intent, session_id):
    """Full refund of the payment. Returns 'succeeded', 'pending' or raises."""
    if not (isinstance(payment_intent, str) and payment_intent.startswith("pi_")):
        raise PaymentError("This payment can't be refunded automatically.")
    code, body = _request("POST", "/v1/refunds",
                          form={"payment_intent": payment_intent,
                                "metadata[reason]": "site_unreachable",
                                "metadata[checkout_session]": session_id},
                          idempotency_key="apex-checkup-refund-" + session_id)
    if code == 200:
        return body.get("status") or "pending"
    err = (body.get("error") or {})
    if err.get("code") == "charge_already_refunded":
        return "succeeded"
    raise StripeUnavailable(f"refund failed ({code} {err.get('code', '')})")
