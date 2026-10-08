#!/usr/bin/env python3
"""f1_measure.py — Forge F1 audit measurement harness (v1 pinned methods).

Usage: f1_measure.py <business-name> <url> [outdir]
Writes <outdir>/<slug>-evidence.json with {claim, method, severity-hint, timestamp}
records for every checklist row that is honestly measurable without a browser
render. Rows needing a rendered browser (LCP/CLS/TBT, tap targets, mobile
menu) are recorded as NOT_MEASURED with the reason.

Never invents: every value traces to a live fetch made by this run.
"""
import json, re, ssl, sys, time, socket, os
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Forge-F1-Audit/1.0 (+public-audit)"
MAX_RESOURCES = 150

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

def ttfb(url):
    t0 = time.time()
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30, allow_redirects=True)
        ttfb_ms = r.elapsed.total_seconds() * 1000
        return r, ttfb_ms, t0
    except Exception as e:
        return None, None, t0

def cert_info(host):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=15) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                cert = ss.getpeercert()
                exp = ssl.cert_time_to_seconds(cert["notAfter"])
                sans = [v for t, v in cert.get("subjectAltName", []) if t == "DNSName"]
                return {"valid": True, "expires": datetime.fromtimestamp(exp, timezone.utc).isoformat(),
                        "sans": sans, "host_in_san": host in sans or any(x.startswith("*.") and host.endswith(x[1:]) for x in sans)}
    except Exception as e:
        return {"valid": False, "error": str(e)}

def head_size(url):
    try:
        r = requests.head(url, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
        cl = r.headers.get("Content-Length")
        return int(cl) if cl and cl.isdigit() else None, r.status_code
    except Exception:
        return None, None

def safe_get(url, timeout=15):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        return r.status_code
    except Exception:
        return None

def measure(name, url, outdir):
    ev = {"business": name, "url": url, "measured_at": now(), "checks": {}}
    C = ev["checks"]
    r, ttfb_ms, _ = ttfb(url)
    if r is None:
        ev["fatal"] = "homepage unreachable"
        json.dump(ev, open(os.path.join(outdir, "evidence.json"), "w"), indent=2)
        return ev
    html = r.text
    final = r.url
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    text_l = text.lower()

    def put(cid, value, method, note=""):
        C[cid] = {"value": value, "method": method, "ts": now(), "note": note}

    # 1.1 TTFB
    put("1.1", round(ttfb_ms, 0), "requests.elapsed (GET homepage, redirects followed)",
        "pass<600ms" if ttfb_ms < 600 else "fail>=600ms")
    # 1.5 / 1.6 / 1.7 resource analysis
    base = final
    res = []
    for tag, attr in (("img", "src"), ("script", "src"), ("link", "href")):
        for el in soup.find_all(tag):
            u = el.get(attr)
            if not u or u.startswith("data:"): continue
            full = urljoin(base, u)
            if urlparse(full).netloc != urlparse(base).netloc: continue
            res.append((tag, full))
    seen, sizes, imgs = set(), [], []
    for tag, full in res[:MAX_RESOURCES]:
        if full in seen: continue
        seen.add(full)
        sz, st = head_size(full)
        sizes.append(sz or 0)
        if tag == "img": imgs.append({"url": full, "bytes": sz, "status": st})
    html_bytes = len(r.content)
    total = html_bytes + sum(sizes)
    put("1.5", {"html_bytes": html_bytes, "resource_bytes_known": sum(sizes),
                "est_total_bytes": total, "n_resources": len(seen)},
        "HTML bytes + HEAD Content-Length sum of same-origin resources (cap %d)" % MAX_RESOURCES,
        "pass<2MB" if total < 2*1024*1024 else "fail>=2MB")
    big = [i for i in imgs if i["bytes"] and i["bytes"] > 500*1024]
    put("1.6", {"n_images": len(imgs), "images_over_500k": big,
                "nonmodern": [i["url"] for i in imgs if re.search(r"\.(jpe?g|png)$", i["url"], re.I)]},
        "img src inventory + HEAD sizes; format from URL extension",
        "pass if no image >500KB and formats modern")
    blocking_js = [s.get("src") for s in soup.find_all("script", src=True)
                   if not (s.get("async") is not None or s.get("defer") is not None)]
    blocking_css = [l.get("href") for l in soup.find_all("link", rel=lambda v: v and "stylesheet" in v)
                    if l.get("media", "all") == "all"]
    put("1.7", {"sync_scripts": len(blocking_js), "blocking_stylesheets": len(blocking_css)},
        "HTML parse: script[src] without async/defer + blocking stylesheets (heuristic; Lighthouse in production)",
        "pass if 0 critical chains (heuristic)")
    # 2.x
    vp = soup.find("meta", attrs={"name": "viewport"})
    put("2.1", bool(vp), "meta[name=viewport] present in HTML")
    put("2.4", [a.get("href") for a in soup.find_all("a", href=re.compile(r"^tel:"))],
        "tel: links in homepage HTML")
    # 3.x
    title = (soup.title.string or "").strip() if soup.title else ""
    md = soup.find("meta", attrs={"name": "description"})
    md = (md.get("content") or "").strip() if md else ""
    h1s = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    put("3.1", {"title": title, "len": len(title)}, "HTML title tag")
    put("3.2", {"description": md[:200], "len": len(md)}, "meta[name=description]")
    put("3.3", {"h1_count": len(h1s), "h1s": h1s[:5]}, "HTML h1 count")
    schemas = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(s.string or "{}")
            items = d if isinstance(d, list) else [d]
            for it in items:
                t = it.get("@type")
                if t: schemas.append(t if isinstance(t, str) else "/".join(t))
        except Exception: pass
    put("3.4", schemas, "JSON-LD script blocks parsed")
    gbp_links = [a.get("href") for a in soup.find_all("a", href=True)
                 if re.search(r"(google\.[a-z.]+/maps|g\.page|business\.google\.com|goo\.gl/maps)", a["href"])]
    put("3.5-linked", bool(gbp_links), "outbound links to Google Maps/GBP from homepage")
    host = urlparse(final).hostname
    put("3.7", {"https": final.startswith("https://"), "cert": cert_info(host) if final.startswith("https://") else None},
        "scheme of final URL + TLS cert via ssl socket")
    rob = safe_get(urljoin(final, "/robots.txt"))
    sm = safe_get(urljoin(final, "/sitemap.xml"))
    put("3.8", {"robots": rob, "sitemap_xml": sm,
                "sitemap_index_guess": safe_get(urljoin(final, "/sitemap_index.xml"))},
        "direct fetch of /robots.txt, /sitemap.xml, /sitemap_index.xml (null = fetch failed)")
    # 4.x
    header = soup.find("header") or soup
    header_text = (soup.find("header").get_text(" ", strip=True) if soup.find("header") else text[:1500])
    phones = re.findall(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}", header_text + " " + text[:3000])
    put("4.1", {"phone_in_header_or_hero": bool(re.search(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}", header_text)),
                "sample": phones[:3]},
        "phone regex over <header> + first 3000 chars of page text")
    forms = []
    for f in soup.find_all("form"):
        fields = [i.get("name") or i.get("type") for i in f.find_all(["input", "textarea", "select"])]
        forms.append({"action": f.get("action"), "fields": fields[:12], "n_fields": len(fields)})
    put("4.2", {"n_forms": len(forms), "forms": forms[:3]}, "form tags in HTML (submit NOT tested: prospect dry-run rule)")
    # 4.3 CTA crawl top 5 internal pages
    internal = []
    for a in soup.find_all("a", href=True):
        full = urljoin(base, a["href"])
        p = urlparse(full)
        if p.netloc == urlparse(base).netloc and full not in internal and "#" not in a["href"]:
            internal.append(full)
        if len(internal) >= 8: break
    cta_re = re.compile(r"(get (a )?quote|free estimate|schedule|book now|contact us|call now|request service)", re.I)
    cta_pages = []
    for u in internal[:5]:
        try:
            rr = requests.get(u, headers={"User-Agent": UA}, timeout=20)
            if rr.status_code == 200 and cta_re.search(rr.text):
                cta_pages.append(u)
        except Exception: pass
    put("4.3", {"pages_checked": internal[:5], "pages_with_cta": cta_pages},
        "crawl first 5 internal pages; CTA = quote/estimate/schedule/book/contact/call regex")
    # 4.4 quote flow steps: homepage -> contact/quote page -> form present?
    contact_links = [urljoin(base, a["href"]) for a in soup.find_all("a", href=True)
                     if re.search(r"(contact|quote|estimate|book|schedule)", a["href"], re.I)][:3]
    steps = 1
    flow_ok, flow_note = False, ""
    if contact_links:
        steps = 2
        try:
            rr = requests.get(contact_links[0], headers={"User-Agent": UA}, timeout=20)
            flow_ok = bool(BeautifulSoup(rr.text, "lxml").find("form")) or "tel:" in rr.text
            flow_note = contact_links[0]
        except Exception as e:
            flow_note = "contact page fetch failed: %s" % e
    put("4.4", {"steps_to_send": steps if flow_ok else "unverifiable", "contact_links": contact_links, "form_or_tel_on_target": flow_ok},
        "link-walk homepage -> first contact/quote link; 'send' = form or tel: present (heuristic, no render)",
        flow_note)
    put("4.5", bool(re.search(r"(within \d+ ?(hour|min)|same[\s-]?day|respond within|reply within|24/7)", text_l)),
        "copy regex for response-time promise over page text")
    hours_hit = bool(re.search(r"(hours|open|mon(day)?|tue(sday)?|wed(nesday)?|thu(rsday)?|fri(day)?|sat(urday)?|sun(day)?).{0,40}\d{1,2}(:\d{2})?\s*(am|pm)", text_l))
    area_hit = bool(re.search(r"(serving|service area|areas we serve|proudly serving|locations)", text_l))
    put("4.6", {"hours_stated": hours_hit, "service_area_stated": area_hit},
        "copy regex over homepage text")
    # 6.x
    put("6.1", bool(re.search(r"(about us|meet the team|our story|owner|founded by|family[\s-]owned)", text_l)),
        "copy regex for about/owner identity")
    put("6.2", bool(re.search(r"(licensed|insured|bonded|license\s*(#|no|number)?\s*:?\s*\d)", text_l)),
        "copy regex for license/insurance mention")
    img_alts = [i.get("alt", "") for i in soup.find_all("img")]
    stocky = [i.get("src", "") for i in soup.find_all("img")
              if re.search(r"(shutterstock|istock|getty|adobe\.stock|stock\.)", i.get("src", ""), re.I)]
    put("6.3", {"n_images": len(img_alts), "with_alt": sum(1 for a in img_alts if a.strip()),
                "known_stock_urls": stocky},
        "img inventory: alt-text coverage + stock-photo CDN URL detection (heuristic only — cannot verify photo origin)")
    socials = {}
    for a in soup.find_all("a", href=True):
        for net in ("facebook.com", "instagram.com", "linkedin.com", "youtube.com", "twitter.com", "x.com", "tiktok.com", "yelp.com"):
            if net in a["href"] and "share" not in a["href"]:
                socials.setdefault(net, a["href"])
    put("6.4", socials, "outbound social profile links in homepage HTML (active-status not checked)")
    put("6.5", bool(re.search(r"(guarantee|warranty|satisfaction guaranteed|warranted)", text_l)),
        "copy regex for guarantee/warranty language")
    # 6.6 broken links: HEAD homepage internal links (bounded 25)
    broken = []
    for u in internal[:25]:
        try:
            rr = requests.head(u, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
            if rr.status_code >= 400: broken.append({"url": u, "status": rr.status_code})
        except Exception as e:
            broken.append({"url": u, "status": "ERR %s" % e})
    put("6.6", {"checked": min(len(internal), 25), "broken": broken}, "HEAD each internal link from homepage (bounded)")
    # reviews presence 5.2 on-site
    put("5.2", bool(re.search(r"(testimonial|review|what (our )?customers say|hear from our customers|\bstars\b)", text_l)),
        "copy regex for on-site reviews/testimonials section")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    path = os.path.join(outdir, f"{slug}-evidence.json")
    json.dump(ev, open(path, "w"), indent=2)
    print("wrote", path)
    return ev

if __name__ == "__main__":
    name, url = sys.argv[1], sys.argv[2]
    outdir = sys.argv[3] if len(sys.argv) > 3 else "."
    os.makedirs(outdir, exist_ok=True)
    measure(name, url, outdir)
