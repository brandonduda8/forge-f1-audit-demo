"""Offline checks for the Apex Forge Website Checkup static site (stdlib + pytest)."""
import os
import re
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser

import pytest

SITE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_FILES = sorted(f for f in os.listdir(SITE) if f.endswith(".html"))
JS_FILES = sorted(f for f in os.listdir(SITE) if f.endswith(".js") and f not in ("config.js", "config.example.js"))
CSS_FILES = sorted(f for f in os.listdir(SITE) if f.endswith(".css"))

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
EXPECTED_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src https:; form-action 'self'; base-uri 'none'")
URL_RE = re.compile(r"https?://([A-Za-z0-9.\-]+)", re.I)


def read(name):
    with open(os.path.join(SITE, name), encoding="utf-8") as f:
        return f.read()


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors = [], []
        self.title = None
        self._in_title = False
        self._in_script = False
        self.metas, self.scripts, self.html_attrs = [], [], None
        self.style_attrs, self.event_attrs, self.style_tags = 0, 0, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.html_attrs = a
        if tag == "meta":
            self.metas.append(a)
        if tag == "title":
            self._in_title, self.title = True, ""
        if tag == "script":
            self._in_script = True
            self.scripts.append({"attrs": a, "body": ""})
        if tag == "style":
            self.style_tags += 1
        if "style" in a:
            self.style_attrs += 1
        if any(k.startswith("on") for k in a):
            self.event_attrs += 1
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._in_script = False
        if not self.stack:
            self.errors.append(f"stray </{tag}> at {self.getpos()}")
            return
        top, pos = self.stack.pop()
        if top != tag:
            self.errors.append(f"<{top}> opened at {pos} closed by </{tag}> at {self.getpos()}")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_script and self.scripts:
            self.scripts[-1]["body"] += data


def parse(name):
    p = PageParser()
    p.feed(read(name))
    p.close()
    return p


def test_html_files_found():
    assert set(HTML_FILES) >= {"index.html", "report.html", "privacy.html", "terms.html", "404.html"}


@pytest.mark.parametrize("name", HTML_FILES)
def test_no_unclosed_or_mismatched_tags(name):
    p = parse(name)
    assert not p.errors, p.errors
    assert not p.stack, f"unclosed tags: {p.stack}"


@pytest.mark.parametrize("name", HTML_FILES)
def test_page_basics(name):
    p = parse(name)
    assert read(name).lstrip().lower().startswith("<!doctype html>")
    assert p.html_attrs and p.html_attrs.get("lang") == "en"
    assert p.title and p.title.strip()
    metas = p.metas
    assert any(m.get("charset", "").lower() == "utf-8" for m in metas)
    assert any(m.get("name") == "viewport" and "width=device-width" in m.get("content", "") for m in metas)
    assert any(m.get("name") == "description" and m.get("content", "").strip() for m in metas)
    csp = [m.get("content") for m in metas if (m.get("http-equiv") or "").lower() == "content-security-policy"]
    assert csp == [EXPECTED_CSP]


@pytest.mark.parametrize("name", HTML_FILES)
def test_no_inline_scripts_styles_or_handlers(name):
    p = parse(name)
    for s in p.scripts:
        assert s["body"].strip() == "", f"inline script body in {name}"
        src = s["attrs"].get("src", "")
        assert src and not URL_RE.search(src), f"script must be same-origin: {src!r}"
    assert p.style_attrs == 0, "style= attributes are blocked by the CSP"
    assert p.event_attrs == 0, "on*= attributes are blocked by the CSP"
    assert p.style_tags == 0


@pytest.mark.parametrize("name", HTML_FILES + JS_FILES + CSS_FILES)
def test_no_external_urls(name):
    text = read(name)
    hosts = {h.lower() for h in URL_RE.findall(text)}
    assert not hosts, f"external hosts in {name}: {hosts}"   # the Square checkout URL comes from our API
    assert "@import" not in text
    assert not re.search(r"url\(\s*['\"]?(https?:)?//", text)


@pytest.mark.parametrize("name", JS_FILES)
def test_no_innerhtml_or_eval(name):
    text = read(name)
    assert "innerHTML" not in text
    assert "outerHTML" not in text
    assert "insertAdjacentHTML" not in text
    assert "document.write" not in text
    assert not re.search(r"\beval\s*\(", text)
    assert "new Function" not in text


def test_index_meta_description_length():
    p = parse("index.html")
    desc = [m["content"] for m in p.metas if m.get("name") == "description"][0]
    assert 120 <= len(desc) <= 160, len(desc)


def test_legal_pages_marked_draft():
    for name in ("privacy.html", "terms.html"):
        head = "\n".join(read(name).splitlines()[:3])
        assert "<!-- DRAFT: Brandon should read before publishing; not legal advice -->" in head


def test_index_pages_load_config_plainly():
    for name in ("index.html", "report.html", "privacy.html", "terms.html"):
        assert '<script src="config.js"></script>' in read(name)


def test_no_human_contact_promises_or_fake_social_proof():
    banned = [r"schedule a call", r"book a call", r"free consultation", r"consultation", r"call us",
              r"reply to talk", r"talk to (an|our) expert", r"trusted by", r"testimonial from",
              r"\bcustomers served\b", r"limited time", r"offer ends", r"countdown", r"only \d+ left"]
    for name in HTML_FILES:
        text = read(name).lower()
        for pat in banned:
            assert not re.search(pat, text), f"{name} contains banned phrase {pat!r}"


def test_index_sample_is_labelled_fictional():
    assert "Example from a fictional test site" in read("index.html")


def test_index_buy_box_asks_for_the_website_before_checkout():
    html = read("index.html")
    assert 'id="buy-form"' in html and 'id="buy-url"' in html and 'id="buy-btn"' in html
    assert "We never see your card details." in html


def test_no_card_fields_anywhere():
    """Card details are only ever typed on Square's page."""
    for name in HTML_FILES:
        text = read(name).lower()
        for word in ("cc-number", "cc-csc", "cc-exp", "card-number", "cvv"):
            assert word not in text, f"{name} has a card field ({word})"


def test_no_secrets():
    pat = re.compile(r"(sk|rk)_(live|test)_[A-Za-z0-9]{8,}|whsec_[A-Za-z0-9]{8,}|EAAA[A-Za-z0-9_-]{20,}|-----BEGIN")
    for root, _, files in os.walk(SITE):
        if "__pycache__" in root or ".pytest_cache" in root:
            continue
        for f in files:
            path = os.path.join(root, f)
            if f == "test_site.py":
                continue
            with open(path, encoding="utf-8", errors="ignore") as fh:
                assert not pat.search(fh.read()), f"possible secret in {path}"


# ---- build_config.py ----

def run_build(env_extra, *args):
    out = tempfile.NamedTemporaryFile(suffix=".js", delete=False)
    out.close()
    os.unlink(out.name)
    env = {k: v for k, v in os.environ.items() if not k.startswith("APEX_")}
    env.update(env_extra)
    proc = subprocess.run([sys.executable, os.path.join(SITE, "build_config.py"), "--out", out.name, *args],
                          env=env, capture_output=True, text=True)
    content = None
    if os.path.exists(out.name):
        with open(out.name, encoding="utf-8") as f:
            content = f.read()
        os.unlink(out.name)
    return proc, content


def parse_config(content):
    import json
    m = re.match(r"^//[^\n]*\nwindow\.APEX_CONFIG = (\{.*\});\n$", content, re.S)
    assert m, content
    return json.loads(m.group(1))


GOOD = {"APEX_API_BASE": "https://forge-api.onrender.com/", "APEX_SUPPORT_EMAIL": "help@example.com"}


@pytest.mark.parametrize("bad", [
    {},
    {**GOOD, "APEX_API_BASE": "http://forge-api.onrender.com"},
    {**GOOD, "APEX_API_BASE": "forge-api.onrender.com"},
    {**GOOD, "APEX_API_BASE": "javascript:alert(1)"},
    {**GOOD, "APEX_API_BASE": "https://api.example.com/x?y=1"},
    {**GOOD, "APEX_API_BASE": "https://user:pw@api.example.com"},
    {**GOOD, "APEX_CHECKOUT": "maybe"},
    {**GOOD, "APEX_SUPPORT_EMAIL": "not-an-email"},
    {**GOOD, "APEX_PRICE": "<script>"},
])
def test_build_config_rejects_bad_input(bad):
    proc, content = run_build(bad)
    assert proc.returncode != 0
    assert content is None, "config.js must not be written on error"
    assert "build_config.py:" in proc.stderr


def test_build_config_writes_valid_config():
    proc, content = run_build({**GOOD, "APEX_SUPPORT_EMAIL": "help@example.com"})
    assert proc.returncode == 0, proc.stderr
    cfg = parse_config(content)
    assert cfg == {"apiBase": "https://forge-api.onrender.com", "checkout": True,
                   "price": "$29", "supportEmail": "help@example.com", "emails": False}
    proc, content = run_build({**GOOD, "APEX_CHECKOUT": "off"})
    assert proc.returncode == 0 and parse_config(content)["checkout"] is False


def test_build_config_custom_price_and_email_required():
    proc, content = run_build({**GOOD, "APEX_PRICE": "$39"})
    assert proc.returncode == 0, proc.stderr
    cfg = parse_config(content)
    assert cfg["price"] == "$39" and cfg["supportEmail"] == "help@example.com"
    no_email = {k: v for k, v in GOOD.items() if k != "APEX_SUPPORT_EMAIL"}
    proc, _ = run_build(no_email)
    assert proc.returncode != 0 and "APEX_SUPPORT_EMAIL" in proc.stderr


def test_build_config_example_mode_needs_no_env():
    proc, content = run_build({}, "--example")
    assert proc.returncode == 0, proc.stderr
    cfg = parse_config(content)
    assert cfg["checkout"] is True
    assert cfg["apiBase"].startswith("https://")


def test_config_example_js_matches_shape():
    text = read("config.example.js")
    assert "window.APEX_CONFIG = {" in text
    for key in ("apiBase", "checkout", "price", "supportEmail"):
        assert f'"{key}"' in text


# ---- v2: plans, landing pages, dashboard, admin, publish ----

LANDING = ["small-business-website-audit.html", "website-accessibility-check.html", "ssl-certificate-check.html",
           "spf-dmarc-email-check.html", "local-seo-website-check.html", "website-monitoring.html"]
INDEXABLE = ["index.html", *LANDING, "privacy.html", "terms.html"]
NOT_INDEXED = ["report.html", "find.html", "dashboard.html", "admin.html", "404.html"]


class Outline(HTMLParser):
    """Counts H1s, collects hrefs, ids and the FAQ terms of a page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.h1, self.hrefs, self.ids, self.faq_dt = 0, [], set(), 0
        self._faq_depth = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in VOID:
            self.depth += 1
        if tag == "h1":
            self.h1 += 1
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "dl" and "faq" in (a.get("class") or "").split():
            self._faq_depth = self.depth
        if tag == "dt" and self._faq_depth is not None:
            self.faq_dt += 1

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if tag == "dl" and self._faq_depth == self.depth:
            self._faq_depth = None
        self.depth -= 1


def outline(name):
    o = Outline()
    o.feed(read(name))
    return o


def meta(name, key):
    return [m.get("content", "") for m in parse(name).metas if m.get("name") == key]


def test_v2_pages_exist():
    assert set(HTML_FILES) >= set(INDEXABLE + NOT_INDEXED)


def test_titles_and_descriptions_are_unique():
    titles = [parse(n).title.strip() for n in HTML_FILES]
    descs = [meta(n, "description")[0] for n in HTML_FILES]
    assert len(set(titles)) == len(titles), titles
    assert len(set(descs)) == len(descs), descs


@pytest.mark.parametrize("name", ["index.html", *LANDING])
def test_landing_page_seo_basics(name):
    """Each page passes the checker's own bars: title 30-60, description 120-160, exactly one H1."""
    title = parse(name).title.strip()
    desc = meta(name, "description")[0]
    assert 30 <= len(title) <= 60, (name, len(title))
    assert 120 <= len(desc) <= 160, (name, len(desc))
    assert outline(name).h1 == 1


@pytest.mark.parametrize("name", LANDING)
def test_landing_page_has_faq_and_free_check(name):
    o = outline(name)
    assert o.faq_dt >= 3, "an FAQ with at least 3 questions"
    assert {"check-form", "site-url", "check-btn", "check-status", "check-result", "free-check"} <= o.ids
    assert "index.html#price" in o.hrefs
    html = read(name)
    for src in ("config.js", "common.js", "app.js"):
        assert f'<script src="{src}"></script>' in html
    assert not meta(name, "robots"), "landing pages are indexable"


@pytest.mark.parametrize("name", HTML_FILES)
def test_at_most_one_h1(name):
    assert outline(name).h1 <= 1


@pytest.mark.parametrize("name", NOT_INDEXED)
def test_private_pages_are_noindex(name):
    assert any("noindex" in c for c in meta(name, "robots"))


def test_admin_and_dashboard_are_nofollow_too():
    for name in ("admin.html", "dashboard.html"):
        assert meta(name, "robots") == ["noindex, nofollow"]


def test_internal_links_point_at_real_pages():
    for name in HTML_FILES:
        o = outline(name)
        for h in o.hrefs:
            if h.startswith(("#", "mailto:", "data:")) or h == "/":
                continue
            path, _, frag = h.lstrip("/").partition("#")
            path = path.split("?")[0]
            assert os.path.exists(os.path.join(SITE, path)), f"{name} links to missing {h}"
            if frag and path:
                assert frag in outline(path).ids, f"{name} links to missing anchor {h}"


def test_honest_claims():
    """No legal-compliance, guarantee or fake social-proof claims anywhere."""
    banned = [r"\bada[- ]compliant site\b", r"\bfully compliant\b", r"\bcertif(y|ies|ied) (your|that)\b",
              r"\bguaranteed\b", r"\b100% (accurate|compliant|secure)\b", r"\b\d[\d,]* (businesses|sites|customers) (use|trust|checked)\b",
              r"\b5[- ]star\b", "★", r"\bas seen (on|in)\b", r"\brated #?1\b", r"\bmoney[- ]back guarantee\b"]
    for name in HTML_FILES:
        text = read(name).lower()
        for pat in banned:
            assert not re.search(pat, text), f"{name} contains {pat!r}"
    a11y = read("website-accessibility-check.html")
    assert "not a legal accessibility audit" in a11y
    assert "can't tell you whether your site complies" in a11y


def test_landing_pages_only_describe_real_checks():
    """Spot-check the claims against the checker's own check list."""
    sys.path.insert(0, os.path.join(SITE, "..", "..", "audit-demo"))
    import importlib
    try:
        ap = importlib.import_module("audit_pipeline")
    except Exception as e:  # pragma: no cover - the pipeline needs its own deps
        pytest.skip(f"audit_pipeline not importable here: {e}")
    labels = ap.LABELS
    for cid in ("7.1", "7.2", "7.3", "7.4", "7.5", "8.1", "8.2", "8.3", "8.4", "8.5", "8.6", "8.7", "3.4", "3.5-linked", "4.6"):
        assert cid in labels, cid


def test_index_pricing_section():
    html = read("index.html")
    for plan in ("checkup", "pro", "monitor"):
        assert f'data-plan-card="{plan}"' in html and f'data-buy-plan="{plan}"' in html and f'data-plan-price="{plan}"' in html
    monitor = re.search(r'<article class="plan" data-plan-card="monitor"[^>]*>', html).group(0)
    assert " hidden" in monitor, "the monitor card starts hidden until /api/plans says it's available"
    btn = re.search(r'<button[^>]*data-buy-plan="monitor"[^>]*>', html).group(0)
    assert 'aria-describedby="monitor-renew"' in btn
    renew = re.search(r'<p id="monitor-renew"[^>]*>(.*?)</p>', html, re.S).group(1)
    for words in ("Renews monthly", "until you cancel", "Cancel online anytime from your dashboard", "end of the month"):
        assert words in renew
    assert "One-time payment. Not a subscription." in html


def test_terms_and_privacy_cover_v2():
    terms, privacy = read("terms.html"), read("privacy.html")
    for words in ("renews automatically", "cancel online at any time", "end of the month", "Check history", "Emails"):
        assert words in terms, words
    for words in ("Check history", "Emails we send", "No newsletters"):
        assert words in privacy, words


def test_admin_token_handling_in_source():
    js = read("admin.js")
    assert "localStorage" not in js, "the admin token never goes to localStorage"
    assert "sessionStorage" in js
    assert not re.search(r"[?&]token=", js), "the token is never put in a URL"
    assert "Authorization" in js and "Bearer " in js
    html = read("admin.html")
    assert 'id="admin-token" type="password" autocomplete="off"' in html
    field = re.search(r"<input[^>]*id=\"admin-token\"[^>]*>", html).group(0)
    assert "name=" not in field, "no name: even without JavaScript the token can't be submitted into a URL"
    assert 'id="admin-logout"' in html


def test_dashboard_never_uses_window_confirm():
    js = read("dashboard.js")
    assert "confirm(" not in js.replace('"cancel-confirm"', "")
    assert "/api/cancel" in js


def test_contract_renders_fixture_shapes():
    import shutil
    if not shutil.which("node"):
        pytest.skip("node not installed")
    fx = os.path.join(SITE, "tests", "fixtures")
    p = subprocess.run(["node", os.path.join(SITE, "tests", "contract.mjs"),
                        os.path.join(fx, "mini.json"), os.path.join(fx, "report-pro.json"),
                        "--plans=" + os.path.join(fx, "plans.json"), "--dashboard=" + os.path.join(fx, "dashboard.json"),
                        "--admin=" + os.path.join(fx, "admin.json")], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "Pro pages (2)" in p.stdout and "admin summary" in p.stdout


@pytest.mark.parametrize("bad", [
    {**GOOD, "APEX_EMAILS": "yes"},
    {**GOOD, "APEX_SITE_ORIGIN": "http://checkup.example.com"},
    {**GOOD, "APEX_SITE_ORIGIN": "https://checkup.example.com/sub"},
    {**GOOD, "APEX_SITE_ORIGIN": "https://checkup.example.com?x=1"},
    {**GOOD, "APEX_SITE_ORIGIN": "https://user@checkup.example.com"},
    {**GOOD, "APEX_SITE_ORIGIN": "https://check\"up.example.com"},
])
def test_build_config_rejects_bad_v2_input(bad):
    proc, content = run_build(bad)
    assert proc.returncode != 0 and content is None
    assert "build_config.py:" in proc.stderr


def test_build_config_emails_flag():
    proc, content = run_build({**GOOD, "APEX_EMAILS": "on"})
    assert proc.returncode == 0, proc.stderr
    assert parse_config(content)["emails"] is True


def run_publish(env_extra):
    work = tempfile.mkdtemp()
    out = os.path.join(work, "public")
    env = {k: v for k, v in os.environ.items() if not k.startswith("APEX_")}
    env.update({**GOOD, **env_extra})
    proc = subprocess.run([sys.executable, os.path.join(SITE, "build_config.py"), "--publish", out],
                          env=env, capture_output=True, text=True, cwd=work)
    return proc, out


def test_publish_excludes_tests_and_notes():
    proc, out = run_publish({})
    assert proc.returncode == 0, proc.stderr
    files = set(os.listdir(out))
    assert files >= {"index.html", "report.html", "find.html", "dashboard.html", "admin.html", "config.js",
                     "common.js", "style.css", "robots.txt", *LANDING}
    assert not files & {"tests", "README.md", "build_config.py", "config.example.js", ".gitignore"}
    for f in files:
        assert os.path.isfile(os.path.join(out, f)), f"{f} is a folder"
        assert f.endswith((".html", ".css", ".js")) or f in ("robots.txt", "sitemap.xml"), f
    assert "sitemap.xml" not in files, "no sitemap without APEX_SITE_ORIGIN"
    for name in INDEXABLE:
        with open(os.path.join(out, name), encoding="utf-8") as fh:
            assert 'rel="canonical"' not in fh.read(), f"no canonical without APEX_SITE_ORIGIN ({name})"
    with open(os.path.join(out, "robots.txt"), encoding="utf-8") as fh:
        assert "Sitemap:" not in "\n".join(l for l in fh.read().splitlines() if not l.startswith("#"))
    with open(os.path.join(out, "config.js"), encoding="utf-8") as fh:
        assert parse_config(fh.read())["apiBase"] == "https://forge-api.onrender.com"


def test_publish_with_origin_adds_canonical_sitemap_and_robots():
    proc, out = run_publish({"APEX_SITE_ORIGIN": "https://Checkup.Example.com/"})
    assert proc.returncode == 0, proc.stderr
    origin = "https://checkup.example.com"
    with open(os.path.join(out, "sitemap.xml"), encoding="utf-8") as fh:
        xml = fh.read()
    import xml.etree.ElementTree as ET
    locs = [e.text for e in ET.fromstring(xml).iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    assert locs[0] == origin + "/"
    assert set(locs) == {origin + "/"} | {origin + "/" + n for n in INDEXABLE if n != "index.html"}
    for name in INDEXABLE:
        with open(os.path.join(out, name), encoding="utf-8") as fh:
            html = fh.read()
        want = origin + ("/" if name == "index.html" else "/" + name)
        assert html.count('rel="canonical"') == 1
        assert f'<link rel="canonical" href="{want}">\n</head>' in html, name
    for name in NOT_INDEXED:
        with open(os.path.join(out, name), encoding="utf-8") as fh:
            assert 'rel="canonical"' not in fh.read(), name
    with open(os.path.join(out, "robots.txt"), encoding="utf-8") as fh:
        assert f"Sitemap: {origin}/sitemap.xml" in fh.read()
    # the source pages are left untouched
    assert 'rel="canonical"' not in read("index.html")


def test_publish_refuses_the_site_folder_itself():
    env = {k: v for k, v in os.environ.items() if not k.startswith("APEX_")}
    env.update(GOOD)
    proc = subprocess.run([sys.executable, os.path.join(SITE, "build_config.py"), "--publish", SITE],
                          env=env, capture_output=True, text=True)
    assert proc.returncode != 0


# ---------------------------------------------------------------- review fixes (round 1)
TWO_PHASE = '<span data-plan-terms="monitor">$29 for the first month, then $19 a month</span>'


@pytest.mark.parametrize("name", ["index.html", "terms.html", *LANDING])
def test_monitoring_price_is_two_phase_everywhere(name):
    """Every page that describes Monthly Monitoring shows both prices (filled from /api/plans at runtime)."""
    html = read(name)
    assert TWO_PHASE in html, name


def test_no_page_shows_a_single_monitoring_price():
    for name in HTML_FILES:
        html = read(name)
        text = re.sub(r"<[^>]+>", "", html)
        assert "per month" not in text, f"{name}: say 'a month' after the first month"
        if 'data-plan-price="monitor"' in html:
            assert "for the first month" in html, f"{name} shows the monthly price without the first-month price"
        assert not re.search(r"renews (each month|monthly) at the price shown", text, re.I), name


def test_landing_monitoring_line_only_when_monitoring_is_on_sale():
    for name in LANDING:
        if name == "website-monitoring.html":
            continue
        line = re.search(r'<p[^>]*data-if-plan="monitor"[^>]*>(.*?)</p>', read(name), re.S)
        assert line and " hidden" in line.group(0), name
        for words in ("for the first month", "renews monthly until you cancel online from your dashboard", "end of the month"):
            assert words in line.group(1), (name, words)


def test_index_auto_renew_disclosure_names_both_prices():
    html = read("index.html")
    renew = re.search(r'<p id="monitor-renew"[^>]*>(.*?)</p>', html, re.S).group(1)
    assert TWO_PHASE in renew
    assert 'href="terms.html#monitoring"' in renew
    card = re.search(r'<article class="plan" data-plan-card="monitor".*?</article>', html, re.S).group(0)
    assert 'data-plan-first-price="monitor"' in card and 'data-plan-price="monitor"' in card
    assert "data-if-emails" not in card, "monitoring is only sold with emails on, so its emails aren't conditional"


def test_terms_match_the_decisions():
    terms = re.sub(r"\s+", " ", read("terms.html"))
    assert 'id="monitoring"' in terms
    for words in ("two prices", "first-month price, which includes the full report",
                  "only sold while our order emails are switched on",
                  "we cancel the subscription first", "Find my report can email your links",
                  "History for one-time reports is deleted after 30 days",
                  "always comes from Square", "cancel online at any time", "end of the month you've already paid for"):
        assert words in terms, words


def test_privacy_matches_the_decisions():
    privacy = re.sub(r"\s+", " ", read("privacy.html"))
    for words in ("Monthly Monitoring is only sold while these emails are switched on",
                  "Find my report sends the links to that email address instead of showing them on the page",
                  "are deleted after 30 days", "best-effort basis", "money records always come from Square",
                  "a short note on your Square order", "holds no personal details"):
        assert words in privacy, words


def test_find_page_explains_emailed_links():
    html = read("find.html")
    assert "we email the report and dashboard links to that address instead of showing them here" in html
    js = read("find.js")
    assert "emailed === true" in js and "We've emailed the links to that address if we found any" in js


def test_pageshow_reenables_plan_buttons_in_source():
    js = read("app.js")
    assert 'addEventListener("pageshow"' in js and "persisted" in js


def test_renewal_terms_block_is_shared_and_printable():
    common = read("common.js")
    assert "function renewalTerms" in common and "terms.html#monitoring" in common
    assert 'data.plan === "monitor"' in read("report.js")
    assert "C.renewalTerms(" in read("dashboard.js")
    css = read("style.css")
    assert ".renewal-terms" in css
    assert "no-print" not in re.search(r"function renewalTerms.*?return s;", common, re.S).group(0)


def test_admin_reads_the_real_summary_shape():
    js = read("admin.js")
    for words in ("data.sales", "sales_error", "last_run", "skipped_recent", "emails_skipped", "truncated", "cancelling", "h-summary"):
        assert words in js, words
    assert "last_run_at" not in js, "old guessed shape is gone"


def test_fixtures_are_real_server_shapes():
    import json
    fx = os.path.join(SITE, "tests", "fixtures")
    admin = json.load(open(os.path.join(fx, "admin.json")))
    assert set(admin) >= {"ok", "counters", "monitoring", "sales"} and "orders" not in admin
    assert set(admin["monitoring"]) == {"running", "last_run"}
    assert set(admin["monitoring"]["last_run"]) == {"started", "finished", "counts"}
    dash = json.load(open(os.path.join(fx, "dashboard.json")))
    assert isinstance(dash["history"][0]["at"], (int, float))
    assert {"score_change", "new", "fixed"} <= set(dash["latest_changes"])
    assert "id" not in dash["subscription"], "server.public_subscription leaves out the subscription id"
    plans = json.load(open(os.path.join(fx, "plans.json")))
    mon = [p for p in plans["plans"] if p["id"] == "monitor"][0]
    assert mon["first_cents"] == 2900 and mon["cents"] == 1900


def _keys_cover(fixture, real, where):
    """Every key the fixture has is in the real output, recursively for objects (lists: first item)."""
    if isinstance(fixture, dict):
        assert isinstance(real, dict), where
        for k, v in fixture.items():
            assert k in real, f"{where}.{k} is in the fixture but not in the server's real output"
            _keys_cover(v, real[k], f"{where}.{k}")
    elif isinstance(fixture, list) and fixture and real:
        _keys_cover(fixture[0], real[0], where + "[0]")


def test_fixtures_match_what_the_server_really_returns(tmp_path, monkeypatch):
    """Build /api/dashboard and /api/admin/summary with the server's own code (a purchase scan, then a
    real monitoring run with the real diff), Square faked; the fixtures must have the same shape, and
    the page code must render the real output."""
    import json
    import shutil
    api = os.path.join(SITE, "..", "api")
    monkeypatch.syspath_prepend(api)
    monkeypatch.setenv("APEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("MAIL_BACKEND", raising=False)
    import server
    import store as store_mod
    import monitor
    pay = server.pay
    st = store_mod.Store(str(tmp_path / "data" / "apex.sqlite3"))
    store_mod.set_default(st)
    try:
        sid = "ORDabcdefghijkl"
        site = {"subscription_id": "sub_fixture_1", "customer_id": "CUST1", "order_id": sid, "website": "https://roof.example/",
                "email": "", "state": {"last": None, "month": None, "failing": [], "not_measured": []}}
        paid = {"order_id": sid, "website": "https://roof.example/", "plan": "monitor", "refunded": False, "delivered": True,
                "customer_id": "CUST1", "email": "", "ready_mailed": True,
                "monitor_state": {"last": None, "month": None, "failing": [], "not_measured": []}}
        sub = {"id": "sub_fixture_1", "status": "ACTIVE", "start_date": "2026-10-26", "charged_through_date": "2026-12-26", "canceled_date": None}
        sales = json.load(open(os.path.join(SITE, "tests", "fixtures", "admin.json")))["sales"]
        monkeypatch.setattr(pay, "monitored_sites", lambda *a, **k: [site])
        monkeypatch.setattr(pay, "get_paid_order", lambda *a, **k: dict(paid))
        monkeypatch.setattr(pay, "subscription_for_order", lambda *a, **k: dict(sub))
        monkeypatch.setattr(pay, "sales_summary", lambda *a, **k: dict(sales))
        monkeypatch.setattr(pay, "set_monitor_state", lambda *a, **k: True, raising=False)
        monkeypatch.setattr(pay, "mark_ready_mailed", lambda *a, **k: True, raising=False)

        def result(grade, score, findings):
            return {"ok": True, "site": "roof.example", "grade": grade, "score": score,
                    "report": {"findings": [{"id": i, "title": t, "severity": s} for i, t, s in findings], "sections": [],
                               "not_measured": []}}
        server.record_scan(sid, result("C", 70, [("1.1", "Slow server response", "CRITICAL"), ("2.4", "No tap-to-call button", "CRITICAL")]), "report")
        later = time.time() + 26 * 24 * 3600
        counts = monitor.run_monitoring(lambda mode, url: result("B", 81, [("8.6", "No DMARC email record", "HIGH")]), now=later, store=st)
        assert counts.get("scanned") == 1, counts

        code, dash = server.dashboard(sid)
        assert code == 200, dash
        code, admin = server.admin_summary("30")
        assert code == 200, admin
    finally:
        store_mod.set_default(None)
        st.close()

    fx = os.path.join(SITE, "tests", "fixtures")
    _keys_cover(json.load(open(os.path.join(fx, "dashboard.json"))), dash, "dashboard")
    _keys_cover(json.load(open(os.path.join(fx, "admin.json"))), admin, "admin")
    assert [c["title"] for c in dash["latest_changes"]["new"]] == ["No DMARC email record"]
    assert sorted(c["title"] for c in dash["latest_changes"]["fixed"]) == ["No tap-to-call button", "Slow server response"]
    assert isinstance(dash["history"][0]["at"], float)

    if not shutil.which("node"):
        pytest.skip("node not installed")
    (tmp_path / "dash.json").write_text(json.dumps(dash))
    (tmp_path / "admin.json").write_text(json.dumps(admin))
    p = subprocess.run(["node", os.path.join(SITE, "tests", "contract.mjs"),
                        os.path.join(fx, "mini.json"), os.path.join(fx, "report-pro.json"),
                        "--dashboard=" + str(tmp_path / "dash.json"), "--admin=" + str(tmp_path / "admin.json")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
