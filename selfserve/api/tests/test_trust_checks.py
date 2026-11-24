"""Accessibility (7.x) and security / email-trust (8.x) checks."""
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.path.dirname(HERE)
REPO = os.path.abspath(os.path.join(API, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, API)
sys.path.insert(0, os.path.join(REPO, "audit-demo"))
sys.path.insert(0, os.path.join(REPO, "f1-audit-product", "tools"))

import audit_pipeline as pipe  # noqa: E402
import dnstxt  # noqa: E402
import f1_trust  # noqa: E402
from helpers import FIX, serve_site  # noqa: E402


def soup(html):
    return BeautifulSoup(html, "lxml")


def score_of(cid, value):
    ev = {"checks": {cid: {"value": value, "method": "m", "ts": "t", "note": ""}}}
    return pipe.score(ev)["check_scores"][cid]


# ---------------------------------------------------------------- accessibility

def test_alt_text_counts_missing_attribute_but_not_decorative_empty_alt():
    v = f1_trust.alt_text(soup('<img src=a><img src=b alt=""><img src=c alt="Roof">'))
    assert v["n_images"] == 3 and v["missing_alt"] == 1
    assert score_of("7.1", v)["score"] == 0          # 1 in 3 is over the 20% line
    assert score_of("7.1", {"n_images": 10, "missing_alt": 2})["score"] == 0.5
    assert score_of("7.1", {"n_images": 4, "missing_alt": 0})["score"] == 1


def test_field_labels_accepts_every_real_labelling_method_and_not_placeholder():
    html = """<form>
      <label for=a>A</label><input id=a>
      <label>B <input name=b></label>
      <input name=c aria-label="C">
      <span id=dl>D</span><input name=d aria-labelledby=dl>
      <select name=e title="E"></select>
      <input name=f placeholder="Phone">
      <textarea name=g></textarea>
      <input type=hidden name=h><input type=submit><button>Go</button>
    </form>"""
    v = f1_trust.field_labels(soup(html))
    assert v["n_fields"] == 7 and v["unlabeled"] == 2
    assert set(v["examples"]) == {"f", "g"}
    assert score_of("7.2", v)["score"] == 0.5


def test_heading_order_skips_and_no_headings():
    v = f1_trust.heading_order(soup("<h1>a</h1><h3>b</h3><h2>c</h2><h4>d</h4>"))
    assert v["n_skips"] == 2 and v["skips"][0] == "h1 to h3"
    assert score_of("7.3", v)["score"] == 0.5
    assert score_of("7.3", f1_trust.heading_order(soup("<p>x</p>")))["score"] == 0
    assert score_of("7.3", f1_trust.heading_order(soup("<h1>a</h1><h2>b</h2><h2>c</h2><h3>d</h3><h2>e</h2>")))["score"] == 1


@pytest.mark.parametrize("html,ok", [('<html lang="en-US">', True), ("<html lang='fr'>", True),
                                     ("<html>", False), ('<html lang="">', False), ('<html lang="english language">', False)])
def test_page_language(html, ok):
    v = f1_trust.page_lang(soup(html + "<body>x</body></html>"))
    assert v["valid"] is ok
    assert score_of("7.4", v)["score"] == (1 if ok else 0)


def test_contrast_matches_wcag_reference_values():
    assert round(f1_trust.contrast_ratio((0, 0, 0), (255, 255, 255)), 1) == 21.0
    assert round(f1_trust.contrast_ratio((119, 119, 119), (255, 255, 255)), 2) == 4.48   # #777 on white
    assert f1_trust.parse_color("#FFF") == (255, 255, 255)
    assert f1_trust.parse_color("rgb(10, 20, 30)") == (10, 20, 30)
    assert f1_trust.parse_color("rgba(0,0,0,0.5)") is None            # see-through: can't know
    assert f1_trust.parse_color("var(--x)") is None


def test_contrast_only_scores_inline_pairs_and_is_otherwise_not_measured():
    v = f1_trust.contrast(soup('<p style="color:#bbb;background-color:#fff">pale</p>'
                               '<p style="color:#777;background:#fff">grey</p>'
                               '<p style="color:#000;background:#fff url(x.png) no-repeat">img bg</p>'
                               '<p style="color:#000">no bg</p>'))
    assert v["n_pairs"] == 2 and v["below_3"] == 1 and v["below_4_5"] == 2
    assert score_of("7.5", v)["score"] == 0
    assert score_of("7.5", {"n_pairs": 1, "below_3": 0, "below_4_5": 1, "examples": [{"text": "x", "ratio": 4.48}]})["score"] == 0.5
    none = f1_trust.contrast(soup('<p class="pale">styled by a stylesheet</p>'))
    assert none["n_pairs"] == 0      # add_checks records this as not measured


# ---------------------------------------------------------------- security

class R:
    def __init__(self, headers, status=200):
        self.headers, self.status_code = headers, status


def test_security_headers():
    full = R({"Strict-Transport-Security": "max-age=31536000; includeSubDomains",
              "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
              "X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin"})
    h = f1_trust.hsts(full, "https://x.example/")
    assert h["present"] and h["max_age"] == 31536000 and score_of("8.1", h)["score"] == 1
    assert score_of("8.1", f1_trust.hsts(R({"Strict-Transport-Security": "max-age=300"}), "https://x/"))["score"] == 0.5
    o = f1_trust.other_headers(full)
    assert o["missing"] == [] and score_of("8.2", o)["score"] == 1     # frame-ancestors stands in for X-Frame-Options
    o = f1_trust.other_headers(R({"X-Content-Type-Options": "nosniff"}))
    assert len(o["missing"]) == 3 and score_of("8.2", o)["score"] == 0
    assert "missing: content-security-policy" in score_of("8.2", o)["why"]


def test_certificate_expiry_thresholds_and_severity():
    def cert(days):
        return {"valid": True, "expires": (datetime.now(timezone.utc) + timedelta(days=days, hours=1)).isoformat()}
    assert score_of("8.3", f1_trust.cert_expiry(cert(90)))["score"] == 1
    assert score_of("8.3", f1_trust.cert_expiry(cert(20)))["score"] == 0.5
    assert score_of("8.3", f1_trust.cert_expiry(cert(5)))["score"] == 0
    assert pipe.fix_for("8.3", 0)[0] == "CRITICAL" and pipe.fix_for("8.3", 0.5)[0] == "MAJOR"
    assert f1_trust.cert_expiry({"valid": False}) is None


class FakeHead:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def head(self, url, **kw):
        self.calls.append((url, kw.get("allow_redirects")))
        a = self.answers[url.split("x.example/", 1)[1]]
        if isinstance(a, Exception):
            raise a
        return R({"Content-Type": a[1]}, a[0])


def test_exposed_files_uses_one_head_each_and_ignores_soft_404_pages():
    fake = FakeHead({".env": (200, "application/octet-stream"), ".git/HEAD": (200, "text/html; charset=utf-8")})
    v = f1_trust.exposed_files(fake, "https://x.example/some/page")
    assert [c[0] for c in fake.calls] == ["https://x.example/.env", "https://x.example/.git/HEAD"]
    assert all(c[1] is False for c in fake.calls)                     # redirects are not followed
    assert v["exposed"] == [{"path": "/.env", "status": 200, "type": "application/octet-stream"}]
    s = score_of("8.4", v)
    assert s["score"] == 0 and "contents not downloaded" in s["why"]
    assert pipe.fix_for("8.4", 0)[0] == "CRITICAL"
    fake = FakeHead({".env": (404, "text/html"), ".git/HEAD": (301, "text/html")})
    assert score_of("8.4", f1_trust.exposed_files(fake, "https://x.example/"))["score"] == 1


def test_exposed_files_unknown_when_server_refuses_or_request_fails():
    fake = FakeHead({".env": (405, ""), ".git/HEAD": OSError("reset")})
    v = f1_trust.exposed_files(fake, "https://x.example/")
    assert v["checked"] == [] and v["exposed"] == []


# ---------------------------------------------------------------- email trust

def fake_dns(table):
    seen = []

    def lookup(name):
        seen.append(name)
        if name in table:
            v = table[name]
            return v if isinstance(v, tuple) else ("ok", v)
        return ("nxdomain", [])
    return lookup, seen


def far():
    import time
    return time.monotonic() + 30


def test_mail_domain():
    assert f1_trust.mail_domain("www.bob-roofing.example") == "bob-roofing.example"
    assert f1_trust.mail_domain("127.0.0.1") is None
    assert f1_trust.mail_domain("[::1]") is None


def test_spf_and_dmarc_scoring(monkeypatch):
    lookup, _ = fake_dns({"a.example": ["google-site-verification=x", "v=spf1 include:_spf.google.com ~all"],
                          "_dmarc.a.example": ["v=DMARC1; p=reject; rua=mailto:d@a.example"],
                          "b.example": ["v=spf1 +all"], "_dmarc.b.example": ["v=DMARC1; p=none"],
                          "c.example": ["v=spf1 -all", "v=spf1 ~all"]})
    monkeypatch.setattr(f1_trust, "TXT_LOOKUP", lookup)
    a = f1_trust.spf("a.example", far())
    assert a["count"] == 1 and a["all"] == "~all" and score_of("8.5", a)["score"] == 1
    assert score_of("8.6", f1_trust.dmarc("a.example", far()))["score"] == 1
    b = f1_trust.spf("b.example", far())
    assert score_of("8.5", b)["score"] == 0.5 and "lets anyone send" in score_of("8.5", b)["why"]
    bd = f1_trust.dmarc("b.example", far())
    assert bd["policy"] == "none" and score_of("8.6", bd)["score"] == 0.5
    assert pipe.fix_for("8.6", 0.5)[0] == "MINOR" and pipe.fix_for("8.6", 0)[0] == "MAJOR"
    assert score_of("8.5", f1_trust.spf("c.example", far()))["score"] == 0.5      # two SPF records
    d = f1_trust.spf("d.example", far())
    assert d["count"] == 0 and score_of("8.5", d)["score"] == 0
    assert score_of("8.6", f1_trust.dmarc("d.example", far()))["score"] == 0


def test_subdomain_falls_back_to_parent_domain(monkeypatch):
    lookup, seen = fake_dns({"example.com": ["v=spf1 mx -all"], "_dmarc.example.com": ["v=DMARC1;p=quarantine"]})
    monkeypatch.setattr(f1_trust, "TXT_LOOKUP", lookup)
    assert f1_trust.spf("shop.example.com", far())["domain"] == "example.com"
    assert f1_trust.dmarc("shop.example.com", far())["policy"] == "quarantine"
    assert seen[0] == "shop.example.com"


def test_dns_failure_is_not_measured_never_a_fail(monkeypatch):
    lookup, _ = fake_dns({"a.example": ("error", [])})
    monkeypatch.setattr(f1_trust, "TXT_LOOKUP", lookup)
    assert f1_trust.spf("a.example", far()) is None
    import time
    assert f1_trust.dmarc("z.example", time.monotonic() - 1) is None      # out of time: not measured


def test_dkim_passes_when_found_and_is_never_a_fail(monkeypatch):
    lookup, seen = fake_dns({"selector1._domainkey.a.example": ["v=DKIM1; k=rsa; p=MIGfMA0G"]})
    monkeypatch.setattr(f1_trust, "TXT_LOOKUP", lookup)
    assert f1_trust.dkim("a.example", far()) == {"selector": "selector1", "found": True}
    assert score_of("8.7", {"selector": "selector1", "found": True})["score"] == 1
    lookup, seen = fake_dns({})
    monkeypatch.setattr(f1_trust, "TXT_LOOKUP", lookup)
    assert f1_trust.dkim("a.example", far())["found"] is False
    assert len(seen) == len(f1_trust.DKIM_SELECTORS)


def _ev_from(html, url, light, lookup, monkeypatch, headers=None, requests_mod=None, cert=None):
    monkeypatch.setattr(f1_trust, "TXT_LOOKUP", lookup)
    C = {"3.7": {"value": {"https": url.startswith("https"), "cert": cert}}}
    skipped = {"8.3", "8.4", "8.7"}

    def put(cid, value, method, note=""):
        if light and cid in skipped:
            C[cid] = {"value": None, "status": "NOT_MEASURED", "method": method, "ts": "t", "note": "light"}
            return
        C[cid] = {"value": value, "method": method, "ts": "t", "note": note}
    f1_trust.add_checks(put, C, R(headers or {}), soup(html), url, url, light, requests_mod)
    return {"checks": C}


def test_light_mode_makes_no_requests_and_only_spf_dmarc_lookups(monkeypatch):
    lookup, seen = fake_dns({"_dmarc.a.example": ["v=DMARC1; p=none"]})

    class NoRequests:
        def head(self, *a, **k):
            raise AssertionError("light mode must not request anything")
    ev = _ev_from("<html><body><h1>x</h1></body></html>", "https://www.a.example/", True, lookup, monkeypatch,
                  requests_mod=NoRequests())
    assert seen == ["a.example", "_dmarc.a.example"]
    sc = pipe.score(ev)
    cs = sc["check_scores"]
    assert cs["8.5"]["score"] == 0 and cs["8.6"]["score"] == 0.5
    for cid in ("8.3", "8.4", "8.7", "7.1", "7.2", "7.5"):
        assert cs[cid]["score"] is None, cid
    assert sc["sections"]["Security and email trust"]["score"] is not None


def test_sections_are_scored_but_not_part_of_the_grade(monkeypatch):
    lookup, _ = fake_dns({})
    bad = _ev_from('<html><body><img src=a><input name=q><h1>a</h1><h4>b</h4></body></html>',
                   "https://a.example/", True, lookup, monkeypatch)
    good = _ev_from('<html lang="en"><body><img src=a alt=x><label>Q <input name=q></label><h1>a</h1><h2>b</h2></body></html>',
                    "https://a.example/", True, lookup, monkeypatch,
                    headers={"Strict-Transport-Security": "max-age=31536000", "Content-Security-Policy": "default-src 'self'",
                             "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})
    # the same graded evidence on both sides
    for ev in (bad, good):
        ev["checks"]["2.1"] = {"value": True, "method": "m", "ts": "t"}
    sb, sg = pipe.score(bad), pipe.score(good)
    assert sb["total"] == sg["total"]
    assert sb["sections"]["Accessibility"]["score"] < sg["sections"]["Accessibility"]["score"] == 100


# ---------------------------------------------------------------- real runs

def run_runner(mode, url):
    env = {"PATH": os.environ["PATH"], "SELFSERVE_TEST_ALLOW_LOOPBACK": "1"}
    p = subprocess.run([sys.executable, "-I", os.path.join(API, "runner.py"), mode, url],
                       capture_output=True, text=True, timeout=120, env=env)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.fixture
def trust_site(tmp_path):
    site = tmp_path / "site"
    shutil.copytree(os.path.join(FIX, "trust-issues"), site)
    (site / ".env").write_text("EXAMPLE_SETTING=not-a-real-value\n")      # made here so no .env sits in the repo
    (site / ".git").mkdir()
    (site / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    srv, base = serve_site(str(site))
    yield srv, base
    srv.shutdown()


def test_full_run_reports_both_sections_and_never_downloads_private_files(trust_site):
    srv, base = trust_site
    res = run_runner("full", base + "/")
    assert res["ok"] and res["requests_made"] <= 90
    assert ("HEAD", "/.env") in srv.seen and ("HEAD", "/.git/HEAD") in srv.seen
    assert not [s for s in srv.seen if s[0] == "GET" and s[1] in ("/.env", "/.git/HEAD")]
    rep = res["report"]
    assert not [f for f in rep["findings"] if f["id"] in pipe.SECTION_IDS]     # graded list stays graded-only
    secs = {s["name"]: s for s in rep["sections"]}
    a11y = {f["id"] for f in secs["Accessibility"]["findings"]}
    assert a11y == {"7.1", "7.2", "7.3", "7.4", "7.5"}
    sec = secs["Security and email trust"]["findings"]
    assert sec[0]["id"] == "8.4" and sec[0]["severity"] == "CRITICAL"
    assert "EXAMPLE_SETTING" not in json.dumps(res)                          # contents never kept
    assert "8.2" in {f["id"] for f in sec}
    nm = {n["id"]: n["reason"] for n in rep["not_measured"]}
    for cid in ("8.5", "8.6", "8.7"):
        assert "not a domain name" in nm[cid]
    assert "8.1" in nm and "8.3" in nm                                         # http fixture: no HTTPS
    md = res["markdown"]
    assert "## ACCESSIBILITY:" in md and "## SECURITY AND EMAIL TRUST:" in md
    assert "Private files may be public" in md and "not in grade" in md


def test_free_check_shows_the_top_finding_of_each_section(trust_site):
    srv, base = trust_site
    res = run_runner("light", base + "/")
    assert res["ok"] and res["requests_made"] == 3
    assert not [s for s in srv.seen if s[1] in ("/.env", "/.git/HEAD")]       # free check never probes files
    secs = {s["name"]: s for s in res["sections"]}
    assert secs["Accessibility"]["top"]["severity"] == "MAJOR" and secs["Accessibility"]["failing"] == 5
    assert secs["Security and email trust"]["top"]["title"] == "Missing security headers"
    assert all(t["title"] not in {"Missing security headers", "Images missing alt text"} for t in res["top"])


def test_old_fixture_full_run_still_passes_with_new_rows():
    srv, base = serve_site("example-roofing")
    res = run_runner("full", base + "/")
    srv.shutdown()
    assert res["ok"] and res["requests_made"] <= 90
    ids = [f["id"] for f in res["report"]["findings"]] + [f["id"] for s in res["report"]["sections"] for f in s["findings"]]
    ids += [p["id"] for p in res["report"]["passed"]] + [n["id"] for n in res["report"]["not_measured"]]
    assert len(ids) == len(set(ids))
    assert {"7.4", "8.2", "8.4"} <= set(ids)


# ---------------------------------------------------------------- the DNS client itself

def _answer(query, records, rcode=0, tc=False):
    qid = struct.unpack(">H", query[:2])[0]
    # question section ends after the name's zero byte + 4
    end = query.index(b"\x00", 12) + 5
    question = query[12:end]
    flags = 0x8180 | rcode | (0x0200 if tc else 0)
    out = struct.pack(">HHHHHH", qid, flags, 1, len(records), 0, 0) + question
    for txt in records:
        rdata = b"".join(bytes([len(p)]) + p for p in txt)
        out += b"\xc0\x0c" + struct.pack(">HHIH", 16, 1, 300, len(rdata)) + rdata      # compressed name pointer
    return out


class FakeResolver:
    def __init__(self, table, truncate=()):
        self.table, self.truncate = table, set(truncate)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.port = self.udp.getsockname()[1]
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind(("127.0.0.1", self.port))
        self.tcp.listen(4)
        self.tcp_used = 0
        threading.Thread(target=self._udp_loop, daemon=True).start()
        threading.Thread(target=self._tcp_loop, daemon=True).start()

    def _name(self, q):
        off, parts = 12, []
        while q[off]:
            parts.append(q[off + 1:off + 1 + q[off]].decode())
            off += 1 + q[off]
        return ".".join(parts)

    def _reply(self, q, via_tcp):
        name = self._name(q)
        if name not in self.table:
            return _answer(q, [], rcode=3)
        if name in self.truncate and not via_tcp:
            return _answer(q, [], tc=True)
        return _answer(q, self.table[name])

    def _udp_loop(self):
        while True:
            try:
                q, addr = self.udp.recvfrom(4096)
            except OSError:
                return
            self.udp.sendto(self._reply(q, False), addr)

    def _tcp_loop(self):
        while True:
            try:
                c, _ = self.tcp.accept()
            except OSError:
                return
            self.tcp_used += 1
            n = struct.unpack(">H", c.recv(2))[0]
            q = b""
            while len(q) < n:
                q += c.recv(n - len(q))
            a = self._reply(q, True)
            c.sendall(struct.pack(">H", len(a)) + a)
            c.close()

    def close(self):
        self.udp.close()
        self.tcp.close()


def test_dns_client_reads_txt_nxdomain_nodata_and_falls_back_to_tcp():
    long_key = [b"v=DKIM1; k=rsa; p=" + b"A" * 230, b"B" * 200]
    r = FakeResolver({"a.example": [[b"v=spf1 ", b"-all"], [b"other"]], "empty.example": [],
                      "k._domainkey.a.example": [long_key]}, truncate={"k._domainkey.a.example"})
    try:
        q = lambda n: dnstxt.query_txt(n, timeout=2, servers=["127.0.0.1"], port=r.port)  # noqa: E731
        assert q("a.example") == ("ok", ["v=spf1 -all", "other"])          # pieces of one record are joined
        assert q("nope.example") == ("nxdomain", [])
        assert q("empty.example") == ("nodata", [])
        st, recs = q("k._domainkey.a.example")
        assert st == "ok" and recs[0].endswith("B" * 200) and r.tcp_used == 1
    finally:
        r.close()


def test_dns_client_reports_errors_instead_of_no_record():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))                    # answers nothing: a timeout
    try:
        assert dnstxt.query_txt("a.example", timeout=0.3, servers=["127.0.0.1"], port=s.getsockname()[1]) == ("error", [])
    finally:
        s.close()
    assert dnstxt.query_txt("bad..name", servers=["127.0.0.1"]) == ("error", [])
    assert dnstxt.query_txt("a.example", servers=[]) == ("error", [])
    with pytest.raises(ValueError):
        dnstxt.parse_response(b"\x00\x01" + b"\x81\x80" + b"\x00\x00\x00\x01\x00\x00\x00\x00" + b"\xc0\x0c" * 100, 1)


def test_dns_pointer_loop_is_refused():
    pkt = struct.pack(">HHHHHH", 7, 0x8180, 0, 1, 0, 0) + b"\xc0\x0c" + struct.pack(">HHIH", 16, 1, 1, 0)
    # a pointer to itself is read once and ends the name, so this parses safely
    assert dnstxt.parse_response(pkt, 7) == ("ok", [""])
