"""netguard.py - outbound-request safety for the self-serve checkup.

The checkup fetches a website a stranger typed in. Without a guard that is a
server-side request forgery hole: someone could point it at 127.0.0.1, the
cloud metadata address (169.254.169.254) or a private network. This module
closes that hole at the one place every connection passes through:

* install() replaces urllib3's create_connection and socket.create_connection
  with a version that resolves the host, refuses any address that is not a
  public (global) unicast address, refuses any port other than 80 and 443, and
  then connects to the exact address it checked. Checking the address that is
  actually dialled (not an earlier lookup) also defeats DNS rebinding. Every
  redirect hop goes through the same function, so a redirect to a private
  address is refused too.
* GuardedRequests is a drop-in for the `requests` module functions that
  f1_measure.py uses (get, head). It ignores proxy environment variables,
  caps redirects, caps the body size, caps the number of requests and enforces
  an overall deadline. When a cap is hit it raises, and it records why, so the
  caller can mark the affected checks "not measured" instead of reporting a
  false failure.

The guard is meant to be installed inside a short-lived worker process (see
runner.py), so patching module globals affects nothing else.

Test hook: SELFSERVE_TEST_ALLOW_LOOPBACK=1 also allows 127.0.0.1/::1 and any
port, so the end-to-end tests can serve fixture sites locally. server.py refuses
to start in that mode with production Square.
"""
import ipaddress
import os
import socket
import time

import requests
import urllib3.util.connection as u3conn

ALLOWED_PORTS = {80, 443}
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5


class Blocked(Exception):
    """The destination is not a public web address on port 80/443."""


class BudgetExceeded(Exception):
    """The run used up its request count or its time."""


class TooLarge(Exception):
    """A response body was larger than MAX_BODY_BYTES."""


def _test_loopback_allowed():
    return os.environ.get("SELFSERVE_TEST_ALLOW_LOOPBACK") == "1"


def address_allowed(ip_str):
    ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if _test_loopback_allowed() and ip.is_loopback:
        return True
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def port_allowed(port):
    return _test_loopback_allowed() or int(port) in ALLOWED_PORTS


def resolve_checked(host, port):
    """getaddrinfo, then refuse if ANY returned address is not allowed."""
    if not port_allowed(port):
        raise Blocked(f"BLOCKED port {port} (only 80 and 443 are checked)")
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise OSError(f"could not resolve {host}: {e}") from None
    if not infos:
        raise OSError(f"could not resolve {host}")
    for fam, _t, _p, _c, sa in infos:
        if not address_allowed(sa[0]):
            raise Blocked(f"BLOCKED {host} resolves to a non-public address")
    return infos


def guarded_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                              source_address=None, socket_options=None, **_kw):
    host, port = address
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    infos = resolve_checked(host, port)
    err = None
    for fam, stype, proto, _c, sa in infos:
        sock = None
        try:
            sock = socket.socket(fam, stype, proto)
            for opt in socket_options or ():
                sock.setsockopt(*opt)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)          # the exact address that was checked
            return sock
        except OSError as e:
            err = e
            if sock is not None:
                sock.close()
    raise err or OSError("connection failed")


_installed = False
_ORIG = {"u3": u3conn.create_connection, "sock": socket.create_connection}


def install():
    """Route every outbound TCP connection in this process through the guard."""
    global _installed
    if _installed:
        return
    u3conn.create_connection = guarded_create_connection
    socket.create_connection = guarded_create_connection
    _installed = True


def uninstall():
    """Tests only: put the original connection functions back."""
    global _installed
    u3conn.create_connection = _ORIG["u3"]
    socket.create_connection = _ORIG["sock"]
    _installed = False


class GuardedRequests:
    """Stands in for the `requests` module inside f1_measure (get/head only)."""

    exceptions = requests.exceptions  # in case callers reference it

    def __init__(self, max_requests, deadline_s, max_body=MAX_BODY_BYTES):
        self.session = requests.Session()
        self.session.trust_env = False          # never use proxy env vars
        self.session.max_redirects = MAX_REDIRECTS
        self.max_requests = max_requests
        self.deadline = time.monotonic() + deadline_s
        self.max_body = max_body
        self.count = 0
        self.events = []                        # what stopped a request, and why

    def _admit(self, timeout):
        if self.count >= self.max_requests:
            self.events.append("budget:requests")
            raise BudgetExceeded("BUDGET request limit reached")
        left = self.deadline - time.monotonic()
        if left <= 0.5:
            self.events.append("budget:time")
            raise BudgetExceeded("BUDGET time limit reached")
        self.count += 1
        return max(0.5, min(float(timeout or 15), left))

    def _call(self, method, url, headers=None, timeout=15, allow_redirects=True, **_kw):
        t = self._admit(timeout)
        try:
            r = self.session.request(method, url, headers=headers, timeout=t,
                                     allow_redirects=allow_redirects, stream=True)
        except Blocked:
            self.events.append("blocked")
            raise
        except requests.exceptions.ConnectionError as e:
            if "BLOCKED" in str(e):
                self.events.append("blocked")
                raise Blocked(str(e)) from None
            raise
        if method == "HEAD":
            r.close()
            r._content = b""
            r._content_consumed = True
            return r
        buf = bytearray()
        try:
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > self.max_body:
                    self.events.append("too_large")
                    raise TooLarge(f"response larger than {self.max_body} bytes")
                if time.monotonic() > self.deadline:
                    self.events.append("budget:time")
                    raise BudgetExceeded("BUDGET time limit reached while downloading")
        finally:
            r.close()
        r._content = bytes(buf)
        r._content_consumed = True
        return r

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def head(self, url, **kw):
        return self._call("HEAD", url, **kw)
