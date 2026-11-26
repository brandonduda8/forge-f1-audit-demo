#!/usr/bin/env python3
"""Write config.js for the Apex Forge Website Checkup static site, and optionally publish it.

Reads these environment variables:
  APEX_API_BASE       required  https URL of the checker API (no trailing slash needed)
  APEX_CHECKOUT       optional  "off" hides buying (default on). Buyers pay on Square's hosted
                                checkout, which the API creates per order; no link is configured here.
  APEX_PRICE          optional  fallback price label for the Website Checkup (default "$29"); the
                                page replaces it with the price from GET /api/plans once that answers
  APEX_SUPPORT_EMAIL  required  address for refund and privacy requests, shown on the legal
                                pages (a buyer must always have a way to reach the seller)
  APEX_EMAILS         optional  "on" when the API sends buyer emails (MAIL_BACKEND=smtp), so the
                                pages may mention them; default "off" (the pages then don't)
  APEX_SITE_ORIGIN    optional  https origin the site is served at, e.g. https://checkup.example.com.
                                With --publish it adds canonical links, sitemap.xml and the robots.txt
                                Sitemap line. Unset: no canonical links and no sitemap.

Usage:
  python3 build_config.py                # validate env vars, write ./config.js
  python3 build_config.py --example      # write config.js with placeholder values
  python3 build_config.py --out PATH     # write config.js somewhere other than ./config.js
  python3 build_config.py --publish DIR  # build the publishable site into DIR: only *.html,
                                         # *.css, *.js, robots.txt (+ sitemap.xml), never tests

Standard library only. Exits non-zero with a clear message on bad input.
"""
import json
import os
import re
import shutil
import sys
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "config.js")

EXAMPLE = {
    "apiBase": "https://your-checker-api.example.invalid",
    "checkout": True,
    "price": "$29",
    "supportEmail": "support@example.invalid",
    "emails": False,
}

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
ORIGIN_RE = re.compile(r"^https://[a-z0-9](?:[a-z0-9.\-]{0,251}[a-z0-9])?(?::\d{1,5})?$")
NOINDEX_RE = re.compile(r'<meta\s+name="robots"\s+content="[^"]*noindex', re.I)

# Published files: these patterns at the top of the site folder, nothing else.
PUBLISH_EXT = (".html", ".css", ".js")
PUBLISH_EXTRA = ("robots.txt",)
NEVER_PUBLISH = {"config.example.js", "config.js"}     # config.js is written fresh into the output


class ConfigError(Exception):
    pass


def _check_https(name, value, required_host=None):
    value = (value or "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. It must be an https:// URL.")
    if any(c.isspace() for c in value) or any(c in value for c in "\"'<>\\`"):
        raise ConfigError(f"{name} contains characters that are not allowed in a URL.")
    parts = urlsplit(value)
    if parts.scheme != "https":
        raise ConfigError(f"{name} must start with https:// (got scheme {parts.scheme or 'none'!r}).")
    if not parts.hostname:
        raise ConfigError(f"{name} has no host name.")
    if parts.username or parts.password:
        raise ConfigError(f"{name} must not contain a user name or password.")
    if required_host and parts.hostname.lower() != required_host:
        raise ConfigError(f"{name} host must be {required_host} (got {parts.hostname!r}).")
    return value


def _on_off(env, name, default):
    flag = (env.get(name) or default).strip().lower()
    if flag not in ("on", "off"):
        raise ConfigError(f'{name} must be "on" or "off".')
    return flag == "on"


def build(env):
    """Return the config dict from an environment mapping, or raise ConfigError."""
    api = _check_https("APEX_API_BASE", env.get("APEX_API_BASE"))
    parts = urlsplit(api)
    if parts.query or parts.fragment:
        raise ConfigError("APEX_API_BASE must not contain a query string or fragment.")
    api = api.rstrip("/")

    checkout = _on_off(env, "APEX_CHECKOUT", "on")
    emails = _on_off(env, "APEX_EMAILS", "off")

    price = (env.get("APEX_PRICE") or "$29").strip()
    if len(price) > 20 or any(c in price for c in "<>\"'`\\"):
        raise ConfigError("APEX_PRICE must be a short label like $29.")

    email = (env.get("APEX_SUPPORT_EMAIL") or "").strip()
    if not email:
        raise ConfigError("APEX_SUPPORT_EMAIL is not set. Buyers need an address for refund requests.")
    if not EMAIL_RE.match(email):
        raise ConfigError("APEX_SUPPORT_EMAIL does not look like an email address.")

    return {"apiBase": api, "checkout": checkout, "price": price, "supportEmail": email, "emails": emails}


def site_origin(env):
    """APEX_SITE_ORIGIN as "https://host[:port]" (lower case, no path), or None when unset."""
    raw = (env.get("APEX_SITE_ORIGIN") or "").strip()
    if not raw:
        return None
    _check_https("APEX_SITE_ORIGIN", raw)
    parts = urlsplit(raw)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigError("APEX_SITE_ORIGIN must be just the origin, like https://checkup.example.com (no path).")
    origin = "https://" + parts.netloc.lower()
    if not ORIGIN_RE.match(origin):
        raise ConfigError("APEX_SITE_ORIGIN has a host name that isn't allowed.")
    return origin


def render(cfg):
    body = json.dumps(cfg, indent=2, ensure_ascii=True)
    # Defensive: never allow a closing script sequence in the output.
    body = body.replace("</", "<\\/")
    return (
        "// Generated by build_config.py. Do not edit by hand; set env vars instead.\n"
        f"window.APEX_CONFIG = {body};\n"
    )


def publishable(src=HERE):
    """Top-level files of the site that are published, sorted. Folders (tests) never are."""
    out = []
    for name in sorted(os.listdir(src)):
        if not os.path.isfile(os.path.join(src, name)) or name in NEVER_PUBLISH:
            continue
        if name.endswith(PUBLISH_EXT) or name in PUBLISH_EXTRA:
            out.append(name)
    return out


def indexable(html):
    """A page search engines may list: not marked noindex."""
    return not NOINDEX_RE.search(html)


def page_url(origin, name):
    return origin + ("/" if name == "index.html" else "/" + name)


def add_canonical(html, url):
    """Insert <link rel="canonical"> before </head> (once). url is built from a checked origin."""
    if 'rel="canonical"' in html or "</head>" not in html:
        return html
    tag = '<link rel="canonical" href="' + xml_escape(url, {'"': "&quot;"}) + '">\n'
    return html.replace("</head>", tag + "</head>", 1)


def sitemap_xml(urls):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    lines += [f"  <url><loc>{xml_escape(u)}</loc></url>" for u in urls]
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def robots_txt(origin):
    return ("# Apex Forge Website Checkup\nUser-agent: *\nAllow: /\n\n"
            f"Sitemap: {origin}/sitemap.xml\n")


def publish(dest, cfg, origin, src=HERE):
    """Copy the publishable files into dest, write config.js, and with an origin add canonical
    links, sitemap.xml and the robots.txt Sitemap line. Returns the sorted list of files written."""
    dest = os.path.abspath(dest)
    if dest == os.path.abspath(src):
        raise ConfigError("--publish needs a separate folder, e.g. public.")
    os.makedirs(dest, exist_ok=True)
    written, urls = [], []
    for name in publishable(src):
        path = os.path.join(src, name)
        if name.endswith(".html"):
            with open(path, encoding="utf-8") as f:
                html = f.read()
            if origin and indexable(html) and name != "404.html":
                html = add_canonical(html, page_url(origin, name))
                urls.append(page_url(origin, name))
            with open(os.path.join(dest, name), "w", encoding="utf-8") as f:
                f.write(html)
        elif name == "robots.txt" and origin:
            with open(os.path.join(dest, name), "w", encoding="utf-8") as f:
                f.write(robots_txt(origin))
        else:
            shutil.copyfile(path, os.path.join(dest, name))
        written.append(name)
    with open(os.path.join(dest, "config.js"), "w", encoding="utf-8") as f:
        f.write(render(cfg))
    written.append("config.js")
    if origin:
        index_first = sorted(urls, key=lambda u: (not u.endswith("/"), u))
        with open(os.path.join(dest, "sitemap.xml"), "w", encoding="utf-8") as f:
            f.write(sitemap_xml(index_first))
        written.append("sitemap.xml")
    return sorted(written)


def main(argv):
    out = DEFAULT_OUT
    example = False
    pub = None
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--example":
            example = True
        elif a == "--out" and args:
            out = args.pop(0)
        elif a == "--publish" and args:
            pub = args.pop(0)
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            print(f"build_config.py: unknown argument {a!r}", file=sys.stderr)
            return 2
    try:
        cfg = dict(EXAMPLE) if example else build(os.environ)
        origin = site_origin(os.environ)
        if pub is not None:
            files = publish(pub, cfg, origin)
            print(f"build_config.py: published {len(files)} files to {pub}"
                  + ("" if origin else " (APEX_SITE_ORIGIN not set: no canonical links or sitemap)"))
            return 0
    except ConfigError as e:
        print(f"build_config.py: {e}", file=sys.stderr)
        print("build_config.py: config.js was NOT written.", file=sys.stderr)
        return 1
    with open(out, "w", encoding="utf-8") as f:
        f.write(render(cfg))
    print(f"build_config.py: wrote {out}" + (" (example placeholders)" if example else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
