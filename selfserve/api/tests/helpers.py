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
        self.refunds = []          # (headers, form)
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
                pre = "/v1/checkout/sessions/"
                if self.path.startswith(pre):
                    sid = urllib.parse.unquote(self.path[len(pre):])
                    if sid in fake.sessions:
                        return self._send(200, fake.sessions[sid])
                return self._send(404, {"error": {"type": "invalid_request_error", "code": "resource_missing"}})

            def do_POST(self):
                fake.auth_seen.add(self.headers.get("Authorization"))
                n = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode())
                if self.path == "/v1/refunds":
                    idem = self.headers.get("Idempotency-Key")
                    first = all(h.get("Idempotency-Key") != idem for h, _ in fake.refunds)
                    fake.refunds.append((dict(self.headers), form))
                    if not first:
                        return self._send(200, {"id": "re_1", "status": "succeeded", "replayed": True})
                    return self._send(200, {"id": "re_1", "status": "succeeded"})
                return self._send(404, {"error": {"code": "resource_missing"}})

        self.srv, self.base = serve(H)

    def add(self, sid, website, *, link="plink_test_checkup", paid=True, live=False, created=None, pi="pi_test_1"):
        import time
        self.sessions[sid] = {
            "id": sid, "object": "checkout.session", "livemode": live,
            "status": "complete" if paid else "open",
            "payment_status": "paid" if paid else "unpaid",
            "payment_link": link, "payment_intent": pi,
            "created": int(created if created is not None else time.time() - 60),
            "custom_fields": [{"key": "website", "label": {"custom": "Website", "type": "custom"},
                               "type": "text", "optional": False,
                               "text": {"value": website, "maximum_length": 255}}],
        }
