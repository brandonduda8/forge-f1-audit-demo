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
    """Just enough of Square's API for the checkup: payment links, orders, payments, refunds."""

    def __init__(self, page_size=4, refund_status="PENDING"):
        self.orders, self.payments = {}, {}
        self.links = []            # bodies of payment-link calls
        self.order_puts = []       # (order_id, body)
        self.refunds = []          # bodies of refund calls
        self.lists = []            # query dicts of payment list calls
        self.headers_seen = []     # (Authorization, Square-Version)
        self.page_size, self.refund_status = page_size, refund_status
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

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n).decode() or "{}")

            def _seen(self):
                fake.headers_seen.append((self.headers.get("Authorization"), self.headers.get("Square-Version")))

            def do_GET(self):
                self._seen()
                path, _, query = self.path.partition("?")
                q = urllib.parse.parse_qs(query)
                if path.startswith("/v2/orders/"):
                    o = fake.orders.get(urllib.parse.unquote(path[len("/v2/orders/"):]))
                    return self._send(200, {"order": o}) if o else self._missing()
                if path == "/v2/payments":
                    fake.lists.append(q)
                    rows = sorted((p for p in fake.payments.values() if p["location_id"] == (q.get("location_id") or [""])[0]),
                                  key=lambda p: p["created_at"], reverse=True)
                    start = int((q.get("cursor") or ["0"])[0])
                    page = rows[start:start + fake.page_size]
                    out = {"payments": page}
                    if start + fake.page_size < len(rows):
                        out["cursor"] = str(start + fake.page_size)
                    return self._send(200, out)
                if path.startswith("/v2/payments/"):
                    p = fake.payments.get(urllib.parse.unquote(path[len("/v2/payments/"):]))
                    return self._send(200, {"payment": p}) if p else self._missing()
                return self._missing()

            def do_PUT(self):
                self._seen()
                body = self._body()
                if self.path.startswith("/v2/orders/"):
                    oid = urllib.parse.unquote(self.path[len("/v2/orders/"):])
                    o = fake.orders.get(oid)
                    if not o:
                        return self._missing()
                    upd = body.get("order") or {}
                    if upd.get("version") != o["version"]:
                        return self._send(400, {"errors": [{"code": "VERSION_MISMATCH"}]})
                    fake.order_puts.append((oid, body))
                    o["metadata"].update(upd.get("metadata") or {})
                    o["version"] += 1
                    return self._send(200, {"order": o})
                return self._missing()

            def do_POST(self):
                self._seen()
                body = self._body()
                if self.path == "/v2/online-checkout/payment-links":
                    fake.links.append(body)
                    fake._n += 1
                    oid = "ORDLINK%020d" % fake._n
                    o = dict(body["order"])
                    o.update(id=oid, version=1, state="OPEN", metadata=dict(o.get("metadata") or {}))
                    fake.orders[oid] = o
                    return self._send(200, {"payment_link": {"id": "PL%d" % fake._n, "order_id": oid,
                                                             "url": fake.base + "/checkout/" + oid}})
                if self.path == "/v2/refunds":
                    key = body.get("idempotency_key")
                    fake.refunds.append(body)
                    if key in fake._refund_by_key:
                        return self._send(200, {"refund": fake._refund_by_key[key]})
                    p = fake.payments.get(body.get("payment_id"))
                    if not p:
                        return self._missing()
                    if p.get("refund_ids"):
                        return self._send(400, {"errors": [{"code": "REFUND_AMOUNT_INVALID"}]})
                    r = {"id": "R%d" % len(fake._refund_by_key), "status": fake.refund_status,
                         "payment_id": p["id"], "amount_money": body.get("amount_money")}
                    fake._refund_by_key[key] = r
                    p["refund_ids"] = [r["id"]]
                    p["refunded_money"] = dict(body.get("amount_money") or {})
                    return self._send(200, {"refund": r})
                return self._missing()

        self.srv, self.base = serve(H)

    def add(self, order_id, website, *, paid=True, location=LOC, marker=MARKER, created=None,
            amount=2900, currency="USD", email="buyer@example.com", delivered=False):
        """An order as the hosted checkout leaves it; paid=True adds its COMPLETED payment."""
        meta = {"apex": marker, "website": website} if marker else {"website": website}
        if delivered:
            meta["apex_report_delivered"] = "1"
        self.orders[order_id] = {"id": order_id, "location_id": location, "metadata": meta,
                                 "version": 3, "state": "OPEN"}
        if paid is not None:
            self.pay(order_id, paid=paid, created=created, amount=amount, currency=currency, email=email)

    def pay(self, order_id, *, paid=True, created=None, amount=None, currency="USD", email="buyer@example.com"):
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
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)),
            "buyer_email_address": email,
        }
        o["tenders"] = [{"id": "T" + pid, "payment_id": pid, "type": "CARD"}]
        if paid:
            o["state"] = "COMPLETED"
        return pid
