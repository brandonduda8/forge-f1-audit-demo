"""Pro checkup: the extra pages (f1_pages.py) and runner.py's `pro` mode."""
import http.server
import json
import os
import shutil
import subprocess
import sys
from functools import partial

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
import f1_pages  # noqa: E402
import netguard  # noqa: E402
import runner  # noqa: E402
import helpers  # noqa: E402
from helpers import FIX, serve, serve_site  # noqa: E402

UA = "Mozilla/5.0 Forge-F1-Audit/1.0"
ALL_IDS = [cid for cid, _l in f1_pages.CHECKS]


@pytest.fixture(autouse=True)
def _restore_sockets():
    yield
    netguard.uninstall()


def _copy_site(tmp_path, name="pro-site"):
    d = tmp_path / name
    shutil.copytree(os.path.join(FIX, name), d)
    return str(d)


def _write_sitemap(d, base):
    tmpl = open(os.path.join(d, "sitemap.xml.tmpl")).read()
    with open(os.path.join(d, "sitemap.xml"), "w") as f:
        f.write(tmpl.replace("{BASE}", base))


@pytest.fixture
def pro_site(tmp_path):
    d = _copy_site(tmp_path)
    srv, base = serve_site(d)
    _write_sitemap(d, base)
    yield srv, base, d
    srv.shutdown()


def run_runner(mode, url, **extra_env):
    env = {"PATH": os.environ["PATH"], "HTTPS_PROXY": "http://10.9.9.9:1", "HTTP_PROXY": "http://10.9.9.9:1",
           "SELFSERVE_TEST_ALLOW_LOOPBACK": "1", **extra_env}
    p = subprocess.run([sys.executable, "-I", os.path.join(API, "runner.py"), mode, url],
                       capture_output=True, text=True, timeout=180, env=env)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def _guard(monkeypatch):
    monkeypatch.setenv("SELFSERVE_TEST_ALLOW_LOOPBACK", "1")
    netguard.install()


def _home(base):
    return netguard.GuardedRequests(5, 30).get(base + "/", headers={"User-Agent": UA})


def by_path(pages):
    return {p["path"]: p for p in pages}


def ids(page, key="issues"):
    return {x["id"] for x in page[key]}


class Resp:
    def __init__(self, html="", url="https://shop.example/about", status=200, headers=None, history=()):
        self.text, self.url, self.status_code = html, url, status
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.history = list(history)


# ---------------------------------------------------------------- robots.txt

def test_robots_rules_groups_wildcards_and_longest_match():
    rules = f1_pages.parse_robots("""
# comment
User-agent: Googlebot
Disallow: /

User-agent: *
Disallow: /private/
Disallow: /*.php$
Allow: /private/open
Disallow: /tmp
Sitemap: https://x.example/s.xml
""")
    ok = f1_pages.robots_allowed
    assert not ok(rules, "/private/a.html") and ok(rules, "/private/open/x.html")
    assert not ok(rules, "/index.php") and ok(rules, "/index.php5")
    assert not ok(rules, "/tmp") and not ok(rules, "/tmp-files/a") and ok(rules, "/about")
    assert ok(rules, "/robots.txt")
    assert f1_pages.robots_sitemaps("Sitemap: https://x.example/s.xml\n") == ["https://x.example/s.xml"]
    # our own group, when there is one, replaces '*'; Allow wins a tie
    ours = f1_pages.parse_robots("User-agent: *\nDisallow: /\n\nUser-agent: Forge-F1-Audit\nDisallow: /a\nAllow: /a\n")
    assert ok(ours, "/a") and ok(ours, "/b")
    # several agents in one group, empty Disallow allows everything
    multi = f1_pages.parse_robots("User-agent: bingbot\nUser-agent: *\nDisallow:\n")
    assert multi == [] and ok(multi, "/anything")


# ---------------------------------------------------------------- choosing pages

def _fixture_inputs(base, d):
    soup = BeautifulSoup(open(os.path.join(d, "index.html")).read(), "lxml")
    links = f1_pages.homepage_links(soup)
    locs = f1_pages.sitemap_locs(open(os.path.join(d, "sitemap.xml")).read())
    rules = f1_pages.parse_robots(open(os.path.join(d, "robots.txt")).read())
    return links, locs, rules


def test_choose_pages_prefers_kinds_skips_junk_and_respects_robots(pro_site):
    _srv, base, d = pro_site
    links, locs, rules = _fixture_inputs(base, d)
    got = f1_pages.choose_pages(base + "/", links, locs, rules)
    assert got == [base + "/about.html", base + "/services.html", base + "/pricing.html", base + "/gallery"]
    # without the robots rules, the sitemap's locations page would have been picked
    free = f1_pages.choose_pages(base + "/", links, locs, [])
    assert base + "/locations.html" in free and len(free) == 4
    for u in got + free:
        assert "?" not in u and "#" not in u and u.startswith(base)
        assert not u.endswith((".pdf", ".webp")) and u != base + "/"
    assert len(f1_pages.choose_pages(base + "/", links, locs, rules, max_pages=2)) == 2


def test_choose_pages_dedupes_and_ranks():
    home = "https://shop.example/"
    links = [("/", "nav"), ("/blog", "nav"), ("index.html", "page"), ("/menu/", "page"), ("/menu", "nav"),
             ("https://shop.example/contact-us?x=1", "nav"), ("HTTPS://SHOP.EXAMPLE/contact-us", "page"),
             ("//cdn.example/about", "nav"), ("javascript:void(0)", "nav"), ("tel:+1555", "nav"),
             ("/files/price-list.docx", "nav"), ("/our-story/", "page"), ("/a/b/c/about", "page")]
    got = f1_pages.choose_pages(home, links, ["https://shop.example/products/", "http://shop.example/pricing"])
    # about (shallowest), contact, products, menu: one of each preferred kind, in preference order
    assert got == ["https://shop.example/our-story/", "https://shop.example/contact-us",
                   "https://shop.example/products/", "https://shop.example/menu/"]
    assert f1_pages.choose_pages(home, [("/", "nav"), ("#x", "nav")], []) == []


# ---------------------------------------------------------------- each check

HOME = {"title": "Shop Example | Home", "description": "The homepage description."}
GOOD = """<!doctype html><html lang="en"><head><title>About | Shop Example</title>
<meta name="description" content="Who we are."><meta name="viewport" content="width=device-width">
<link rel="canonical" href="https://shop.example/about"></head>
<body><nav>menu words</nav><h1>About</h1><img src=a.png alt="x"><p>%s</p></body></html>""" % ("word " * 200)


def _page(html, url="https://shop.example/about", **kw):
    return f1_pages.check_page(url, Resp(html, url=url, **kw), HOME)


def test_a_good_page_passes_all_ten_checks():
    p = _page(GOOD)
    assert p["issues"] == [] and p["passed"] == 10 and p["not_measured"] == []
    assert p["title"] == "About | Shop Example" and p["path"] == "/about"
    assert set(p) == {"url", "path", "title", "issues", "passed", "not_measured"}


@pytest.mark.parametrize("old,new,cid,sev", [
    ("<title>About | Shop Example</title>", "", "title", "MAJOR"),
    ("<title>About | Shop Example</title>", "<title>Shop Example | Home</title>", "title", "MINOR"),
    ("<title>About | Shop Example</title>", "<title>%s</title>" % ("x" * 70), "title", "MINOR"),
    ('<meta name="description" content="Who we are.">', "", "meta_description", "MINOR"),
    ('content="Who we are."', 'content="The homepage description."', "meta_description", "MINOR"),
    ('content="Who we are."', 'content="%s"' % ("d" * 170), "meta_description", "MINOR"),
    ("<h1>About</h1>", "", "h1", "MAJOR"),
    ("<h1>About</h1>", "<h1>About</h1><h1>Again</h1>", "h1", "MINOR"),
    ('<img src=a.png alt="x">', '<img src=a.png alt="x"><img src=b.png>', "alt_text", "MAJOR"),
    ('<html lang="en">', "<html>", "lang", "MINOR"),
    ('<meta name="viewport" content="width=device-width">', "", "viewport", "MAJOR"),
    ("word " * 200, "too few words here", "thin_content", "MINOR"),
    ("<h1>About</h1>", '<meta name="robots" content="noindex,follow"><h1>About</h1>', "noindex", "MAJOR"),
    ("<h1>About</h1>", '<meta name="googlebot" content="none"><h1>About</h1>', "noindex", "MAJOR"),
    ('<link rel="canonical" href="https://shop.example/about">', "", "canonical", "MINOR"),
    ('href="https://shop.example/about"', 'href="https://other.example/about"', "canonical", "MAJOR"),
    ('href="https://shop.example/about"', 'href="/"', "canonical", "MAJOR"),
    ('<link rel="canonical" href="https://shop.example/about">',
     '<link rel="canonical" href="/about"><link rel="canonical" href="/about/">', "canonical", "MINOR"),
])
def test_each_check_flags_its_problem(old, new, cid, sev):
    html = GOOD.replace(old, new, 1)
    assert html != GOOD
    p = _page(html)
    assert [(i["id"], i["severity"]) for i in p["issues"]] == [(cid, sev)]
    assert p["passed"] == 9 and all(i["title"] and i["detail"] for i in p["issues"])


def test_alt_text_minor_when_few_missing_and_not_measured_without_images():
    html = GOOD.replace('<img src=a.png alt="x">', '<img src=a.png alt="x">' * 9 + "<img src=b.png>")
    assert [(i["id"], i["severity"]) for i in _page(html)["issues"]] == [("alt_text", "MINOR")]
    p = _page(GOOD.replace('<img src=a.png alt="x">', ""))
    assert p["issues"] == [] and [n["id"] for n in p["not_measured"]] == ["alt_text"] and p["passed"] == 9


def test_noindex_header_and_contact_pages_are_not_thin():
    p = _page(GOOD, headers={"X-Robots-Tag": "noindex"})
    assert ids(p) == {"noindex"} and "X-Robots-Tag" in p["issues"][0]["detail"]
    url = "https://shop.example/contact"
    p = _page(GOOD.replace("word " * 200, "Call us.").replace("/about\"", "/contact\""), url=url)
    assert p["issues"] == [] and [n["id"] for n in p["not_measured"]] == ["thin_content"]


def test_canonical_trailing_slash_and_relative_self_are_fine():
    assert _page(GOOD.replace('href="https://shop.example/about"', 'href="/about/"'))["issues"] == []


def test_broken_redirecting_offsite_nonhtml_and_bot_check_pages():
    p = _page("<html><title>Not found</title></html>", status=404)
    assert [(i["id"], i["severity"]) for i in p["issues"]] == [("status", "CRITICAL")]
    assert "404" in p["issues"][0]["title"] and p["passed"] == 0
    assert [n["id"] for n in p["not_measured"]] == ALL_IDS[1:]
    p = f1_pages.check_page("https://shop.example/about-us",
                            Resp(GOOD, url="https://shop.example/about", history=[object()]), HOME)
    assert [(i["id"], i["severity"]) for i in p["issues"]] == [("status", "MINOR")]
    assert "/about" in p["issues"][0]["detail"] and p["passed"] == 9
    p = f1_pages.check_page("https://shop.example/x", Resp(GOOD, url="https://elsewhere.example/", history=[1]), HOME)
    assert ids(p) == {"status"} and [n["id"] for n in p["not_measured"]] == ALL_IDS[1:]
    p = _page("%PDF-1.4", headers={"Content-Type": "application/pdf"})
    assert p["issues"] == [] and p["passed"] == 1 and "application/pdf" in p["not_measured"][0]["reason"]
    # a bot-check page is the site refusing our checker: the whole page is not measured, never passed
    p = _page("<html><title>Just a moment...</title></html>")
    assert p["issues"] == [] and p["passed"] == 0 and [n["id"] for n in p["not_measured"]] == ALL_IDS
    assert all("refused our checker" in n["reason"] and "bot-check" in n["reason"] for n in p["not_measured"])


# ---------------------------------------------------------------- budget and robots in a real fetch

def test_pages_step_fetches_only_allowed_pages(pro_site, monkeypatch):
    srv, base, _d = pro_site
    _guard(monkeypatch)
    home = _home(base)
    del srv.seen[:]
    c = netguard.GuardedRequests(150, 60)
    st = f1_pages.check_pages(c, base + "/", home, UA)
    pages = by_path(st["pages"])
    assert list(pages) == ["/about.html", "/services.html", "/pricing.html", "/gallery"]
    # robots.txt, sitemap.xml, four pages, and /gallery's redirect to /gallery/ as its own counted hop
    assert c.count == 2 + 4 + 1
    got = [p for _m, p in srv.seen]
    assert got[:2] == ["/robots.txt", "/sitemap.xml"]
    assert not any(p.startswith("/locations") or p.endswith(".pdf") or "?" in p for p in got)
    assert ids(pages["/about.html"]) == {"title"}
    assert ids(pages["/services.html"]) == {"noindex", "canonical"}
    assert ids(pages["/pricing.html"]) == {"status"} and "404" in pages["/pricing.html"]["issues"][0]["title"]
    assert ids(pages["/gallery"]) == {"status", "meta_description", "h1", "alt_text", "lang", "viewport",
                                      "thin_content", "canonical"}
    assert pages["/gallery"]["title"] == "Gallery | Sample Bakery"


@pytest.mark.parametrize("cap,measured", [(1, 0), (2, 1), (3, 1), (5, 3)])
def test_request_budget_cut_marks_the_rest_not_measured(pro_site, monkeypatch, cap, measured):
    # robots.txt first; the sitemap only while a request is left for a page after it
    _srv, base, _d = pro_site
    _guard(monkeypatch)
    home = _home(base)
    c = netguard.GuardedRequests(cap, 60)
    st = f1_pages.check_pages(c, base + "/", home, UA)
    assert c.count <= cap and len(st["pages"]) == 4
    for p in st["pages"][measured:]:
        assert p["issues"] == [] and p["passed"] == 0
        assert [n["id"] for n in p["not_measured"]] == ALL_IDS
        assert all("limit" in n["reason"] for n in p["not_measured"])
    for p in st["pages"][:measured]:
        assert p["issues"] or p["passed"]


def test_time_budget_cut_fetches_nothing_and_fails_nothing(pro_site, monkeypatch):
    srv, base, _d = pro_site
    _guard(monkeypatch)
    home = _home(base)
    del srv.seen[:]
    monkeypatch.setattr(f1_pages, "MIN_TIME_S", 10_000)
    c = netguard.GuardedRequests(150, 60)
    st = f1_pages.check_pages(c, base + "/", home, UA)
    assert c.count == 0 and srv.seen == []
    assert st["pages"] and all(p["issues"] == [] and p["passed"] == 0 for p in st["pages"])
    assert "limit" in st["note"]


def test_cached_responses_are_reused(pro_site, monkeypatch):
    srv, base, _d = pro_site
    _guard(monkeypatch)
    home = _home(base)
    warm = netguard.GuardedRequests(20, 60)
    cache = {u: warm.get(u, headers={"User-Agent": UA}) for u in
             (base + "/robots.txt", base + "/sitemap.xml", base + "/about.html")}
    c = netguard.GuardedRequests(150, 60)
    st = f1_pages.check_pages(c, base + "/", home, UA, cache=cache)
    assert c.count == 4 and len(st["pages"]) == 4      # only services, pricing and gallery (+ its redirect)


class _RobotsDown(helpers._Quiet):
    def do_GET(self):
        if self.path == "/robots.txt":
            self._seen()
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return super().do_GET()


def test_robots_server_error_means_no_pages_fetched(tmp_path, monkeypatch):
    d = _copy_site(tmp_path)
    srv, base = serve(partial(_RobotsDown, directory=d))
    _guard(monkeypatch)
    home = _home(base)
    del srv.seen[:]
    c = netguard.GuardedRequests(150, 60)
    st = f1_pages.check_pages(c, base + "/", home, UA)
    srv.shutdown()
    assert [p for _m, p in srv.seen] == ["/robots.txt"]
    assert st["pages"] and all(p["passed"] == 0 and not p["issues"] for p in st["pages"])
    assert "503" in st["note"] and "503" in st["pages"][0]["not_measured"][0]["reason"]


def test_missing_robots_allows_and_robots_sitemap_index_is_followed(tmp_path, monkeypatch):
    d = _copy_site(tmp_path)
    os.remove(os.path.join(d, "robots.txt"))
    srv, base = serve_site(d)
    os.makedirs(os.path.join(d, "maps"))
    # no robots.txt (404) and no /sitemap.xml: everything allowed, pages come from the homepage links
    _guard(monkeypatch)
    home = _home(base)
    st = f1_pages.check_pages(netguard.GuardedRequests(150, 60), base + "/", home, UA)
    assert len(st["pages"]) == 4 and st["note"] is None
    # now robots.txt names a sitemap index, which names the real sitemap
    with open(os.path.join(d, "robots.txt"), "w") as f:
        f.write(f"User-agent: *\nDisallow: /about\nSitemap: {base}/maps/index.xml\n")
    with open(os.path.join(d, "maps", "index.xml"), "w") as f:
        f.write(f"<sitemapindex><sitemap><loc>{base}/maps/pages.xml</loc></sitemap></sitemapindex>")
    with open(os.path.join(d, "maps", "pages.xml"), "w") as f:
        f.write(f"<urlset><url><loc>{base}/locations.html</loc></url></urlset>")
    del srv.seen[:]
    st = f1_pages.check_pages(netguard.GuardedRequests(150, 60), base + "/", home, UA)
    srv.shutdown()
    got = [p for _m, p in srv.seen]
    assert got[:4] == ["/robots.txt", "/sitemap.xml", "/maps/index.xml", "/maps/pages.xml"]
    paths = [p["path"] for p in st["pages"]]
    assert "/locations.html" in paths and "/about.html" not in paths


def test_finish_unmeasured_completes_an_interrupted_step():
    st = f1_pages.new_state()
    st["selected"] = ["https://x.example/a", "https://x.example/b"]
    st["pages"] = [f1_pages.unmeasured_page("https://x.example/a", "x")]
    f1_pages.finish_unmeasured(st)
    assert [p["url"] for p in st["pages"]] == st["selected"]
    assert "limit" in st["pages"][1]["not_measured"][0]["reason"]


# ---------------------------------------------------------------- the runner

def test_pro_run_end_to_end(pro_site):
    srv, base, _d = pro_site
    full = run_runner("full", base + "/")
    del srv.seen[:]
    pro = run_runner("pro", base + "/")
    assert pro["ok"] and pro["mode"] == "pro" and pro["requests_made"] <= 150
    # the homepage part is the full report, and the grade is the homepage's
    assert (pro["score"], pro["grade"]) == (full["score"], full["grade"])
    assert set(pro) == set(full)
    assert set(pro["report"]) == set(full["report"]) | {"pages"}
    for key in ("findings", "passed", "not_measured"):
        assert [x["id"] for x in pro["report"][key]] == [x["id"] for x in full["report"][key]]
    assert [c["score"] for c in pro["report"]["categories"]] == [c["score"] for c in full["report"]["categories"]]
    # the extra pages
    pages = by_path(pro["report"]["pages"])
    assert list(pages) == ["/about.html", "/services.html", "/pricing.html", "/gallery"]
    assert ids(pages["/about.html"]) == {"title"}
    assert ids(pages["/services.html"]) == {"noindex", "canonical"}
    assert ids(pages["/pricing.html"]) == {"status"}
    assert "status" in ids(pages["/gallery"])
    for p in pages.values():
        assert set(p) == {"url", "path", "title", "issues", "passed", "not_measured"}
        assert p["passed"] + len(p["issues"]) + len(p["not_measured"]) == 10
    # robots.txt is respected: the disallowed page is never requested
    assert not any(p.startswith("/locations") for _m, p in srv.seen)
    # pages the homepage crawl already fetched are reused, not fetched again
    assert pro["requests_made"] <= full["requests_made"] + 4
    md = pro["markdown"]
    assert "## PAGES CHECKED" in md and "### Page 3: /pricing.html" in md
    assert md.index("## PAGES CHECKED") < md.index("## WHAT TO DO NEXT")
    assert "PAGES CHECKED" not in full["markdown"] and "pages" not in full["report"]


def test_pro_run_request_cap_honoured(pro_site):
    _srv, base, _d = pro_site
    res = run_runner("pro", base + "/", RUNNER_TEST_MAX_REQUESTS="8")
    assert res["ok"] and res["requests_made"] <= 8
    assert len(res["report"]["pages"]) == 4
    nm = {n["id"] for n in res["report"]["not_measured"]}
    assert {"4.3", "6.6"} <= nm                 # the homepage's own cut is not measured, not failed
    cut = [p for p in res["report"]["pages"] if not p["issues"] and not p["passed"]]
    assert cut                                   # the budget ran out before every page was fetched
    for p in res["report"]["pages"]:
        if not p["issues"] and not p["passed"]:
            assert all("limit" in n["reason"] for n in p["not_measured"])


def test_pro_run_deadline_honoured(pro_site):
    import time
    _srv, base, _d = pro_site
    t0 = time.time()
    res = run_runner("pro", base + "/", RUNNER_TEST_DEADLINE="4")
    assert time.time() - t0 < 30 and res["ok"]


def test_light_stays_three_requests_and_full_has_no_pages(pro_site):
    _srv, base, _d = pro_site
    light = run_runner("light", base + "/")
    assert light["requests_made"] == 3 and "report" not in light and "pages" not in light
    assert runner.LIMITS["pro"] == {"max_requests": 150, "deadline_s": 120, "max_resources": 40}
    assert runner.LIMITS["light"]["max_requests"] == 3 and runner.LIMITS["full"]["max_requests"] == 90


def test_bad_mode_is_refused():
    with pytest.raises(ValueError):
        runner.run("everything", "example.com")


# ---------------------------------------------------------------- the Markdown and PDF

def _ev_sc():
    ev = {"checks": {"2.1": {"value": True, "method": "m"}, "3.3": {"value": {"h1_count": 1, "h1s": ["x"]}, "method": "m"}}}
    return ev, pipe.score(ev)


def test_markdown_pages_section_is_safe_and_optional():
    ev, sc = _ev_sc()
    plain = pipe.build_report("x.example", "https://x.example/", ev, sc, "2026-11-26", "x.example", variant="selfserve")
    same = pipe.build_report("x.example", "https://x.example/", ev, sc, "2026-11-26", "x.example", variant="selfserve",
                             pages=None)
    assert plain == same and "PAGES CHECKED" not in plain
    page = _page(GOOD.replace("About | Shop Example", "**Big** [deal](javascript:x) | `code`"))
    md = pipe.build_report("x.example", "https://x.example/", ev, sc, "2026-11-26", "x.example", variant="selfserve",
                           pages=[page, f1_pages.unmeasured_page("https://x.example/b", f1_pages.R_BUDGET)])
    sec = md[md.index("## PAGES CHECKED"):md.index("## WHAT TO DO NEXT")]
    assert "### Page 1: /about" in sec and "### Page 2: /b" in sec
    assert "Big deal(javascript:x) | code" in sec and "**Big**" not in sec and "`" not in sec
    assert "Not counted as a problem" in sec and "10 passed, 0 problem(s)" in sec
    empty = pipe.build_report("x.example", "https://x.example/", ev, sc, "2026-11-26", "x.example",
                              variant="selfserve", pages=[], pages_note="no other pages were found")
    assert "No other pages were checked: no other pages were found." in empty


def test_pro_markdown_renders_to_pdf(pro_site, tmp_path):
    _srv, base, _d = pro_site
    md = run_runner("pro", base + "/")["markdown"]
    src, out = tmp_path / "r.md", tmp_path / "r.pdf"
    src.write_text(md)
    p = subprocess.run([sys.executable, os.path.join(REPO, "f1-audit-product", "tools", "render_pdf.py"),
                        str(src), str(out)], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert out.stat().st_size > 5000


# ---------------------------------------------------------------- review fixes: malformed URLs, refusals, robots

class _EdgeSite(helpers._Quiet):
    """The pro-edge-site fixture plus answers a static server can't give."""
    STATUS = {"/members": 403, "/login-only": 401, "/busy": 429, "/oops": 500, "/down": 503, "/gone": 410}
    REDIRECT = {"/old-services": "/private/services.html", "/to-elsewhere": "https://elsewhere.example/",
                "/bad-redirect": "http://[x/", "/hop": "/about.html"}

    def _send(self, status, body=b"", headers=()):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in self.STATUS:
            self._seen()
            st = self.STATUS[path]
            return self._send(st, b"<html><head><title>Error %d</title></head><body>no</body></html>" % st,
                              [("Content-Type", "text/html")])
        if path in self.REDIRECT:
            self._seen()
            return self._send(301, headers=[("Location", self.REDIRECT[path])])
        if path == "/check":
            self._seen()
            return self._send(200, b"<html><head><title>Attention Required! | Cloudflare</title></head>"
                                   b"<body>verify you are human</body></html>", [("Content-Type", "text/html")])
        return super().do_GET()


@pytest.fixture
def edge_site(tmp_path):
    d = _copy_site(tmp_path, "pro-edge-site")
    srv, base = serve(partial(_EdgeSite, directory=d))
    _write_sitemap(d, base)
    yield srv, base, d
    srv.shutdown()


def _seen_paths(srv):
    from urllib.parse import unquote
    return [unquote(p) for _m, p in srv.seen]


def test_malformed_urls_are_skipped_not_fatal():
    origin = f1_pages._origin("https://shop.example/")
    assert f1_pages._origin("http://[x/") is None
    for bad in ("http://[x/", "http://[::1/a", "http://a]b/"):
        assert f1_pages._candidate(bad, "https://shop.example/", origin) is None
    got = f1_pages.choose_pages("https://shop.example/", [("http://[x/", "nav"), ("/about", "nav")],
                                ["http://[y/", "https://shop.example/menu"])
    assert got == ["https://shop.example/about", "https://shop.example/menu"]
    # a malformed canonical link is a (minor) problem on the page, not a crash
    p = _page(GOOD.replace('href="https://shop.example/about"', 'href="http://[x/"'))
    assert [(i["id"], i["severity"]) for i in p["issues"]] == [("canonical", "MINOR")]
    assert "not a valid" in p["issues"][0]["title"]


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503])
def test_refused_pages_are_not_measured_never_broken(status):
    p = _page("<html><title>Forbidden</title></html>", status=status)
    assert p["issues"] == [] and p["passed"] == 0
    assert [n["id"] for n in p["not_measured"]] == ALL_IDS
    assert all("refused our checker" in n["reason"] and str(status) in n["reason"] for n in p["not_measured"])


@pytest.mark.parametrize("status", [404, 410])
def test_missing_pages_stay_broken(status):
    p = _page("<html><title>Not here</title></html>", status=status)
    assert [(i["id"], i["severity"]) for i in p["issues"]] == [("status", "CRITICAL")]
    assert str(status) in p["issues"][0]["title"]


def test_robots_paths_are_normalized_before_matching():
    n = f1_pages.norm_path
    assert n("/café") == n("/caf%c3%a9") == n("/caf%C3%A9") == "/caf%C3%A9"
    assert n("/%7Eme") == n("/~me") == "/~me"                      # unreserved: decoded
    assert n("/a%2Fb") == "/a%2Fb" and n("/a b") == "/a%20b" and n("/100%") == "/100%25"
    assert n(n("/café/x y%zz")) == n("/café/x y%zz")              # idempotent
    ok = f1_pages.robots_allowed
    encoded = f1_pages.parse_robots("User-agent: *\nDisallow: /caf%C3%A9\nDisallow: /~private/\n")
    for path in ("/café.html", "/caf%c3%a9.html", "/caf%C3%A9.html"):
        assert not ok(encoded, path)
    assert not ok(encoded, "/%7Eprivate/x") and ok(encoded, "/cafe.html")
    raw = f1_pages.parse_robots("User-agent: *\nDisallow: /café\nAllow: /café/menu$\n")
    assert not ok(raw, "/caf%C3%A9/drinks") and ok(raw, "/caf%c3%a9/menu") and ok(raw, "/caf")


def test_cached_redirect_to_disallowed_page_is_not_analysed():
    rules = f1_pages.parse_robots("User-agent: *\nDisallow: /private/\n")
    origin = f1_pages._origin("https://shop.example/")
    cache = {"https://shop.example/team": Resp(GOOD, url="https://shop.example/private/team", history=[1])}
    r, why, budget = f1_pages.fetch_page(None, "https://shop.example/team", cache, UA, rules, origin)
    assert r is None and why == f1_pages.R_ROBOTS_FINAL and not budget


def test_edge_pages_refusals_redirects_and_robots(edge_site, monkeypatch):
    srv, base, _d = edge_site
    _guard(monkeypatch)
    home = _home(base)
    assert f1_pages.choose_pages(base + "/", f1_pages.homepage_links(BeautifulSoup(home.text, "lxml")), [],
                                 f1_pages.parse_robots(open(os.path.join(_d, "robots.txt")).read()))
    targets = ["/about.html", "/café.html", "/old-services", "/members", "/login-only", "/busy", "/oops",
               "/down", "/gone", "/missing", "/check", "/to-elsewhere", "/bad-redirect", "/hop"]
    monkeypatch.setattr(f1_pages, "choose_pages",
                        lambda *a, **k: [base + t for t in targets if t != "/café.html"])
    del srv.seen[:]
    c = netguard.GuardedRequests(150, 60)
    st = f1_pages.check_pages(c, base + "/", home, UA, max_pages=len(targets))
    pages = by_path(st["pages"])
    seen = _seen_paths(srv)
    # robots: the redirect to /private/ is not followed; the disallowed page itself is never requested
    assert not any(p.startswith("/private") or p.startswith("/café") for p in seen)
    assert all(n["reason"] == f1_pages.R_ROBOTS_FINAL for n in pages["/old-services"]["not_measured"])
    assert pages["/old-services"]["issues"] == [] and pages["/old-services"]["passed"] == 0
    # refused = not measured; gone/missing = broken
    for path, code in (("/members", 403), ("/login-only", 401), ("/busy", 429), ("/oops", 500), ("/down", 503)):
        p = pages[path]
        assert p["issues"] == [] and p["passed"] == 0 and [n["id"] for n in p["not_measured"]] == ALL_IDS
        assert "refused our checker" in p["not_measured"][0]["reason"] and str(code) in p["not_measured"][0]["reason"]
    for path, code in (("/gone", 410), ("/missing", 404)):
        assert ids(pages[path]) == {"status"} and str(code) in pages[path]["issues"][0]["title"]
    chk = pages["/check"]
    assert chk["issues"] == [] and chk["passed"] == 0 and "bot-check" in chk["not_measured"][0]["reason"]
    # an off-site redirect is reported but the other site is never fetched; a malformed Location is not followed
    assert ids(pages["/to-elsewhere"]) == {"status"} and [n["id"] for n in pages["/to-elsewhere"]["not_measured"]] \
        == ALL_IDS[1:]
    assert all(n["reason"] == f1_pages.R_BAD_REDIRECT for n in pages["/bad-redirect"]["not_measured"])
    # a same-site redirect is followed hop by hop, every hop counted
    assert ids(pages["/hop"]) == {"status", "canonical"} and seen.count("/about.html") == 2
    assert c.count == len(seen)
    # the malformed canonical on /about.html is a page problem, not a crash
    assert "not a valid" in [i for i in pages["/about.html"]["issues"] if i["id"] == "canonical"][0]["title"]


def test_pro_run_with_malformed_links_and_sitemap_is_delivered(edge_site):
    srv, base, _d = edge_site
    full = run_runner("full", base + "/")
    assert full["ok"] and full["report"]["findings"] is not None and "pages" not in full["report"]
    del srv.seen[:]
    pro = run_runner("pro", base + "/")
    assert pro["ok"] and (pro["score"], pro["grade"]) == (full["score"], full["grade"])
    # /café.html is disallowed (Disallow: /caf%C3%A9) so never chosen; the homepage crawl already followed
    # /old-services to /private/, so that cached page is not analysed; /members answers 403
    pages = by_path(pro["report"]["pages"])
    assert list(pages) == ["/about.html", "/old-services", "/members"]
    assert ids(pages["/about.html"]) == {"canonical"}
    assert all(n["reason"] == f1_pages.R_ROBOTS_FINAL for n in pages["/old-services"]["not_measured"])
    assert pages["/members"]["issues"] == [] and "403" in pages["/members"]["not_measured"][0]["reason"]
    assert run_runner("light", base + "/")["ok"]


def test_utf8_links_read_right_without_a_charset_header():
    html = '<html><head><meta charset="utf-8"><title>Caf\u00e9</title></head><body><a href="/caf\u00e9">x</a></body></html>'
    r = Resp(html.encode("utf-8").decode("latin-1"), headers={"Content-Type": "text/html"})
    r.content = html.encode("utf-8")                 # what requests gives with no charset: Latin-1 text
    assert f1_pages.homepage_links(f1_pages.page_soup(r)) == [("/caf\u00e9", "page")]
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    r.text = html
    assert f1_pages.page_soup(r).title.get_text() == "Caf\u00e9"


def test_any_error_in_the_pages_step_still_delivers_the_homepage_report(pro_site):
    _srv, base, _d = pro_site
    code = ("import sys; sys.path.insert(0, %r); import runner, f1_pages\n"
            "def boom(*a, **k):\n"
            "    st = k['state']; st['selected'] = [%r]; raise RuntimeError('unexpected')\n"
            "f1_pages.check_pages = boom\n"
            "sys.exit(runner.main(['runner.py', 'pro', %r]))\n") % (API, base + "/about.html", base + "/")
    env = {"PATH": os.environ["PATH"], "HTTPS_PROXY": "http://10.9.9.9:1", "HTTP_PROXY": "http://10.9.9.9:1",
           "SELFSERVE_TEST_ALLOW_LOOPBACK": "1"}
    p = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=180, env=env)
    assert p.returncode == 0, p.stderr
    res = json.loads(p.stdout.strip().splitlines()[-1])
    assert res["ok"] and res["grade"] and res["report"]["findings"] is not None
    assert [x["path"] for x in res["report"]["pages"]] == ["/about.html"]
    pg = res["report"]["pages"][0]
    assert pg["issues"] == [] and pg["passed"] == 0 and [n["id"] for n in pg["not_measured"]] == ALL_IDS
    assert all(n["reason"] == runner.R_PAGES_FAILED for n in pg["not_measured"])
    assert "## PAGES CHECKED" in res["markdown"]
