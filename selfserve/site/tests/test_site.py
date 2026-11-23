"""Offline checks for the Apex Forge Website Checkup static site (stdlib + pytest)."""
import os
import re
import subprocess
import sys
import tempfile
from html.parser import HTMLParser

import pytest

SITE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_FILES = sorted(f for f in os.listdir(SITE) if f.endswith(".html"))
JS_FILES = sorted(f for f in os.listdir(SITE) if f.endswith(".js") and f not in ("config.js", "config.example.js"))
CSS_FILES = sorted(f for f in os.listdir(SITE) if f.endswith(".css"))

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
EXPECTED_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src https:; form-action 'self' https://buy.stripe.com; base-uri 'none'")
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
    assert hosts <= {"buy.stripe.com"}, f"external hosts in {name}: {hosts}"
    if name.endswith(".html"):
        # buy.stripe.com may only appear inside the CSP meta tag
        no_csp = re.sub(r'<meta http-equiv="Content-Security-Policy"[^>]*>', "", text)
        assert "buy.stripe.com" not in no_csp
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


def test_index_buy_box_note():
    assert "On the checkout page, type the same website address." in read("index.html")


def test_no_secrets():
    pat = re.compile(r"(sk|rk)_(live|test)_[A-Za-z0-9]{8,}|whsec_[A-Za-z0-9]{8,}|-----BEGIN")
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


GOOD = {"APEX_API_BASE": "https://forge-api.onrender.com/", "APEX_PAYMENT_LINK": "https://buy.stripe.com/test_abc123"}


@pytest.mark.parametrize("bad", [
    {},
    {"APEX_API_BASE": "http://forge-api.onrender.com", "APEX_PAYMENT_LINK": GOOD["APEX_PAYMENT_LINK"]},
    {"APEX_API_BASE": "forge-api.onrender.com", "APEX_PAYMENT_LINK": GOOD["APEX_PAYMENT_LINK"]},
    {"APEX_API_BASE": "javascript:alert(1)", "APEX_PAYMENT_LINK": GOOD["APEX_PAYMENT_LINK"]},
    {"APEX_API_BASE": "https://api.example.com/x?y=1", "APEX_PAYMENT_LINK": GOOD["APEX_PAYMENT_LINK"]},
    {"APEX_API_BASE": GOOD["APEX_API_BASE"]},
    {"APEX_API_BASE": GOOD["APEX_API_BASE"], "APEX_PAYMENT_LINK": "http://buy.stripe.com/abc"},
    {"APEX_API_BASE": GOOD["APEX_API_BASE"], "APEX_PAYMENT_LINK": "https://evil.example.com/abc"},
    {"APEX_API_BASE": GOOD["APEX_API_BASE"], "APEX_PAYMENT_LINK": "https://buy.stripe.com.evil.com/abc"},
    {"APEX_API_BASE": GOOD["APEX_API_BASE"], "APEX_PAYMENT_LINK": "https://buy.stripe.com/"},
    {"APEX_API_BASE": "https://user:pw@api.example.com", "APEX_PAYMENT_LINK": GOOD["APEX_PAYMENT_LINK"]},
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
    assert cfg == {"apiBase": "https://forge-api.onrender.com", "paymentLink": "https://buy.stripe.com/test_abc123",
                   "price": "$29", "supportEmail": "help@example.com"}


def test_build_config_custom_price_and_no_email():
    proc, content = run_build({**GOOD, "APEX_PRICE": "$39"})
    assert proc.returncode == 0, proc.stderr
    cfg = parse_config(content)
    assert cfg["price"] == "$39" and cfg["supportEmail"] == ""


def test_build_config_example_mode_needs_no_env():
    proc, content = run_build({}, "--example")
    assert proc.returncode == 0, proc.stderr
    cfg = parse_config(content)
    assert cfg["paymentLink"].startswith("https://buy.stripe.com/")
    assert cfg["apiBase"].startswith("https://")


def test_config_example_js_matches_shape():
    text = read("config.example.js")
    assert "window.APEX_CONFIG = {" in text
    for key in ("apiBase", "paymentLink", "price", "supportEmail"):
        assert f'"{key}"' in text
