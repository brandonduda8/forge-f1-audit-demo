#!/usr/bin/env python3
"""f1_pages.py - the Pro checkup's extra pages (beyond the homepage).

After the homepage has been measured (f1_measure.measure), the Pro checkup looks
at up to 4 more pages on the same site and checks each one for the on-page
basics: title, meta description, one H1, image alt text, page language, mobile
viewport, thin content, noindex, canonical link, and whether the page loads
without an error or a redirect.

* Pages come from the homepage's links (nav and header links first) and the
  site's sitemap, preferring pages that look like about, services, contact,
  pricing, products, menu or locations. Other hosts, files (PDFs, images...),
  anchors, links with a query string, and paths robots.txt disallows for us
  are never fetched.
* Every request goes through the `requests_mod` it is given (the guarded client
  in the self-serve runner), and responses already fetched while measuring the
  homepage are reused from `cache` instead of being fetched again.
* It stops cleanly before the run's request or time budget runs out: a page it
  did not get to is listed with every check "not measured", never failed.
* robots.txt paths are compared in one normalized, percent-encoded form, and a
  redirect is followed one hop at a time: an address robots.txt disallows is not
  requested, and a page that redirects there is "not measured".
* Only 404 and 410 make a page "broken". Any other error (401, 403, 429, 5xx) or
  a bot-check page means the site refused our checker: "not measured".
* A malformed link or sitemap address (e.g. "http://[x/") is skipped, never fatal.

Nothing here touches the grade: these results are reported beside it.
"""
import re
import time
from urllib.parse import quote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

import f1_trust

UA_TOKEN = "forge-f1-audit"         # the product token in f1_measure.UA, for robots.txt groups
MAX_PAGES = 4
MIN_TIME_S = 6.0                     # don't start a page fetch with less time left than this
FETCH_TIMEOUT_S = 15
THIN_WORDS = 150
TITLE_MAX = 65
DESC_MAX = 160
ROBOTS_MAX_BYTES = 500 * 1024
MAX_SITEMAP_LOCS = 2000
MAX_HOPS = 5                         # redirects followed for one extra page, each one a counted request
BROKEN_STATUS = (404, 410)           # the page is gone; any other error status means the site refused us
REDIRECT_STATUS = (301, 302, 303, 307, 308)
UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

CHECKS = [
    ("status", "Page loads without an error or redirect"),
    ("title", "Page title"),
    ("meta_description", "Meta description"),
    ("h1", "One main heading (H1)"),
    ("alt_text", "Image alt text"),
    ("lang", "Page language"),
    ("viewport", "Mobile viewport"),
    ("thin_content", "Enough text on the page"),
    ("noindex", "Visible to search engines (no noindex)"),
    ("canonical", "Canonical link"),
]
LABEL = dict(CHECKS)
CONTENT_IDS = [cid for cid, _l in CHECKS if cid != "status"]

# preferred kinds of page, in order; matched against the words of the URL path
KEYWORDS = [
    ("about", {"about", "about-us", "aboutus", "our-story", "story", "team", "who-we-are"}),
    ("services", {"services", "service", "what-we-do", "solutions"}),
    ("contact", {"contact", "contact-us", "contactus", "get-in-touch", "quote", "estimate"}),
    ("pricing", {"pricing", "prices", "price", "rates", "plans", "packages"}),
    ("products", {"products", "product", "shop", "store", "catalog", "collections"}),
    ("menu", {"menu", "menus", "food", "drinks"}),
    ("locations", {"locations", "location", "find-us", "directions", "service-area", "areas", "areas-we-serve"}),
]
PAGE_EXT = {"", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".shtml", ".cfm"}
SRC_RANK = {"nav": 0, "page": 1, "sitemap": 2}
CHALLENGE_RE = re.compile(r"(just a moment|attention required|access denied|checking your browser|"
                          r"verify you are human|are you a robot|ddos protection|security check|captcha)", re.I)
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)

R_BUDGET = "the run reached its time or request limit before this page was checked"
R_ROBOTS_FINAL = "the page redirects to an address robots.txt asks us not to visit, so it was not checked"
R_BAD_REDIRECT = "the page redirects to an address that is not a valid web address, so it was not followed"
R_HOPS = "the page redirects too many times to follow"


def r_refused(status):
    return f"the site refused our checker (HTTP {status}), so this page was not checked"


R_BOT_CHECK = "the site refused our checker: it showed a bot-check page instead of this page"


def _clean(s, n=120):
    return re.sub(r"\s+", " ", s or "").strip()[:n]


# ---------------------------------------------------------------- robots.txt (RFC 9309)

def parse_robots(text):
    """Allow/Disallow rules that apply to us: our own group if the file has one, else '*'."""
    groups, cur, in_agents = [], None, False
    for raw in (text or "")[:ROBOTS_MAX_BYTES].splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if not in_agents:
                cur = {"agents": [], "rules": []}
                groups.append(cur)
                in_agents = True
            cur["agents"].append(val.lower())
        elif key in ("allow", "disallow"):
            in_agents = False
            if cur is not None and val:
                cur["rules"].append((key == "allow", val))
        else:
            in_agents = False if key != "sitemap" else in_agents
    ours = [g for g in groups if any(a.split("/")[0] == UA_TOKEN for a in g["agents"])]
    star = [g for g in groups if "*" in g["agents"]]
    rules = []
    for g in ours or star:
        rules += g["rules"]
    return rules


def robots_sitemaps(text):
    return [line.split(":", 1)[1].strip() for line in (text or "")[:ROBOTS_MAX_BYTES].splitlines()
            if line.split(":", 1)[0].strip().lower() == "sitemap" and ":" in line]


def norm_path(s):
    """A path in the one form robots.txt matching compares (RFC 9309 2.2.2): characters outside
    ASCII (as UTF-8) and unsafe ASCII percent-encoded, %xx in upper case, and %xx of an unreserved
    character decoded. So /caf\u00e9, /caf%c3%a9 and /caf%C3%A9 are the same path, and so are /%7Eme and /~me."""
    s = quote(s or "", safe="/:@!$&'()*+,;=?#[]%~")
    s = re.sub(r"%(?![0-9A-Fa-f]{2})", "%25", s)

    def fix(m):
        h = m.group(1).upper()
        c = chr(int(h, 16))
        return c if c in UNRESERVED else "%" + h
    return re.sub(r"%([0-9A-Fa-f]{2})", fix, s)


def _rule_re(pattern):
    pattern = norm_path(pattern)
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    return re.compile(".*".join(re.escape(p) for p in body.split("*")) + ("$" if anchored else ""))


def robots_allowed(rules, path):
    """Longest matching rule wins; Allow wins a tie; no matching rule means allowed."""
    path = norm_path(path)
    if path == "/robots.txt":
        return True
    best_len, allow = -1, True
    for is_allow, pat in rules:
        if _rule_re(pat).match(path):
            n = len(norm_path(pat))
            if n > best_len or (n == best_len and is_allow):
                best_len, allow = n, is_allow
    return allow


# ---------------------------------------------------------------- choosing pages

def _origin(u):
    """(scheme, host[:port]) of a URL, or None when it is not a valid URL (e.g. "http://[x/")."""
    try:
        p = urlparse(u)
    except ValueError:
        return None
    return (p.scheme, (p.netloc or "").lower())


def page_key(u):
    """Dedupe key: same path with or without a trailing slash or index.html is one page."""
    path = urlparse(u).path or "/"
    path = re.sub(r"/index\.(html?|php)$", "/", path, flags=re.I)
    return path.rstrip("/") or "/"


def _candidate(href, base, origin):
    """The absolute URL to check for a link, or None if it is not a same-site web page."""
    href = (href or "").strip()
    if not href or href.startswith("#"):
        return None
    try:
        full = urljoin(base, href)
        p = urlparse(full)
    except ValueError:                        # a malformed link ("http://[x/") is skipped, never fatal
        return None
    if p.scheme not in ("http", "https") or (p.scheme, (p.netloc or "").lower()) != origin:
        return None
    if p.query or p.params or p.username or p.password:
        return None                           # query strings: duplicates and session links
    last = (p.path or "/").rsplit("/", 1)[-1]
    ext = ("." + last.rsplit(".", 1)[-1].lower()) if "." in last else ""
    if ext not in PAGE_EXT:
        return None                           # a file, not a page
    return urlunparse((p.scheme, p.netloc.lower(), p.path or "/", "", "", ""))


def _keyword(u):
    words = set(re.split(r"[/._]+", urlparse(u).path.lower()))
    words |= {w for part in list(words) for w in part.split("-")}
    for i, (_name, kws) in enumerate(KEYWORDS):
        if words & kws:
            return i
    return None


def _is_contact(u):
    i = _keyword(u)
    return i is not None and KEYWORDS[i][0] == "contact"


def homepage_links(soup):
    """[(href, source)] in page order; links in nav, header or role=navigation are 'nav'."""
    out = []
    for a in soup.find_all("a", href=True):
        nav = a.find_parent(["nav", "header"]) is not None or \
            a.find_parent(attrs={"role": "navigation"}) is not None
        out.append((a["href"], "nav" if nav else "page"))
    return out


def sitemap_locs(xml_text):
    return LOC_RE.findall(xml_text or "")[:MAX_SITEMAP_LOCS]


def choose_pages(home_url, links, locs, rules=None, max_pages=MAX_PAGES):
    """Pick up to max_pages same-site pages: one of each preferred kind first, then the rest.

    links: [(href, source)] from the homepage; locs: sitemap URLs; rules: parsed robots rules
    (None = robots.txt unknown, so nothing is filtered here and the caller must not fetch)."""
    origin = _origin(home_url)
    home = page_key(home_url)
    cands, order = {}, 0
    for href, src in list(links) + [(u, "sitemap") for u in locs]:
        u = _candidate(href, home_url, origin)
        if not u:
            continue
        k = page_key(u)
        if k == home:
            continue
        if rules is not None and not robots_allowed(rules, urlparse(u).path or "/"):
            continue
        if k in cands:
            c = cands[k]
            c["rank"] = min(c["rank"], SRC_RANK[src])
            continue
        cands[k] = {"url": u, "rank": SRC_RANK[src], "order": order, "kw": _keyword(u),
                    "depth": (urlparse(u).path or "/").strip("/").count("/")}
        order += 1
    pool = sorted(cands.values(), key=lambda c: (c["rank"], c["depth"], c["order"]))
    picked = []
    for i in range(len(KEYWORDS)):
        for c in pool:
            if c["kw"] == i and c not in picked:
                picked.append(c)
                break
        if len(picked) >= max_pages:
            break
    rest = [c for c in pool if c not in picked]
    rest.sort(key=lambda c: (c["kw"] is None, c["kw"] or 0, c["rank"], c["depth"], c["order"]))
    picked += rest[:max(0, max_pages - len(picked))]
    return [c["url"] for c in picked[:max_pages]]


# ---------------------------------------------------------------- checking one page

def _issue(cid, title, severity, detail):
    return {"id": cid, "title": title, "severity": severity, "detail": _clean(detail, 300)}


def _nm(cid, reason):
    return {"id": cid, "label": LABEL[cid], "reason": reason}


def unmeasured_page(url, reason, title=""):
    return {"url": url, "path": urlparse(url).path or "/", "title": title, "issues": [], "passed": 0,
            "not_measured": [_nm(cid, reason) for cid, _l in CHECKS]}


def page_soup(resp):
    """Parse a page. Without a charset in Content-Type, requests decodes HTML as Latin-1, so a
    UTF-8 link like /caf\u00e9 would read as /caf\u00c3\u00a9 (wrong page, wrong robots match); the raw
    bytes let the parser use the page's own <meta charset> instead."""
    ctype = str((getattr(resp, "headers", None) or {}).get("Content-Type", "")).lower()
    raw = getattr(resp, "content", None)
    if isinstance(raw, bytes) and raw and "charset=" not in ctype:
        return BeautifulSoup(raw, "lxml")
    return BeautifulSoup(resp.text or "", "lxml")


def _meta(soup, name):
    for m in soup.find_all("meta"):
        if str(m.get("name", "")).strip().lower() == name:
            return m
    return None


def word_count(soup):
    body = soup.body or soup
    body = BeautifulSoup(str(body), "lxml")
    for t in body.find_all(["script", "style", "noscript", "template", "nav", "header", "footer", "svg"]):
        t.decompose()
    return len(re.findall(r"[^\W_]+", body.get_text(" ", strip=True)))


def check_content(url, resp, soup, home):
    """The nine content checks for one fetched HTML page. Returns (issues, passed, not_measured)."""
    issues, passed, nm = [], 0, []

    def ok():
        nonlocal passed
        passed += 1

    title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "", 300)
    if not title:
        issues.append(_issue("title", "Page has no title", "MAJOR",
                             "no <title> text; Google shows a title it makes up, or the bare address"))
    elif home.get("title") and title.lower() == home["title"].lower():
        issues.append(_issue("title", "Same title as the homepage", "MINOR",
                             f'title "{title[:80]}" is the homepage\'s title, so Google can\'t tell the pages apart'))
    elif len(title) > TITLE_MAX:
        issues.append(_issue("title", "Title too long", "MINOR",
                             f'title is {len(title)} characters (over {TITLE_MAX}); Google cuts it off'))
    else:
        ok()

    md = _meta(soup, "description")
    desc = _clean(md.get("content") if md else "", 400)
    if not desc:
        issues.append(_issue("meta_description", "No meta description", "MINOR",
                             "no meta description; Google picks text from the page for the listing"))
    elif home.get("description") and desc.lower() == home["description"].lower():
        issues.append(_issue("meta_description", "Same meta description as the homepage", "MINOR",
                             "the description is copied from the homepage"))
    elif len(desc) > DESC_MAX:
        issues.append(_issue("meta_description", "Meta description too long", "MINOR",
                             f"{len(desc)} characters (over {DESC_MAX}); Google cuts it off"))
    else:
        ok()

    h1s = soup.find_all("h1")
    if len(h1s) == 1:
        ok()
    elif not h1s:
        issues.append(_issue("h1", "No main heading (H1)", "MAJOR", "the page has no H1 heading"))
    else:
        issues.append(_issue("h1", "More than one main heading (H1)", "MINOR", f"{len(h1s)} H1 headings (bar: exactly 1)"))

    a = f1_trust.alt_text(soup)
    if not a["n_images"]:
        nm.append(_nm("alt_text", "no images on this page"))
    elif a["missing_alt"] == 0:
        ok()
    else:
        sev = "MAJOR" if a["missing_alt"] > 0.2 * a["n_images"] else "MINOR"
        issues.append(_issue("alt_text", "Images missing alt text", sev,
                             f"{a['missing_alt']} of {a['n_images']} images have no alt text"))

    lg = f1_trust.page_lang(soup)
    if lg["valid"]:
        ok()
    else:
        issues.append(_issue("lang", "Page language not set", "MINOR",
                             "no valid lang attribute on the html tag" + (f' (found "{lg["lang"]}")' if lg["lang"] else "")))

    if _meta(soup, "viewport") is not None:
        ok()
    else:
        issues.append(_issue("viewport", "Not set up for phones (no viewport tag)", "MAJOR",
                             "no meta viewport tag, so phones show a shrunken desktop page"))

    if _is_contact(url):
        nm.append(_nm("thin_content", "not applied to contact pages, which are short by design"))
    else:
        n = word_count(soup)
        if n >= THIN_WORDS:
            ok()
        else:
            issues.append(_issue("thin_content", "Thin content", "MINOR",
                                 f"about {n} words of text outside the menu, header and footer (bar: {THIN_WORDS})"))

    robots_meta = " ".join(str(m.get("content", "")) for m in soup.find_all("meta")
                           if str(m.get("name", "")).strip().lower() in ("robots", "googlebot")).lower()
    header = str((getattr(resp, "headers", None) or {}).get("X-Robots-Tag", "")).lower()
    if "noindex" in robots_meta or "noindex" in header or "none" in re.split(r"[\s,]+", robots_meta):
        where = "a robots meta tag" if ("noindex" in robots_meta or "none" in robots_meta) else "the X-Robots-Tag header"
        issues.append(_issue("noindex", "Hidden from search engines (noindex)", "MAJOR",
                             f"{where} tells Google not to list this page; remove it if the page should be found"))
    else:
        ok()

    canon = [l for l in soup.find_all("link", href=True)
             if "canonical" in [str(x).lower() for x in (l.get("rel") or [])]]
    if not canon:
        issues.append(_issue("canonical", "No canonical link", "MINOR",
                             "no rel=canonical link; Google may treat copies of this page (with tracking tags, "
                             "with or without www) as separate pages"))
    else:
        here = resp.url or url
        try:
            target = urljoin(here, canon[0]["href"].strip())
            target_host = urlparse(target).hostname
        except ValueError:
            target = target_host = None
        if len(canon) > 1:
            issues.append(_issue("canonical", "More than one canonical link", "MINOR",
                                 f"{len(canon)} rel=canonical links; Google may ignore them all"))
        elif target is None:
            issues.append(_issue("canonical", "Canonical link is not a valid address", "MINOR",
                                 "the rel=canonical link is not a valid web address, so Google ignores it"))
        elif target_host != urlparse(here).hostname:
            issues.append(_issue("canonical", "Canonical link points to another site", "MAJOR",
                                 f"canonical is {target[:120]}; Google may list that address instead of this page"))
        elif page_key(target) != page_key(here):
            issues.append(_issue("canonical", "Canonical link points to a different page", "MAJOR",
                                 f"canonical is {urlparse(target).path or '/'}; Google may skip this page"))
        else:
            ok()
    return issues, passed, nm, title


def _classify(e):
    s = f"{type(e).__name__} {e}"
    if "BUDGET" in s or "BudgetExceeded" in s:
        return R_BUDGET, True
    if "BLOCKED" in s or "Blocked" in s:
        return "the page redirects to an address that is not a public website, so it was not fetched", False
    if "TooLarge" in s or "larger than" in s:
        return "the page is too large to check", False
    if "TooManyRedirects" in s:
        return "the page redirects too many times to follow", False
    return "the page could not be loaded in this run", False


def check_page(url, resp, home):
    """One page dict from a response (redirects already followed)."""
    page = {"url": url, "path": urlparse(url).path or "/", "title": "", "issues": [], "passed": 0,
            "not_measured": []}
    status = resp.status_code
    final = resp.url or url
    hops = len(getattr(resp, "history", None) or [])
    if status >= 400 and status not in BROKEN_STATUS:
        # 401, 403, 429, 5xx...: the site turned our checker away; that says nothing about the page
        return unmeasured_page(url, r_refused(status))
    if status >= 400:
        page["issues"].append(_issue("status", f"Page is broken (HTTP {status})", "CRITICAL",
                                     f"a link to {page['path']} on your site leads to an error page"))
        page["not_measured"] = [_nm(cid, "the page answered with an error, so its content was not checked")
                                for cid in CONTENT_IDS]
        return page
    off_site = _origin(final) != _origin(url)
    if hops:
        dest = final[:120] if off_site else (urlparse(final).path or "/")
        page["issues"].append(_issue("status", "Page redirects", "MINOR",
                                     f"{page['path']} redirects to {dest} ({hops} hop(s)); "
                                     "link straight to the final address"))
    else:
        page["passed"] += 1
    if off_site:
        page["not_measured"] = [_nm(cid, "the page redirects to another site or address, which this check "
                                         "does not cover")
                                for cid in CONTENT_IDS]
        return page
    ctype = str((getattr(resp, "headers", None) or {}).get("Content-Type", "")).lower()
    if ctype and "html" not in ctype:
        page["not_measured"] = [_nm(cid, f"not a web page ({ctype.split(';')[0][:40]})") for cid in CONTENT_IDS]
        return page
    soup = page_soup(resp)
    raw_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if CHALLENGE_RE.search(raw_title):
        return unmeasured_page(url, R_BOT_CHECK)
    issues, passed, nm, title = check_content(url, resp, soup, home)
    page["issues"] += issues
    page["passed"] += passed
    page["not_measured"] += nm
    page["title"] = _clean(title)
    return page


# ---------------------------------------------------------------- the whole step

def budget_reason(req, need=1):
    """Why a new fetch must not start now, or None."""
    mx, count = getattr(req, "max_requests", None), getattr(req, "count", 0)
    if mx is not None and mx - count < need:
        return R_BUDGET
    dl = getattr(req, "deadline", None)
    if dl is not None and dl - time.monotonic() < MIN_TIME_S:
        return R_BUDGET
    return None


def _timeout(req):
    dl = getattr(req, "deadline", None)
    if dl is None:
        return FETCH_TIMEOUT_S
    return max(1.0, min(FETCH_TIMEOUT_S, dl - time.monotonic() - 3))


def _get(req, url, cache, ua):
    if cache is not None and url in cache:
        return cache[url]
    r = req.get(url, headers={"User-Agent": ua}, timeout=_timeout(req))
    if cache is not None:
        cache[url] = r
    return r


class _Stopped:
    """A redirect we did not follow (to another site or address): enough for check_page to report it."""

    def __init__(self, url, status, history):
        self.url, self.status_code, self.history = url, status, history
        self.headers, self.text = {}, ""


def fetch_page(req, url, cache, ua, rules, origin):
    """GET one extra page, following same-site redirects one counted request at a time and
    checking every address it redirects to against robots.txt before asking for it.
    Returns (response, None, False), or (None, why it is not measured, whether that is the budget)."""
    if cache is not None and url in cache:
        r = cache[url]                       # fetched (redirects followed) while measuring the homepage
        final = r.url or url
        if getattr(r, "history", None) and _origin(final) == origin and \
                not robots_allowed(rules, urlparse(final).path or "/"):
            return None, R_ROBOTS_FINAL, False
        return r, None, False
    history, cur = [], url
    for _hop in range(MAX_HOPS + 1):
        if history:
            why = budget_reason(req)
            if why:
                return None, why, True
        try:
            r = req.get(cur, headers={"User-Agent": ua}, timeout=_timeout(req), allow_redirects=False)
        except ValueError:
            # requests parses Location even when not following it; "http://[x/" raises here
            return None, R_BAD_REDIRECT, False
        loc = str((getattr(r, "headers", None) or {}).get("Location", "")).strip()
        if r.status_code not in REDIRECT_STATUS or not loc:
            r.history = history
            return r, None, False
        history.append(r)
        try:
            nxt = urljoin(cur, loc)
            p = urlparse(nxt)
        except ValueError:
            return None, R_BAD_REDIRECT, False
        if p.scheme not in ("http", "https") or _origin(nxt) != origin:
            return _Stopped(nxt, r.status_code, history), None, False   # not fetched
        if not robots_allowed(rules, p.path or "/"):
            return None, R_ROBOTS_FINAL, False
        cur = urlunparse((p.scheme, p.netloc, p.path or "/", p.params, p.query, ""))
    return None, R_HOPS, False


def new_state():
    return {"selected": [], "pages": [], "note": None}


def finish_unmeasured(state, reason=R_BUDGET):
    """Mark every selected page not done yet as not measured (after an interruption)."""
    done = {p["url"] for p in state["pages"]}
    for u in state["selected"]:
        if u not in done:
            state["pages"].append(unmeasured_page(u, reason))
    return state


def check_pages(req, home_url, home_resp, ua, cache=None, max_pages=MAX_PAGES, state=None):
    """Choose and check up to max_pages extra pages. Fills and returns state:
    {"selected": [url], "pages": [page], "note": str | None}.

    home_url is the address the homepage was finally served from; home_resp its response."""
    st = state if state is not None else new_state()
    soup = page_soup(home_resp)
    md = _meta(soup, "description")
    home = {"title": _clean(soup.title.get_text(" ", strip=True) if soup.title else "", 300),
            "description": _clean(md.get("content") if md else "", 400)}
    links = homepage_links(soup)
    origin = _origin(home_url)

    # robots.txt: 2xx = its rules, 4xx = no rules, anything else = we don't fetch other pages
    robots_url = urljoin(home_url, "/robots.txt")
    rules, robots_text, robots_why = None, "", None
    why = None if (cache and robots_url in cache) else budget_reason(req)
    if why:
        robots_why = why
    else:
        try:
            rr = _get(req, robots_url, cache, ua)
            if 200 <= rr.status_code < 300:
                robots_text = rr.text
                rules = parse_robots(robots_text)
            elif 400 <= rr.status_code < 500:
                rules = []
            else:
                robots_why = (f"robots.txt answered with an error (HTTP {rr.status_code}), "
                              "so other pages were not fetched")
        except Exception as e:     # noqa: BLE001 - every failure becomes "not measured"
            reason, budget = _classify(e)
            robots_why = reason if budget else "robots.txt could not be read, so other pages were not fetched"

    # the sitemap: /sitemap.xml, or the first same-site Sitemap: line in robots.txt
    locs = []
    if rules is not None:
        tried = []
        sm_urls = [urljoin(home_url, "/sitemap.xml")] + [u for u in robots_sitemaps(robots_text)
                                                          if _origin(u) == origin]
        for sm in sm_urls:
            if locs or len(tried) >= 2:
                break
            if sm in tried:
                continue
            tried.append(sm)
            if not robots_allowed(rules, urlparse(sm).path or "/"):
                continue
            if not (cache and sm in cache) and budget_reason(req, need=2):
                break
            try:
                r = _get(req, sm, cache, ua)
            except Exception:      # noqa: BLE001 - no sitemap: homepage links still count
                continue
            if r.status_code != 200:
                continue
            body = r.text[:2 * 1024 * 1024]
            if re.search(r"<sitemapindex", body, re.I):
                child = [u for u in sitemap_locs(body) if _origin(u) == origin
                         and robots_allowed(rules, urlparse(u).path or "/")][:1]
                if child and ((cache and child[0] in cache) or not budget_reason(req, need=2)):
                    try:
                        cr = _get(req, child[0], cache, ua)
                        if cr.status_code == 200:
                            locs = sitemap_locs(cr.text[:2 * 1024 * 1024])
                    except Exception:  # noqa: BLE001
                        pass
            else:
                locs = sitemap_locs(body)

    st["selected"] = choose_pages(home_url, links, locs, rules, max_pages)
    if robots_why:
        # without robots.txt we may not fetch; list what we would have checked
        st["note"] = robots_why
        st["pages"] = [unmeasured_page(u, robots_why) for u in st["selected"]]
        return st
    if not st["selected"]:
        st["note"] = "no other pages on the same site were found in the homepage links or the sitemap"
        return st

    stopped = None
    for u in st["selected"]:
        if stopped is None and not (cache and u in cache):
            stopped = budget_reason(req)
        if stopped:
            st["pages"].append(unmeasured_page(u, stopped))
            continue
        try:
            r, why, budget = fetch_page(req, u, cache, ua, rules, origin)
            if r is None:
                if budget:
                    stopped = why
                st["pages"].append(unmeasured_page(u, why))
            else:
                st["pages"].append(check_page(u, r, home))
        except Exception as e:     # noqa: BLE001 - every failure becomes "not measured"
            reason, budget = _classify(e)
            if budget:
                stopped = reason
            st["pages"].append(unmeasured_page(u, reason))
    return st
