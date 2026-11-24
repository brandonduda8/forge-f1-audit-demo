"""f1_trust.py - accessibility and security/email-trust checks (rows 7.x and 8.x).

Called from f1_measure.measure() with the homepage response it already fetched,
so the accessibility rows and the security-header rows cost no extra request.

7.1 image alt text          7.2 form field labels      7.3 heading order
7.4 page language           7.5 text contrast (only where both colours are set
                                inline on the same element; otherwise not measured)
8.1 HSTS header             8.2 other security headers 8.3 certificate expiry (full run)
8.4 exposed /.env and /.git/HEAD (full run; one HEAD each, the files are never downloaded)
8.5 SPF   8.6 DMARC   8.7 DKIM (full run; common selector names only, never a fail)

Every value comes from this run. Anything that could not be looked at is
recorded as not measured with the reason, never as a failure.
"""
import ipaddress
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import dnstxt

# name -> (status, [txt, ...]); tests replace it
TXT_LOOKUP = dnstxt.query_txt

FIELD_SKIP_TYPES = {"hidden", "submit", "button", "reset", "image"}
DKIM_SELECTORS = ("google", "default", "selector1", "selector2", "k1", "s1", "dkim", "mail")
EXPOSED_PATHS = ("/.env", "/.git/HEAD")
OTHER_HEADERS = ("content-security-policy", "x-content-type-options", "x-frame-options", "referrer-policy")
# how long the DNS part may take in total, so a slow resolver can't eat the run's time
DNS_BUDGET_S = 8.0


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- accessibility

def _has_text(v):
    return bool(v and str(v).strip())


HIDDEN_STYLE = re.compile(r"(display\s*:\s*none|visibility\s*:\s*hidden)", re.I)


def _hidden(tag):
    """Not shown to anyone, or hidden from screen readers: tracking pixels, noscript fallbacks, honeypots."""
    for t in [tag] + list(tag.parents):
        if getattr(t, "name", None) is None:
            continue
        if t.name == "noscript" or t.has_attr("hidden") or str(t.get("aria-hidden", "")).lower() == "true":
            return True
        if HIDDEN_STYLE.search(t.get("style") or ""):
            return True
    return False


def _tiny(img):
    return str(img.get("width", "")).strip() in ("0", "1") or str(img.get("height", "")).strip() in ("0", "1")


def alt_text(soup):
    imgs = [i for i in soup.find_all("img")
            if not _hidden(i) and not _tiny(i)
            and str(i.get("role", "")).lower() not in ("presentation", "none")]
    missing = [i.get("src", "")[:100] for i in imgs
               if i.get("alt") is None and not _has_text(i.get("aria-label")) and not _has_text(i.get("aria-labelledby"))]
    return {"n_images": len(imgs), "missing_alt": len(missing), "examples": missing[:3]}


def field_labels(soup):
    label_for = {l.get("for") for l in soup.find_all("label") if l.get("for")}
    fields, unlabeled = 0, []
    for f in soup.find_all(["input", "select", "textarea"]):
        if f.name == "input" and (f.get("type") or "text").lower() in FIELD_SKIP_TYPES:
            continue
        if _hidden(f):
            continue                          # honeypot fields hidden from people
        fields += 1
        ok = ((f.get("id") and f.get("id") in label_for)
              or f.find_parent("label") is not None
              or _has_text(f.get("aria-label"))
              or _has_text(f.get("aria-labelledby"))
              or _has_text(f.get("title")))
        if not ok:
            unlabeled.append(f.get("name") or f.get("id") or f.get("placeholder") or f.name)
    return {"n_fields": fields, "unlabeled": len(unlabeled), "examples": [str(x)[:40] for x in unlabeled[:3]]}


def heading_order(soup):
    levels = [int(h.name[1]) for h in soup.find_all(re.compile(r"^h[1-6]$"))]
    skips = []
    for a, b in zip(levels, levels[1:]):
        if b > a + 1:
            skips.append(f"h{a} to h{b}")
    return {"n_headings": len(levels), "first": (f"h{levels[0]}" if levels else None), "skips": skips[:5],
            "n_skips": len(skips)}


LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$")


def page_lang(soup):
    html = soup.find("html")
    lang = (html.get("lang") or html.get("xml:lang") or "").strip() if html else ""
    return {"lang": lang[:20], "valid": bool(LANG_RE.match(lang))}


# --- contrast (WCAG 2 relative luminance), only for inline colour pairs

NAMED = {"black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0), "green": (0, 128, 0),
         "blue": (0, 0, 255), "yellow": (255, 255, 0), "gray": (128, 128, 128), "grey": (128, 128, 128),
         "silver": (192, 192, 192), "navy": (0, 0, 128), "orange": (255, 165, 0), "purple": (128, 0, 128),
         "maroon": (128, 0, 0), "teal": (0, 128, 128), "olive": (128, 128, 0), "lime": (0, 255, 0),
         "aqua": (0, 255, 255), "cyan": (0, 255, 255), "fuchsia": (255, 0, 255), "magenta": (255, 0, 255)}


def parse_color(v):
    """(r, g, b) for an opaque colour, else None (transparent, variables, gradients...)."""
    v = (v or "").strip().lower().replace("!important", "").strip()
    if v in NAMED:
        return NAMED[v]
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    m = re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*([\d.]+)\s*)?\)", v)
    if m:
        if m.group(4) is not None and float(m.group(4)) < 1:
            return None                      # see-through: depends on what's behind it
        rgb = tuple(int(m.group(i)) for i in (1, 2, 3))
        return rgb if all(0 <= x <= 255 for x in rgb) else None
    return None


def _lum(rgb):
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _style(tag):
    out = {}
    for part in (tag.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _large(tag, st):
    """WCAG large text (3:1 is enough): at least 24px, or 18.66px bold. Default h1-h3 sizes qualify."""
    m = re.fullmatch(r"([\d.]+)\s*(px|pt|em|rem)", (st.get("font-size") or "").strip().lower())
    if m:
        px = float(m.group(1)) * {"px": 1, "pt": 4 / 3, "em": 16, "rem": 16}[m.group(2)]
        bold = (st.get("font-weight") or "").strip().lower() in ("bold", "bolder", "600", "700", "800", "900") \
            or tag.name in ("b", "strong", "h1", "h2", "h3", "h4", "h5", "h6")
        return px >= 24 or (bold and px >= 18.66)
    return tag.name in ("h1", "h2", "h3")


def contrast(soup):
    pairs = []
    for tag in soup.find_all(style=True):
        if _hidden(tag):
            continue
        st = _style(tag)
        fg = parse_color(st.get("color"))
        bg = parse_color(st.get("background-color") or st.get("background") or "")   # images, gradients: None
        if fg is None or bg is None or not tag.get_text(strip=True):
            continue
        ratio = round(contrast_ratio(fg, bg), 2)
        need = 3.0 if _large(tag, st) else 4.5
        pairs.append({"text": tag.get_text(" ", strip=True)[:40], "ratio": ratio, "needs": need})
    low = [p for p in pairs if p["ratio"] < p["needs"]]
    return {"n_pairs": len(pairs), "below_3": sum(1 for p in pairs if p["ratio"] < 3),
            "below_4_5": len(low), "examples": sorted(low, key=lambda p: p["ratio"])[:3]}


# ---------------------------------------------------------------- security

def header_map(resp):
    try:
        return {k.lower(): str(v) for k, v in resp.headers.items()}
    except Exception:
        return {}


def hsts(resp, final):
    h = header_map(resp)
    v = h.get("strict-transport-security", "")
    m = re.search(r"max-age\s*=\s*\"?(\d+)", v, re.I)
    return {"https": final.startswith("https://"), "present": bool(v), "max_age": int(m.group(1)) if m else None}


def other_headers(resp):
    h = header_map(resp)
    present = [k for k in OTHER_HEADERS if h.get(k, "").strip()]
    csp = h.get("content-security-policy", "")
    if "x-frame-options" not in present and re.search(r"frame-ancestors", csp, re.I):
        present.append("x-frame-options")           # CSP frame-ancestors does the same job
    present = [k for k in OTHER_HEADERS if k in present]
    return {"present": present, "missing": [k for k in OTHER_HEADERS if k not in present]}


def cert_expiry(cert):
    if not cert or not cert.get("valid") or not cert.get("expires"):
        return None
    try:
        exp = datetime.fromisoformat(cert["expires"])
        issued = datetime.fromisoformat(cert["issued"]) if cert.get("issued") else None
    except ValueError:
        return None
    days = (exp - datetime.now(timezone.utc)).total_seconds() / 86400
    life = (exp - issued).total_seconds() / 86400 if issued else None
    # share of the certificate's own lifetime still left: short-lived certificates that renew
    # themselves always have few days left, which is healthy
    left_share = round(days / life, 3) if life and life > 0 else None
    return {"expires": cert["expires"], "days_left": int(days), "lifetime_days": (int(life) if life else None),
            "left_share": left_share}


PROBE_TIMEOUT_S = 5


def _probe(requests_mod, url):
    """(status, content type, content length) from one HEAD, or None if it failed."""
    try:
        r = requests_mod.head(url, headers={"User-Agent": "Forge-F1-Audit/1.0"}, timeout=PROBE_TIMEOUT_S,
                              allow_redirects=False)
    except Exception:
        return None
    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    cl = (r.headers.get("Content-Length") or "").strip()
    return r.status_code, ctype, (int(cl) if cl.isdigit() else None)


def _looks_like_file(ans):
    st, ctype, length = ans
    return (st == 200 and ctype and ctype not in ("text/html", "application/xhtml+xml") and length != 0)


def exposed_files(requests_mod, final):
    """One HEAD per path, redirects not followed, body never downloaded, plus one HEAD to a random
    address that can't exist. "Answers as a file" = 200, a declared non-HTML type, not empty, and
    not what the site answers for any address (catch-all servers and gateways answer everything)."""
    out, unknown = [], []
    control = _probe(requests_mod, urljoin(final, "/apex-checkup-" + secrets.token_hex(8) + ".env"))
    if control is None or control[0] in (405, 501):
        return {"checked": [], "exposed": [], "unknown": list(EXPOSED_PATHS)}
    catch_all = _looks_like_file(control)
    for path in EXPOSED_PATHS:
        ans = _probe(requests_mod, urljoin(final, path))
        if ans is None or ans[0] in (405, 501):
            unknown.append(path)                     # failed, or the server won't answer HEAD: can't tell
        elif _looks_like_file(ans):
            if catch_all and ans[1] == control[1]:
                unknown.append(path)                 # answers like any made-up address: can't tell
            else:
                out.append({"path": path, "status": 200, "type": ans[1]})
    return {"checked": [p for p in EXPOSED_PATHS if p not in unknown], "exposed": out, "unknown": unknown}


# ---------------------------------------------------------------- email trust (DNS)

def mail_domain(host):
    """The domain a business sends email from, from the website's host name."""
    h = (host or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(h.strip("[]"))
        return None
    except ValueError:
        pass
    if h.startswith("www."):
        h = h[4:]
    return h if "." in h else None


def _txt(name, deadline):
    if deadline - time.monotonic() < 0.3:
        return "error", []
    try:
        return TXT_LOOKUP(name, deadline=deadline)
    except Exception:
        return "error", []


SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc", "me", "sch", "nhs", "or", "ne", "go"}


def parent_domain(domain):
    """shop.example.com -> example.com; never a public suffix like co.uk (a rough rule, no suffix list)."""
    parts = domain.split(".")
    if len(parts) < 3:
        return None
    parent = parts[1:]
    if len(parent) == 2 and len(parent[1]) == 2 and parent[0] in SECOND_LEVEL:
        return None                                  # smith.co.uk: the parent is co.uk
    return ".".join(parent)


def spf(domain, deadline):
    names = [domain]
    if parent_domain(domain):
        names.append(parent_domain(domain))          # shop.example.com sends as example.com
    for name in names:
        st, recs = _txt(name, deadline)
        if st == "error":
            return None
        found = [r for r in recs if r.lower().startswith("v=spf1")]
        if found:
            pol = found[0].split()[-1].lower() if found[0].split() else ""
            return {"domain": name, "count": len(found), "all": pol if pol.endswith("all") else None}
    return {"domain": domain, "count": 0, "all": None}


def dmarc(domain, deadline):
    names = ["_dmarc." + domain]
    if parent_domain(domain):
        names.append("_dmarc." + parent_domain(domain))
    for i, name in enumerate(names):
        st, recs = _txt(name, deadline)
        if st == "error":
            return None
        found = [r for r in recs if r.replace(" ", "").lower().startswith("v=dmarc1")]
        if found:
            m = re.search(r"(?:^|;)\s*p\s*=\s*(\w+)", found[0], re.I)
            pol = m.group(1).lower() if m else None
            if i > 0:                                # a parent's policy covers subdomains through sp=, if set
                sp = re.search(r"(?:^|;)\s*sp\s*=\s*(\w+)", found[0], re.I)
                pol = sp.group(1).lower() if sp else pol
            return {"domain": name[len("_dmarc."):], "count": len(found), "policy": pol}
    return {"domain": domain, "count": 0, "policy": None}


def dkim(domain, deadline):
    errors = 0
    for sel in DKIM_SELECTORS:
        st, recs = _txt(f"{sel}._domainkey.{domain}", deadline)
        if st == "error":
            errors += 1
            continue
        if any(("p=" in r.replace(" ", "")) for r in recs):
            return {"selector": sel, "found": True}
    return {"found": False, "errors": errors}


# ---------------------------------------------------------------- wiring

def add_checks(put, C, resp, soup, url, final, light, requests_mod):
    """Write rows 7.x and 8.x through f1_measure's put().

    Runs after every graded row, so it must never use up the run's time: the probes only start
    with room for all three to time out, and DNS stops well before the deadline. Anything these
    extra checks record in the guarded client's event log (a timeout, a refused address) is
    removed again afterwards, so it can't mark the graded rows as cut short."""
    events = getattr(requests_mod, "events", None)
    mark = len(events) if isinstance(events, list) else None
    try:
        _add_checks(put, C, resp, soup, url, final, light, requests_mod)
    finally:
        if mark is not None:
            del events[mark:]


def _add_checks(put, C, resp, soup, url, final, light, requests_mod):
    a = alt_text(soup)
    put("7.1", a if a["n_images"] else None, "img tags without an alt attribute (alt=\"\" counts as decorative)",
        "" if a["n_images"] else "no images on the homepage")
    lb = field_labels(soup)
    put("7.2", lb if lb["n_fields"] else None,
        "form fields with no label[for], wrapping label, aria-label, aria-labelledby or title",
        "" if lb["n_fields"] else "no form fields on the homepage")
    put("7.3", heading_order(soup), "h1-h6 in page order; a jump of more than one level is a skip")
    put("7.4", page_lang(soup), "lang attribute on the html tag")
    ct = contrast(soup)
    put("7.5", ct if ct["n_pairs"] else None,
        "WCAG contrast ratio for elements whose text and background colours are both set inline",
        "" if ct["n_pairs"] else "colours come from stylesheets, so contrast needs a browser render")

    put("8.1", hsts(resp, final) if final.startswith("https://") else None,
        "Strict-Transport-Security header on the homepage response",
        "" if final.startswith("https://") else "the site isn't on HTTPS (see 3.7)")
    put("8.2", other_headers(resp), "CSP, X-Content-Type-Options, X-Frame-Options (or CSP frame-ancestors), "
        "Referrer-Policy on the homepage response")
    cert = ((C.get("3.7") or {}).get("value") or {}).get("cert")
    exp = None if light else cert_expiry(cert)
    put("8.3", exp, "certificate notAfter date from a direct TLS connection",
        "" if exp else "the certificate could not be inspected in this run")
    left = getattr(requests_mod, "deadline", None)
    if light:
        put("8.4", None, "one HEAD request each to /.env and /.git/HEAD (files never downloaded)")
    elif left is not None and left - time.monotonic() < 3 * PROBE_TIMEOUT_S + 5:
        put("8.4", None, "one HEAD request each to /.env and /.git/HEAD (files never downloaded)",
            "the run reached its time limit before this check")
    else:
        ex = exposed_files(requests_mod, final)
        put("8.4", ex if ex["checked"] else None,
            "one HEAD request each to /.env and /.git/HEAD, redirects not followed (files never downloaded)",
            "" if ex["checked"] else "the server would not answer these requests")

    domain = mail_domain(urlparse(url).hostname)
    if not domain:
        for cid in ("8.5", "8.6", "8.7"):
            put(cid, None, "DNS TXT lookup", "the site address is not a domain name")
        return
    deadline = time.monotonic() + DNS_BUDGET_S
    if left is not None:
        deadline = min(deadline, left - 5)
    s = spf(domain, deadline)
    put("8.5", s, f"DNS TXT lookup for {domain} (v=spf1)", "" if s else "the DNS lookup did not complete")
    d = dmarc(domain, deadline)
    put("8.6", d, f"DNS TXT lookup for _dmarc.{domain} (v=DMARC1)", "" if d else "the DNS lookup did not complete")
    if light:
        put("8.7", None, "DNS TXT lookups for common DKIM selectors")
        return
    k = dkim(domain, deadline)
    if k["found"]:
        put("8.7", k, f"DNS TXT lookup for {k['selector']}._domainkey.{domain}")
    else:
        put("8.7", None, f"DNS TXT lookups for {len(DKIM_SELECTORS)} common DKIM selector names on {domain}",
            "DKIM keys sit under a name only your email provider knows, and none of the common names answered; "
            "that is not proof DKIM is missing")
