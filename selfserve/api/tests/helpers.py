"""Local test servers: fixture websites and a fake Square API (loopback only)."""
import http.server
import json
import os
import threading
import urllib.parse
from functools import partial

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture-sites")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _seen(self):
        seen = getattr(self.server, "seen", None)
        if seen is not None:
            seen.append((self.command, self.path))

    def do_HEAD(self):
        self._seen()
        if self.path in getattr(self.server, "slow_paths", ()):
            import time
            time.sleep(30)                  # a tarpit: never answers in time
            return
        return super().do_HEAD()

    def do_GET(self):
        self._seen()
        if self.path.startswith("/redirect-private"):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        if self.path.startswith("/huge"):
            body = b"x" * (6 * 1024 * 1024)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/forbidden"):
            body = b"<html><head><title>403 Forbidden</title></head><body>Forbidden</body></html>"
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/challenge"):
            body = b"<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/slow"):
            # drips one byte a second: each read is quick, the whole body never ends
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            import time
            try:
                for _ in range(600):
                    self.wfile.write(b"<")
                    self.wfile.flush()
                    time.sleep(1)
            except OSError:
                pass
            return
        return super().do_GET()


def serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    srv.seen = []              # (method, path) of every request, for tests
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def serve_site(name):
    """Serve one fixture site at its own root, like a real domain."""
    return serve(partial(_Quiet, directory=os.path.join(FIX, name)))   # name may also be an absolute path


LOC = "LTESTLOCATION01"
MARKER = "apex-checkup-v1"


class FakeSquare:
    """Enough of Square's API for Apex Forge, shaped like Square's documented responses:
    payment links (order or quick_pay + subscription_plan_id), orders (get, update, search),
    payments (get, list), refunds, locations, catalog (subscription plans), subscriptions
    (search, get, cancel) and customers. Loopback only; never talks to Square."""

    def __init__(self, page_size=4, refund_status="PENDING", token=None):
        self.orders, self.payments = {}, {}
        self.links = []            # bodies of payment-link calls
        self.order_puts = []       # (order_id, body)
        self.refunds = []          # bodies of refund calls
        self.lists = []            # query dicts of payment list calls
        self.headers_seen = []     # (Authorization, Square-Version)
        self.order_searches = []   # bodies of SearchOrders calls
        self.sub_searches = []     # bodies of SearchSubscriptions calls
        self.catalog_upserts = []  # bodies of UpsertCatalogObject calls
        self.cancels = []          # subscription ids cancelled
        self.deleted_links = []    # payment link ids deleted
        self.catalog, self.subscriptions, self.customers = {}, {}, {}
        self.link_plans = {}       # order_id -> subscription_plan_id the link was made with
        self.locations = [{"id": LOC, "name": "Apex Forge Test", "status": "ACTIVE", "currency": "USD",
                           "country": "US"},
                          {"id": "LSOMEONEELSE01", "name": "Other", "status": "ACTIVE", "currency": "USD",
                           "country": "US"}]
        self.page_size, self.refund_status, self.token = page_size, refund_status, token
        self.fail_order_put = False
        self._refund_by_key = {}
        self._n = 0
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _missing(self):
                return self._send(404, {"errors": [{"category": "INVALID_REQUEST_ERROR", "code": "NOT_FOUND"}]})

            def _bad(self, code="BAD_REQUEST", detail=""):
                return self._send(400, {"errors": [{"category": "INVALID_REQUEST_ERROR", "code": code,
                                                    "detail": detail}]})

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n).decode() or "{}")

            def _seen(self):
                fake.headers_seen.append((self.headers.get("Authorization"), self.headers.get("Square-Version")))
                if fake.token and self.headers.get("Authorization") != "Bearer " + fake.token:
                    self._send(401, {"errors": [{"category": "AUTHENTICATION_ERROR", "code": "UNAUTHORIZED"}]})
                    return False
                return True

            def do_GET(self):
                if not self._seen():
                    return
                path, _, query = self.path.partition("?")
                q = urllib.parse.parse_qs(query)
                tail = urllib.parse.unquote(path.rsplit("/", 1)[-1])
                if path == "/v2/locations":
                    return self._send(200, {"locations": fake.locations})
                if path.startswith("/v2/orders/"):
                    o = fake.orders.get(tail)
                    return self._send(200, {"order": o}) if o else self._missing()
                if path == "/v2/payments":
                    fake.lists.append(q)
                    loc = (q.get("location_id") or [""])[0]
                    begin = (q.get("begin_time") or [""])[0]
                    end = (q.get("end_time") or [""])[0]
                    rows = sorted((p for p in fake.payments.values() if p["location_id"] == loc
                                   and (not begin or _ts(p["created_at"]) >= _ts(begin))
                                   and (not end or _ts(p["created_at"]) < _ts(end))),
                                  key=lambda p: p["created_at"], reverse=True)
                    return self._send(200, _page(rows, "payments", q.get("cursor", ["0"])[0], fake.page_size))
                if path.startswith("/v2/payments/"):
                    p = fake.payments.get(tail)
                    return self._send(200, {"payment": p}) if p else self._missing()
                if path.startswith("/v2/catalog/object/"):
                    o = fake.catalog.get(tail)
                    return self._send(200, {"object": o}) if o else self._missing()
                if path.startswith("/v2/subscriptions/"):
                    s = fake.subscriptions.get(tail)
                    return self._send(200, {"subscription": s}) if s else self._missing()
                if path.startswith("/v2/customers/"):
                    c = fake.customers.get(tail)
                    return self._send(200, {"customer": c}) if c else self._missing()
                return self._missing()

            def do_DELETE(self):
                if not self._seen():
                    return
                if self.path.startswith("/v2/online-checkout/payment-links/"):
                    lid = urllib.parse.unquote(self.path.rsplit("/", 1)[-1])
                    fake.deleted_links.append(lid)
                    return self._send(200, {"id": lid})
                return self._missing()

            def do_PUT(self):
                if not self._seen():
                    return
                body = self._body()
                if self.path.startswith("/v2/orders/"):
                    oid = urllib.parse.unquote(self.path[len("/v2/orders/"):])
                    o = fake.orders.get(oid)
                    if not o:
                        return self._missing()
                    if fake.fail_order_put:
                        return self._bad("BAD_REQUEST", "order can't be updated")
                    upd = body.get("order") or {}
                    if upd.get("version") != o["version"]:
                        return self._bad("VERSION_MISMATCH")
                    fake.order_puts.append((oid, body))
                    o.setdefault("metadata", {}).update(upd.get("metadata") or {})
                    o["version"] += 1
                    return self._send(200, {"order": o})
                return self._missing()

            def do_POST(self):
                if not self._seen():
                    return
                body = self._body()
                path = self.path
                if path == "/v2/online-checkout/payment-links":
                    return self._link(body)
                if path == "/v2/orders/search":
                    fake.order_searches.append(body)
                    return self._send(200, fake._search_orders(body))
                if path == "/v2/subscriptions/search":
                    fake.sub_searches.append(body)
                    filt = ((body.get("query") or {}).get("filter") or {})
                    rows = [s for s in fake.subscriptions.values()
                            if (not filt.get("location_ids") or s["location_id"] in filt["location_ids"])
                            and (not filt.get("customer_ids") or s["customer_id"] in filt["customer_ids"])]
                    rows.sort(key=lambda s: s["created_at"])
                    return self._send(200, _page(rows, "subscriptions", body.get("cursor") or "0", fake.page_size))
                if path.startswith("/v2/subscriptions/") and path.endswith("/cancel"):
                    sid = urllib.parse.unquote(path.split("/")[3])
                    s = fake.subscriptions.get(sid)
                    if not s:
                        return self._missing()
                    if s["status"] == "CANCELED" or s.get("canceled_date"):
                        return self._bad("BAD_REQUEST", "subscription already canceled")
                    fake.cancels.append(sid)
                    # Square: cancels at the end of the billing period; status stays ACTIVE until then
                    s["canceled_date"] = s.get("charged_through_date") or s["start_date"]
                    s["version"] = s.get("version", 1) + 1
                    return self._send(200, {"subscription": s, "actions": [
                        {"id": "act-" + sid, "type": "CANCEL", "effective_date": s["canceled_date"]}]})
                if path == "/v2/catalog/object":
                    return self._catalog(body)
                if path == "/v2/refunds":
                    return self._refund(body)
                return self._missing()

            def _link(self, body):
                fake.links.append(body)
                opts = body.get("checkout_options") or {}
                if bool(body.get("order")) == bool(body.get("quick_pay")):
                    return self._bad("INVALID_REQUEST", "exactly one of order or quick_pay")
                plan_id = opts.get("subscription_plan_id")
                if plan_id and body.get("order"):
                    # Square staff: a subscription link with an order isn't available
                    return self._bad("INVALID_REQUEST", "subscription_plan_id needs quick_pay")
                if plan_id:
                    v = fake.catalog.get(plan_id)
                    if not v or v["type"] != "SUBSCRIPTION_PLAN_VARIATION":
                        return self._bad("NOT_FOUND", "subscription plan variation")
                fake._n += 1
                oid = "ORDLINK%020d" % fake._n
                if body.get("order"):
                    o = dict(body["order"])
                    o.update(metadata=dict(o.get("metadata") or {}))
                else:
                    qp = body["quick_pay"]
                    o = {"location_id": qp["location_id"],
                         "line_items": [{"name": qp["name"], "quantity": "1",
                                         "base_price_money": qp["price_money"]}]}
                o.update(id=oid, version=1, state="DRAFT",
                         created_at=_iso(__import__("time").time()))
                fake.orders[oid] = o
                if plan_id:
                    fake.link_plans[oid] = plan_id
                return self._send(200, {"payment_link": {"id": "PLINK%010d" % fake._n, "version": 1,
                                                         "order_id": oid, "url": fake.base + "/checkout/" + oid,
                                                         "checkout_options": opts},
                                        "related_resources": {"orders": [dict(o)]}})

            def _catalog(self, body):
                fake.catalog_upserts.append(body)
                obj = dict(body.get("object") or {})
                if not body.get("idempotency_key") or not str(obj.get("id", "")).startswith("#"):
                    return self._bad("MISSING_REQUIRED_PARAMETER", "idempotency_key / temporary id")
                if obj.get("type") == "SUBSCRIPTION_PLAN_VARIATION":
                    d = obj.get("subscription_plan_variation_data") or {}
                    if (fake.catalog.get(d.get("subscription_plan_id")) or {}).get("type") != "SUBSCRIPTION_PLAN":
                        return self._bad("INVALID_VALUE", "subscription_plan_id")
                    for ph in d.get("phases") or []:
                        pr = ph.get("pricing") or {}
                        if "recurring_price_money" in ph:
                            return self._bad("INVALID_VALUE", "phase pricing must be set on the pricing field")
                        if pr.get("type") == "STATIC" and not (pr.get("price_money") or {}).get("amount"):
                            return self._bad("MISSING_REQUIRED_PARAMETER", "pricing.price_money")
                elif obj.get("type") != "SUBSCRIPTION_PLAN":
                    return self._bad("INVALID_VALUE", "type")
                fake._n += 1
                real = "CAT%021d" % fake._n
                client = obj["id"]
                obj.update(id=real, version=1, is_deleted=False, present_at_all_locations=True)
                fake.catalog[real] = obj
                return self._send(200, {"catalog_object": obj,
                                        "id_mappings": [{"client_object_id": client, "object_id": real}]})

            def _refund(self, body):
                key = body.get("idempotency_key")
                fake.refunds.append(body)
                if key in fake._refund_by_key:
                    return self._send(200, {"refund": fake._refund_by_key[key]})
                p = fake.payments.get(body.get("payment_id"))
                if not p:
                    return self._missing()
                if p.get("refund_ids"):
                    return self._bad("REFUND_AMOUNT_INVALID")
                r = {"id": "R%d" % len(fake._refund_by_key), "status": fake.refund_status,
                     "payment_id": p["id"], "amount_money": body.get("amount_money")}
                fake._refund_by_key[key] = r
                p["refund_ids"] = [r["id"]]
                p["refunded_money"] = dict(body.get("amount_money") or {})
                return self._send(200, {"refund": r})

        self.srv, self.base = serve(H)

    def _search_orders(self, body):
        locs = body.get("location_ids") or []
        if not locs or len(locs) > 10:
            return {"errors": [{"code": "BAD_REQUEST"}]}
        filt = ((body.get("query") or {}).get("filter") or {})
        states = (filt.get("state_filter") or {}).get("states")
        custs = (filt.get("customer_filter") or {}).get("customer_ids")
        created = (filt.get("date_time_filter") or {}).get("created_at") or {}
        rows = [o for o in self.orders.values() if o["location_id"] in locs
                and (not states or o.get("state") in states)
                and (not custs or o.get("customer_id") in custs)
                and (not created.get("start_at") or _ts(o["created_at"]) >= _ts(created["start_at"]))
                and (not created.get("end_at") or _ts(o["created_at"]) < _ts(created["end_at"]))]
        desc = ((body.get("query") or {}).get("sort") or {}).get("sort_order", "DESC") == "DESC"
        rows.sort(key=lambda o: o["created_at"], reverse=desc)
        return _page(rows, "orders", body.get("cursor") or "0", self.page_size)

    def add(self, order_id, website, *, paid=True, location=LOC, marker=MARKER, created=None,
            amount=2900, currency="USD", email="buyer@example.com", delivered=False, plan=None,
            customer_id=None, refund_cents=0):
        """An order as the hosted checkout leaves it; paid=True adds its COMPLETED payment."""
        import time
        meta = {"apex": marker, "website": website} if marker else {"website": website}
        if plan:
            meta["plan"] = plan
        if delivered:
            meta["apex_report_delivered"] = "1"
        ts = time.time() - 60 if created is None else created
        self.orders[order_id] = {"id": order_id, "location_id": location, "metadata": meta,
                                 "version": 3, "state": "OPEN", "created_at": _iso(ts - 30)}
        if customer_id:
            self.orders[order_id]["customer_id"] = customer_id
        if paid is not None:
            self.pay(order_id, paid=paid, created=created, amount=amount, currency=currency, email=email,
                     customer_id=customer_id, refund_cents=refund_cents)

    def pay(self, order_id, *, paid=True, created=None, amount=None, currency="USD", email="buyer@example.com",
            customer_id=None, refund_cents=0):
        import time
        o = self.orders[order_id]
        pid = "PAY" + order_id[-20:]
        ts = time.time() - 60 if created is None else created
        if amount is None:
            amount = (o.get("line_items") or [{}])[0].get("base_price_money", {}).get("amount", 2900)
        self.payments[pid] = {
            "id": pid, "order_id": order_id, "location_id": o["location_id"],
            "status": "COMPLETED" if paid else "APPROVED",
            "amount_money": {"amount": amount, "currency": currency},
            "created_at": _iso(ts),
            "buyer_email_address": email,
        }
        if customer_id:
            self.payments[pid]["customer_id"] = customer_id
            o["customer_id"] = customer_id
        if refund_cents:
            self.payments[pid]["refunded_money"] = {"amount": refund_cents, "currency": currency}
            self.payments[pid]["refund_ids"] = ["RF" + pid]
        o["tenders"] = [{"id": "T" + pid, "payment_id": pid, "type": "CARD"}]
        if paid:
            o["state"] = "COMPLETED"
        return pid

    def add_customer(self, customer_id, email=None):
        self.customers[customer_id] = {"id": customer_id, **({"email_address": email} if email else {})}
        return customer_id

    def subscribe(self, sub_id, customer_id, *, variation, location=LOC, status="ACTIVE", created=None,
                  start_date="2026-10-01", charged_through_date="2026-12-01", canceled_date=None,
                  price_override=None):
        """A subscription as Square's payment-link checkout leaves it (it names no order)."""
        import time
        s = {"id": sub_id, "location_id": location, "plan_variation_id": variation, "customer_id": customer_id,
             "start_date": start_date, "charged_through_date": charged_through_date, "status": status,
             "created_at": _iso(time.time() if created is None else created), "timezone": "UTC",
             "version": 1, "source": {"name": "Square Online Checkout"}}
        if canceled_date:
            s["canceled_date"] = canceled_date
        if price_override is not None:
            s["price_override_money"] = {"amount": price_override, "currency": "USD"}
        self.subscriptions[sub_id] = s
        return s

    def add_plan_variation(self, cents=1900, cadence="MONTHLY"):
        """A SUBSCRIPTION_PLAN + one STATIC variation straight in the catalog; returns the variation id."""
        self._n += 1
        plan_id, var_id = "CATPLAN%017d" % self._n, "CATVAR%018d" % self._n
        self.catalog[plan_id] = {"id": plan_id, "type": "SUBSCRIPTION_PLAN", "is_deleted": False,
                                 "subscription_plan_data": {"name": "Apex Forge Monthly Monitoring"}}
        self.catalog[var_id] = {"id": var_id, "type": "SUBSCRIPTION_PLAN_VARIATION", "is_deleted": False,
                                "subscription_plan_variation_data": {
                                    "name": "Monthly", "subscription_plan_id": plan_id,
                                    "phases": [{"uid": "PH1", "cadence": cadence, "ordinal": 0, "pricing": {
                                        "type": "STATIC", "price_money": {"amount": cents, "currency": "USD"}}}]}}
        return var_id


def _iso(ts):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def _ts(s):
    from datetime import datetime
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def _page(rows, key, cursor, size):
    start = int(cursor or 0)
    out = {key: rows[start:start + size]}
    if start + size < len(rows):
        out["cursor"] = str(start + size)
    return out
