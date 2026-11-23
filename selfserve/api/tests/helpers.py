"""Local test servers: fixture websites and a fake Stripe API (loopback only)."""
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

    def do_GET(self):
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
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def serve_site(name):
    """Serve one fixture site at its own root, like a real domain."""
    return serve(partial(_Quiet, directory=os.path.join(FIX, name)))


class FakeStripe:
    """Just enough of api.stripe.com for the checkup: sessions + refunds."""

    def __init__(self):
        self.sessions = {}
        self.pis = {}              # payment_intent id -> {"metadata", "refunded"}
        self.refunds = []          # (headers, form)
        self.pi_updates = []       # (pi, form)
        self.lists = []            # query dicts of list calls
        self.auth_seen = set()
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

            def do_GET(self):
                fake.auth_seen.add(self.headers.get("Authorization"))
                path, _, query = self.path.partition("?")
                q = urllib.parse.parse_qs(query)
                pre = "/v1/checkout/sessions/"
                if path == "/v1/checkout/sessions":
                    fake.lists.append(q)
                    email = (q.get("customer_details[email]") or [""])[0]
                    link = (q.get("payment_link") or [""])[0]
                    data = [fake.view(s, False) for s in fake.sessions.values()
                            if s.get("customer_details", {}).get("email") == email and s["payment_link"] == link]
                    return self._send(200, {"object": "list", "data": data, "has_more": False})
                if path.startswith(pre):
                    sid = urllib.parse.unquote(path[len(pre):])
                    if sid in fake.sessions:
                        expand = "payment_intent.latest_charge" in q.get("expand[]", [])
                        return self._send(200, fake.view(fake.sessions[sid], expand))
                return self._send(404, {"error": {"type": "invalid_request_error", "code": "resource_missing"}})

            def do_POST(self):
                fake.auth_seen.add(self.headers.get("Authorization"))
                n = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode())
                if self.path.startswith("/v1/payment_intents/"):
                    pi = urllib.parse.unquote(self.path.rsplit("/", 1)[1])
                    if pi not in fake.pis:
                        return self._send(404, {"error": {"code": "resource_missing"}})
                    fake.pi_updates.append((pi, form))
                    for k, v in form.items():
                        if k.startswith("metadata["):
                            fake.pis[pi]["metadata"][k[9:-1]] = v[0]
                    return self._send(200, {"id": pi, "metadata": fake.pis[pi]["metadata"]})
                if self.path == "/v1/refunds":
                    idem = self.headers.get("Idempotency-Key")
                    first = all(h.get("Idempotency-Key") != idem for h, _ in fake.refunds)
                    fake.refunds.append((dict(self.headers), form))
                    pi = (form.get("payment_intent") or [""])[0]
                    if pi in fake.pis:
                        fake.pis[pi]["refunded"] = True
                    if not first:
                        return self._send(200, {"id": "re_1", "status": "succeeded", "replayed": True})
                    return self._send(200, {"id": "re_1", "status": "succeeded"})
                return self._send(404, {"error": {"code": "resource_missing"}})

        self.srv, self.base = serve(H)

    def view(self, sess, expand):
        out = dict(sess)
        pi = sess["payment_intent"]
        if expand and pi in self.pis:
            st = self.pis[pi]
            out["payment_intent"] = {"id": pi, "object": "payment_intent", "metadata": dict(st["metadata"]),
                                     "latest_charge": {"id": "ch_" + pi[3:], "refunded": st["refunded"],
                                                       "amount_refunded": 2900 if st["refunded"] else 0}}
        return out

    def add(self, sid, website, *, link="plink_test_checkup", paid=True, live=False, created=None,
            pi="pi_test_1", email="buyer@example.com"):
        import time
        self.pis.setdefault(pi, {"metadata": {}, "refunded": False})
        self.sessions[sid] = {
            "id": sid, "object": "checkout.session", "livemode": live,
            "status": "complete" if paid else "open",
            "payment_status": "paid" if paid else "unpaid",
            "payment_link": link, "payment_intent": pi,
            "customer_details": {"email": email},
            "created": int(created if created is not None else time.time() - 60),
            "custom_fields": [{"key": "website", "label": {"custom": "Website", "type": "custom"},
                               "type": "text", "optional": False,
                               "text": {"value": website, "maximum_length": 255}}],
        }
