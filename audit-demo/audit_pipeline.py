#!/usr/bin/env python3
"""audit_pipeline.py — Forge F1 one-click audit glue.

Usage: audit_pipeline.py "<business name>" <url> <jobdir>
  1. Runs tools/f1_measure.py (real measurement, subprocess) -> evidence.json
  2. Applies the PINNED SCORING_RUBRIC thresholds to the evidence -> scoring.json
  3. Writes report.md from the SAMPLE_REPORT template (measured values only)
  4. Runs tools/render_pdf.py -> audit.pdf

Nothing here re-implements measurement. All scoring thresholds come from
AUDIT_CHECKLIST.md / SCORING_RUBRIC.md. Checks that cannot be honestly
measured in a no-browser demo environment are excluded from denominators
and listed under "Not measured" — never invented, never estimated.
"""
import json, os, re, sys, subprocess, shutil
from datetime import datetime, timezone
from urllib.parse import urlparse

VENV_PY = os.environ.get("AUDIT_PYBIN",
                         os.path.expanduser("~/workspace/vendor/genesis-tools-venv/bin/python"))
F1 = os.environ.get("F1_DIR",
                    os.path.expanduser("~/workspace/genesis-os/war-room/empire/forge/f1-audit-product"))

# ---- pinned thresholds & fix copy (from AUDIT_CHECKLIST.md v1) ----
# severity: CRITICAL (losing jobs now) / MAJOR (measurable loss) / MINOR (polish)
FIXES = {
 "1.1": ("CRITICAL", "Server answers too slowly", "Needs a developer, ~2-4 hours",
         "Enable page caching (server-level), use a CDN for static assets, and check for slow plugins/database queries."),
 "1.5": ("MAJOR", "Homepage ships too much weight", "Needs a developer, ~2-4 hours",
         "Compress images, remove unused scripts and stylesheets, and set an explicit performance budget (~2MB max)."),
 "1.6": ("MINOR", "Oversized or legacy-format images", "DIY with image tools, ~1 hour",
         "Re-export any image over 500KB (compress, resize to display size) and prefer webp/avif formats."),
 "1.7": ("MAJOR", "Render-blocking scripts and stylesheets", "Needs a developer, ~1-2 hours",
         "Add defer/async to non-critical scripts and inline critical CSS so the page paints before everything downloads."),
 "2.1": ("CRITICAL", "No mobile viewport configured", "Needs a developer, ~30 minutes",
         "Add the standard viewport meta tag to the page head so phones render the layout correctly."),
 "2.4": ("CRITICAL", "No tap-to-call button", "DIY, ~15 minutes",
         "Wrap the phone number in a tel: link (ideally a sticky call button on mobile) so a caller reaches you in one tap."),
 "3.1": ("MINOR", "Title tag too long or missing", "DIY, ~5 minutes",
         "Rewrite the title to 30-60 characters with the main keyword up front."),
 "3.2": ("MINOR", "Meta description missing or off-length", "DIY, ~10 minutes",
         "Write a 120-160 character meta description with a call to action."),
 "3.3": ("MAJOR", "Heading structure off (not exactly one H1)", "DIY, ~15 minutes",
         "Give the page exactly one H1 containing the main keyword; demote the rest to H2/H3."),
 "3.4": ("MAJOR", "No LocalBusiness schema markup", "Needs a developer, ~1 hour",
         "Add a JSON-LD LocalBusiness block (name, address, phone, hours, geo) to the homepage."),
 "3.5-linked": ("MINOR", "Google Business Profile not linked", "DIY, ~10 minutes",
         "Link the Google Business Profile / Maps listing from the homepage (footer or contact section)."),
 "3.7": ("MAJOR", "Certificate check inconclusive", "Needs a developer, ~1 hour",
         "Have a developer verify the origin TLS certificate chain directly (validity window + SAN covers the host) and fix any mixed-content references."),
 "3.8": ("MINOR", "Missing sitemap.xml or robots.txt", "DIY with a developer, ~30 minutes",
         "Publish /sitemap.xml (and submit it in Search Console) and a correct /robots.txt."),
 "4.1": ("CRITICAL", "Phone number not visible in the header", "DIY, ~15 minutes",
         "Put the phone number in the header/hero so it's visible without scrolling."),
 "4.2": ("CRITICAL", "No contact/quote form found", "DIY in most page builders, ~1 hour",
         "Add a short quote form (name, phone, address) — name/phone/message fields minimum."),
 "4.3": ("MAJOR", "Weak call-to-action coverage", "DIY, ~1 hour",
         "Make sure every main page has a visible quote/estimate/schedule/book/contact/call button."),
 "4.4": ("MAJOR", "Quote path is long or broken", "Needs a developer, ~2 hours",
         "Shorten the path from homepage to a working form or call button to at most 3 steps."),
 "4.5": ("MINOR", "No response-time promise stated", "DIY, ~10 minutes",
         "Add one line near the phone number stating the response promise (only promise what can be kept)."),
 "4.6": ("MINOR", "Hours or service area not stated", "DIY, ~15 minutes",
         "Publish opening hours and the service-area list on the site."),
 "5.2": ("MINOR", "No reviews/testimonials on the site", "DIY, ~30 minutes",
         "Add a reviews/testimonials section to the homepage with real customer reviews."),
 "6.1": ("MINOR", "No owner/about identity", "DIY, ~30 minutes",
         "Add an about section naming a real owner and a real photo."),
 "6.2": ("MAJOR", "License/insurance not stated", "DIY, ~10 minutes",
         "State the license number and insured/bonded status where buyers look (footer or about)."),
 "6.3": ("MINOR", "Possible stock-photo usage", "DIY, ~1 hour",
         "Replace stock photos with real photos of the crew, trucks, and jobs (note: this check is heuristic only)."),
 "6.4": ("MINOR", "No social profiles linked", "DIY, ~15 minutes",
         "Link the business's Facebook/Instagram/LinkedIn profiles from the site."),
 "6.5": ("MINOR", "No guarantee/warranty stated", "DIY, ~10 minutes",
         "State the guarantee or warranty explicitly (only what is true)."),
 "6.6": ("MAJOR", "Broken links found", "DIY or needs a developer, ~1 hour",
         "Fix or remove every broken internal link found."),
 # 7.x accessibility (basic WCAG checks on the homepage HTML)
 "7.1": ("MAJOR", "Images missing alt text", "DIY in most site builders, ~30 minutes",
         "Give every meaningful image a short alt description in the image settings; mark purely decorative images with an empty alt."),
 "7.2": ("MAJOR", "Form fields without labels", "DIY or needs a developer, ~30 minutes",
         "Give every form field a visible label tied to it (most form builders have a label setting); placeholder text alone is not a label."),
 "7.3": ("MINOR", "Headings skip levels", "DIY, ~20 minutes",
         "Use headings in order (H1, then H2, then H3) so no level is skipped; change the heading level, not just the font size."),
 "7.4": ("MINOR", "Page language not declared", "Needs a developer or a site setting, ~5 minutes",
         "Set the site language (for example lang=\"en\" on the html tag; most builders have a language setting)."),
 "7.5": ("MAJOR", "Low-contrast text", "DIY, ~30 minutes",
         "Darken the text or lighten the background until the contrast ratio is at least 4.5 to 1 (3 to 1 for large headings)."),
 # 8.x security and email trust
 "8.1": ("MINOR", "HTTPS not enforced with HSTS", "Needs a developer or your host, ~15 minutes",
         "Send a Strict-Transport-Security header (max-age of at least 6 months) so browsers always use HTTPS."),
 "8.2": ("MINOR", "Missing security headers", "Needs a developer or your host, ~30 minutes",
         "Add the missing headers listed above (most hosts and CDNs have a setting or plugin for them); test the site after adding a Content-Security-Policy."),
 "8.3": ("MAJOR", "Certificate is close to expiring and hasn't renewed", "Needs your host, ~15 minutes",
         "Check that automatic renewal is working with your host and renew the certificate now if it isn't."),
 "8.4": ("CRITICAL", "Private files may be public", "Needs a developer today, ~30 minutes",
         "Open the addresses listed above in a browser to confirm. If the file opens, block web access to it (or delete it from the web server), then change any passwords or keys it holds."),
 "8.5": ("MAJOR", "No SPF email record", "DIY at your domain registrar, ~15 minutes",
         "Add an SPF TXT record listing the services that send your email (your email provider gives the exact text); end it with ~all or -all."),
 "8.6": ("MAJOR", "No DMARC email record", "DIY at your domain registrar, ~15 minutes",
         "Add a TXT record at _dmarc with at least v=DMARC1; p=none and a reporting address, then move to p=quarantine once mail checks out."),
 "8.7": ("MINOR", "DKIM signing not found", "DIY with your email provider, ~15 minutes",
         "Turn on DKIM signing in your email provider and publish the record it gives you."),
}

CATEGORIES = [
    ("Speed", 25, ["1.1", "1.5", "1.6", "1.7"]),
    ("Mobile", 20, ["2.1", "2.4"]),
    ("SEO basics", 20, ["3.1", "3.2", "3.3", "3.4", "3.5-linked", "3.7", "3.8"]),
    ("Contact path", 15, ["4.1", "4.2", "4.3", "4.4", "4.5", "4.6"]),
    ("Reviews", 10, ["5.2"]),
    ("Trust signals", 10, ["6.1", "6.2", "6.3", "6.4", "6.5", "6.6"]),
]

# Shown with their own score next to the grade. Not part of the 0-100 grade, so
# grades stay comparable with earlier checkups.
SECTIONS = [
    ("Accessibility", ["7.1", "7.2", "7.3", "7.4", "7.5"]),
    ("Security and email trust", ["8.1", "8.2", "8.3", "8.4", "8.5", "8.6", "8.7"]),
]
SECTION_LINES = {
    "Accessibility": "Can people using screen readers, keyboards or weaker eyesight use the site?",
    "Security and email trust": "Is the site locked down, and can others send email pretending to be you?",
}
SECTION_IDS = {cid for _n, ids in SECTIONS for cid in ids}

NOT_MEASURED = [
    ("1.2", "Largest Contentful Paint (LCP)", "needs a browser render (pinned: Lighthouse CLI)"),
    ("1.3", "Cumulative Layout Shift (CLS)", "needs a browser render (pinned: Lighthouse CLI)"),
    ("1.4", "Total Blocking Time (TBT)", "needs a browser render (pinned: Lighthouse CLI)"),
    ("2.2", "Tap-target sizing on mobile", "needs Playwright mobile emulation (pinned)"),
    ("2.3", "Text readable without zoom", "needs Playwright mobile emulation (pinned)"),
    ("2.5", "Mobile menu usability", "needs Playwright mobile emulation (pinned)"),
    ("2.6", "Form usability on mobile render", "needs Playwright mobile emulation (pinned)"),
    ("3.5b", "Google Business Profile listing exists (rating/count)", "needs Google Business Profile data; only the link to it is checked here"),
    ("3.6", "NAP consistency across directories", "needs pairwise source comparison (BBB/Yelp/site); not automated in demo mode"),
    ("5.1", "Google review count and rating", "needs Google Business Profile data, which this check does not read"),
    ("5.3", "Review recency (90 days)", "not verifiable from public snippets"),
    ("5.4", "Owner responds to reviews", "not verifiable from public snippets"),
]

# schema.org LocalBusiness and its common subtypes (finding 1: trades, restaurants,
# salons and dentists use these specific types, and they ARE LocalBusiness).
LOCAL_BUSINESS_TYPES = {t.lower() for t in """
LocalBusiness AnimalShelter ArchiveOrganization AutomotiveBusiness AutoBodyShop AutoDealer
AutoPartsStore AutoRental AutoRepair AutoWash GasStation MotorcycleDealer MotorcycleRepair
ChildCare Dentist DryCleaningOrLaundry EmergencyService FireStation Hospital PoliceStation
EmploymentAgency EntertainmentBusiness AmusementPark ArtGallery Casino ComedyClub MovieTheater
NightClub FinancialService AccountingService AutomatedTeller BankOrCreditUnion InsuranceAgency
FoodEstablishment Bakery BarOrPub Brewery CafeOrCoffeeShop Distillery FastFoodRestaurant
IceCreamShop Restaurant Winery GovernmentOffice PostOffice HealthAndBeautyBusiness BeautySalon
DaySpa HairSalon HealthClub NailSalon TattooParlor HomeAndConstructionBusiness Electrician
GeneralContractor HVACBusiness HousePainter Locksmith MovingCompany Plumber RoofingContractor
InternetCafe LegalService Attorney Notary Library LodgingBusiness BedAndBreakfast Campground
Hostel Hotel Motel Resort MedicalBusiness CommunityHealth Dermatology DietNutrition Gynecologic
MedicalClinic Optician Optometric Pediatric Physician Physiotherapy PlasticSurgery Podiatric
PrimaryCare Psychiatric PublicHealth ProfessionalService RadioStation RealEstateAgent
RecyclingCenter SelfStorage ShoppingCenter SportsActivityLocation BowlingAlley ExerciseGym
GolfCourse PublicSwimmingPool SkiResort SportsClub StadiumOrArena TennisComplex Store
BikeStore BookStore ClothingStore ComputerStore ConvenienceStore DepartmentStore
ElectronicsStore Florist FurnitureStore GardenStore GroceryStore HardwareStore HobbyShop
HomeGoodsStore JewelryStore LiquorStore MensClothingStore MobilePhoneStore MovieRentalStore
MusicStore OfficeEquipmentStore OutletStore PawnShop PetStore ShoeStore SportingGoodsStore
TireShop ToyStore WholesaleStore TelevisionStation TouristInformationCenter TravelAgency
""".split()}


def is_local_business_type(t):
    """True when a JSON-LD @type (str, or "A/B" joined list) is LocalBusiness or a subtype."""
    parts = [x.strip().rsplit("/", 1)[-1].rsplit(":", 1)[-1] for x in str(t).split("/")]
    return any(x.lower() in LOCAL_BUSINESS_TYPES for x in parts if x)


def fix_for(cid, sc):
    """(severity, title, effort, fix) for a failed check. Finding 4: a 3.7 score of 0
    means no HTTPS at all, which is not the same problem as an inconclusive cert check."""
    if cid == "3.7" and sc == 0:
        return ("MAJOR", "Site not served over HTTPS", "Needs a developer or your host, ~1 hour",
                "Turn on HTTPS (most hosts include a free certificate) and redirect every http:// address to https://.")
    if cid == "7.3" and sc == 0:
        return ("MINOR", "No headings on the homepage", "DIY, ~20 minutes",
                "Mark the page's main title as H1 and each section title as H2, using the heading setting rather than bold text.")
    if cid == "8.3" and sc == 0:
        return ("CRITICAL", "Certificate expires within 7 days and hasn't renewed", "Needs your host, today",
                "Renew the certificate now and turn on automatic renewal with your host; once it lapses, browsers block the site.")
    if cid == "8.5" and sc == 0.5:
        return ("MAJOR", "SPF email record is too loose or broken", "DIY at your domain registrar, ~15 minutes",
                "Keep exactly one SPF record, list only the services that send your email, and end it with ~all or -all (never +all).")
    if cid == "8.6" and sc == 0.5:
        return ("MINOR", "DMARC is set to monitor only", "DIY at your domain registrar, ~10 minutes",
                "Once your reports show your real mail passing, change p=none to p=quarantine so look-alike mail gets filtered.")
    return FIXES[cid]


def score(ev):
    """Apply pinned thresholds to evidence. Returns {cat: {...}, checks: {...}}."""
    C = ev["checks"]
    res = {}
    notes = []
    def put(cid, val, why):
        res[cid] = {"score": val, "why": why}
    for cid in list(C.keys()):
        if cid not in FIXES:
            continue
        c = C[cid]
        try:
            v = c["value"]
        except (KeyError, TypeError):
            res[cid] = {"score": None, "why": "evidence missing for this check (excluded)"}
            continue
        if (isinstance(c, dict) and c.get("status") == "NOT_MEASURED") or v is None:
            res[cid] = {"score": None, "why": "not measured in this run (excluded): " + str(c.get("note", ""))}
            continue
        if cid == "1.1":
            ms = v
            put(cid, 1 if ms < 600 else (0 if ms >= 1500 else 0.5), f"TTFB {ms:.0f}ms (bar <600ms pass, >=1.5s fail)")
        elif cid == "1.5":
            b = v["est_total_bytes"]
            put(cid, 1 if b < 2*1024*1024 else (0 if b >= 4*1024*1024 else 0.5),
                f"est total {b/1024/1024:.2f}MB (HTML {v['html_bytes']/1024:.0f}KB + {v['n_resources']} resources)")
        elif cid == "1.6":
            big = v.get("images_over_500k") or []
            n = v.get("n_images", 0)
            if n == 0:
                res[cid] = {"score": None, "why": "no images found to check (excluded)"}
            else:
                put(cid, 1 if not big else 0,
                    f"{n} images inventoried; {len(big)} over 500KB" +
                    ("" if big else "") )
                if big: notes.append(f"1.6 oversized: {[i['url'][:80] for i in big][:3]}")
        elif cid == "1.7":
            n = v["sync_scripts"] + v["blocking_stylesheets"]
            put(cid, 1 if n == 0 else 0, f"{v['sync_scripts']} sync scripts + {v['blocking_stylesheets']} blocking stylesheets (heuristic; a full browser test can confirm)")
        elif cid == "2.1":
            put(cid, 1 if v else 0, "viewport meta tag " + ("present" if v else "missing"))
        elif cid == "2.4":
            put(cid, 1 if v else 0, f"{len(v)} tel: link(s) found" if v else "no tel: links on homepage")
        elif cid == "3.1":
            t, l = v["title"], v["len"]
            if 30 <= l <= 60: put(cid, 1, f'title "{t[:70]}" ({l} chars, within 30-60)')
            elif l == 0: put(cid, 0, "no title tag")
            elif l > 0 and (t.lower() in ("home", "home page", "welcome")): put(cid, 0.5, f'generic title "{t}" ({l} chars)')
            else: put(cid, 0.5, f'title "{t[:70]}" ({l} chars, outside 30-60)')
        elif cid == "3.2":
            d, l = v["description"], v["len"]
            if 120 <= l <= 160: put(cid, 1, f"meta description {l} chars (within 120-160)")
            elif l == 0: put(cid, 0, "no meta description")
            else: put(cid, 0.5, f"meta description {l} chars (outside 120-160)")
        elif cid == "3.3":
            n = v["h1_count"]
            put(cid, 1 if n == 1 else 0, f"{n} H1 element(s) (bar: exactly 1)")
        elif cid == "3.4":
            types = v or []
            lb = [t for t in types if is_local_business_type(t)]
            if lb: put(cid, 1, f"JSON-LD @type: {', '.join(types)}")
            elif types: put(cid, 0.5, f"JSON-LD present but no LocalBusiness family: {', '.join(types)}")
            else: put(cid, 0, "no JSON-LD schema found")
        elif cid == "3.5-linked":
            put(cid, 1 if v else 0, "Google Maps/GBP link " + ("present" if v else "not found") + " (link half only)")
        elif cid == "3.7":
            if v.get("https") and v.get("cert") and v["cert"].get("valid"):
                put(cid, 1, "HTTPS + valid cert")
            elif v.get("https"):
                put(cid, 0.5, "HTTPS served, but the certificate could not be inspected directly in this run")
            else:
                put(cid, 0, "site not served over HTTPS")
        elif cid == "3.8":
            r, s = v["robots"], v["sitemap_xml"]
            ok = (r == 200) + (s == 200)
            put(cid, 1 if ok == 2 else (0.5 if ok == 1 else 0),
                f"robots.txt={r}, sitemap.xml={s}")
        elif cid == "4.1":
            put(cid, 1 if v["phone_in_header_or_hero"] else 0,
                "phone number " + ("visible in header/hero" if v["phone_in_header_or_hero"] else "not found in header/hero"))
        elif cid == "4.2":
            n = v["n_forms"]
            if n == 0: put(cid, 0, "no <form> elements on homepage (forms are never submitted)")
            else:
                best = 0
                for fm in v["forms"]:
                    got = sum(1 for f in fm["fields"]
                              if f and re.search(r"(name|phone|tel|message)", str(f), re.I))
                    best = max(best, got)
                put(cid, 1 if best >= 3 else 0.5,
                    f"{n} form(s); best form has {best} name/phone/message-ish fields (submits never tested)")
        elif cid == "4.3":
            pages = v["pages_checked"]; cta = v["pages_with_cta"]
            if not pages: res[cid] = {"score": None, "why": "no internal pages found to crawl (excluded)"}
            else:
                ratio = len(cta) / len(pages)
                put(cid, 1 if ratio >= 0.8 else (0.5 if ratio > 0 else 0),
                    f"CTA on {len(cta)}/{len(pages)} internal pages checked")
        elif cid == "4.4":
            if not v["contact_links"]:
                put(cid, 0, "no contact/quote/estimate link found on homepage (no quote path)")
            elif v["form_or_tel_on_target"]:
                put(cid, 1, f"homepage -> {v['contact_links'][0][:80]}: form or tel: present (2-step path)")
            else:
                put(cid, 0, f"contact page {v['contact_links'][0][:80] if v['contact_links'] else ''}: no form or tel: found (heuristic)")
        elif cid == "4.5":
            put(cid, 1 if v else 0, "response-time promise " + ("stated" if v else "not found in page copy"))
        elif cid == "4.6":
            h, a = v["hours_stated"], v["service_area_stated"]
            put(cid, 1 if (h and a) else (0.5 if (h or a) else 0),
                f"hours stated: {h}; service area stated: {a} (regex heuristic)")
        elif cid == "5.2":
            put(cid, 1 if v else 0, "on-site reviews/testimonials " + ("present" if v else "not found"))
        elif cid == "6.1":
            put(cid, 1 if v else 0, "about/owner identity copy " + ("present" if v else "not found"))
        elif cid == "6.2":
            put(cid, 1 if v else 0, "license/insurance mention " + ("present" if v else "not found"))
        elif cid == "6.3":
            stock = v.get("known_stock_urls") or []
            put(cid, 1 if not stock else 0,
                f"{v['n_images']} images, {v['with_alt']} with alt text; {len(stock)} stock-CDN URL hits (heuristic only — cannot verify photo origin)")
        elif cid == "6.4":
            put(cid, 1 if v else 0, f"social profiles linked: {', '.join(v.keys())}" if v else "no social profile links found")
        elif cid == "6.5":
            put(cid, 1 if v else 0, "guarantee/warranty language " + ("present" if v else "not found"))
        elif cid == "7.1":
            n, miss = v["n_images"], v["missing_alt"]
            put(cid, 1 if miss == 0 else (0.5 if miss <= 0.2 * n else 0), f"{miss} of {n} images lack alt text")
        elif cid == "7.2":
            n, un = v["n_fields"], v["unlabeled"]
            ex = f" (e.g. {', '.join(v['examples'])})" if un and v.get("examples") else ""
            put(cid, 1 if un == 0 else (0.5 if un * 2 <= n else 0), f"{un} of {n} form fields lack a label{ex}")
        elif cid == "7.3":
            if not v["n_headings"]:
                put(cid, 0, "no headings on the homepage")
            elif v["n_skips"]:
                put(cid, 0.5, f"{v['n_headings']} headings; level skipped {v['n_skips']} time(s), e.g. {v['skips'][0]}")
            else:
                put(cid, 1, f"{v['n_headings']} headings, no skipped levels")
        elif cid == "7.4":
            put(cid, 1 if v["valid"] else 0, f'page language "{v["lang"]}"' if v["valid"] else
                ("no page language declared" if not v["lang"] else f'page language "{v["lang"]}" is not a valid code'))
        elif cid == "7.5":
            n, b3, b45 = v["n_pairs"], v["below_3"], v["below_4_5"]
            worst = f"; lowest {v['examples'][0]['ratio']}:1" if v.get("examples") else ""
            put(cid, 0 if b3 else (0.5 if b45 else 1),
                f"{n} text/background pair(s) set inline; {b45} below the WCAG minimum for their size{worst} (colours from stylesheets not checked)")
        elif cid == "8.1":
            if not v["present"]:
                put(cid, 0, "no Strict-Transport-Security header")
            elif not v["max_age"]:
                put(cid, 0, "Strict-Transport-Security max-age is 0 or missing, which switches it off")
            elif (v["max_age"] or 0) < 15552000:
                put(cid, 0.5, f"Strict-Transport-Security max-age {v['max_age']} (under 6 months)")
            else:
                put(cid, 1, f"Strict-Transport-Security max-age {v['max_age']}")
        elif cid == "8.2":
            n = len(v["present"])
            put(cid, 1 if n == 4 else (0.5 if n >= 2 else 0),
                f"{n} of 4 security headers present" + (f"; missing: {', '.join(v['missing'])}" if v["missing"] else ""))
        elif cid == "8.3":
            d, share, life = v["days_left"], v.get("left_share"), v.get("lifetime_days")
            if share is None:                    # no issue date: assume a common 90-day certificate
                share = d / 90
            # a quarter of its own life left means automatic renewal should already have happened
            late = share < 0.25 and d < 30
            put(cid, 0 if (late and d < 7) else (0.5 if late else 1),
                f"certificate expires in {d} days ({v['expires'][:10]})" + (f", of a {life}-day lifetime" if life else ""))
        elif cid == "8.4":
            ex = v["exposed"]
            put(cid, 0 if ex else 1, ("answered like a real file: " + ", ".join(f"{e['path']} ({e['type']})" for e in ex)
                                      + " (contents not downloaded, so not confirmed)") if ex else
                f"{', '.join(v['checked'])} not publicly served")
        elif cid == "8.5":
            if not v["count"]:
                put(cid, 0, f"no SPF record on {v['domain']}")
            elif v["count"] > 1:
                put(cid, 0.5, f"{v['count']} SPF records on {v['domain']} (only one is allowed)")
            elif v["all"] in ("+all", "all"):
                put(cid, 0.5, f"SPF on {v['domain']} ends in {v['all']}, which lets anyone send as you")
            elif v["all"] == "?all":
                put(cid, 0.5, f"SPF on {v['domain']} ends in ?all, which asks inboxes to treat unlisted senders as neutral")
            else:
                put(cid, 1, f"SPF record on {v['domain']}" + (f" ending {v['all']}" if v["all"] else ""))
        elif cid == "8.6":
            if not v["count"]:
                put(cid, 0, f"no DMARC record on {v['domain']}")
            elif v["policy"] in ("quarantine", "reject"):
                put(cid, 1, f"DMARC on {v['domain']} with policy {v['policy']}")
            else:
                put(cid, 0.5, f"DMARC on {v['domain']} with policy {v['policy'] or 'missing'} (monitor only)")
        elif cid == "8.7":
            put(cid, 1, f"DKIM key found at selector {v['selector']}")
        elif cid == "6.6":
            b = v["broken"]
            put(cid, 1 if not b else 0,
                f"{v['checked']} internal links HEAD-checked; {len(b)} broken" + (f" ({b[0]['url'][:70]})" if b else ""))
    cats = {}
    for name, weight, ids in CATEGORIES:
        vals = [res[i]["score"] for i in ids if i in res and res[i]["score"] is not None]
        excluded = [i for i in ids if i not in res or (i in res and res[i]["score"] is None)]
        # A category with no measured check gets no score (None), not a 0: a 0
        # would be an invented failure. The total is re-weighted over the
        # categories that were actually measured.
        s = round((sum(vals) / len(vals)) * 100) if vals else None
        cats[name] = {"score": s, "weight": weight, "checks": {i: res[i] for i in ids if i in res},
                      "excluded": excluded}
    sections = {}
    for name, ids in SECTIONS:
        vals = [res[i]["score"] for i in ids if i in res and res[i]["score"] is not None]
        sections[name] = {"score": round((sum(vals) / len(vals)) * 100) if vals else None,
                          "checks": {i: res[i] for i in ids if i in res}}
    measured = [c for c in cats.values() if c["score"] is not None]
    wsum = sum(c["weight"] for c in measured)
    total = round(sum(c["score"] * c["weight"] for c in measured) / wsum) if wsum else 0
    grade = "A" if total >= 90 else "B" if total >= 75 else "C" if total >= 60 else "D" if total >= 40 else "F"
    return {"categories": cats, "sections": sections, "total": total, "grade": grade, "check_scores": res}

def fmt_bytes(b):
    if b < 1024: return f"{b}B"
    if b < 1024*1024: return f"{b/1024:.0f}KB"
    return f"{b/1024/1024:.2f}MB"

def kb(n):
    return f"{n/1024:.0f}KB" if n < 1024*1024 else f"{n/1024/1024:.2f}MB"

CAT_LINES = {
    "Speed": "Slow pages lose impatient visitors, most of all on phones.",
    "Mobile": "Many customers find and call local businesses from a phone.",
    "SEO basics": "If they can't be found, nothing else matters.",
    "Contact path": "A found, fast site that can't take a call still loses.",
    "Reviews": "Trust at the decision moment.",
    "Trust signals": "Tiebreaker that compounds everything else.",
}

# Plain-English impact per check (finding 7). Explanatory only: no statistics, no invented numbers.
IMPACT = {
    "1.1": "Before anything appears, your server takes this long to answer. On a phone signal, a slow start is when people hit Back and try the next result.",
    "1.5": "Everything on the page has to download before it is usable. Heavy pages are slow on mobile data.",
    "1.6": "Oversized photos are often the biggest drag on load time, and among the easiest things to fix.",
    "1.7": "These files must finish downloading before the page shows anything, so visitors look at a blank screen for longer.",
    "2.1": "Without this one line of code, phones can show a shrunken desktop page that people have to pinch and zoom to read.",
    "2.4": "If your number is not a tap-to-call link, a phone visitor has to copy it by hand, and some won't bother.",
    "3.1": "The title is the headline of your Google listing. A missing or badly sized one makes the listing less likely to get the click.",
    "3.2": "This is the short blurb under your Google listing. When it is missing or off-length, Google picks text for you.",
    "3.3": "Google uses the main heading to understand what the page is about. None, or several, muddies that signal.",
    "3.4": "This hidden code hands Google your business name, phone, address and hours in a format it reads directly for local results.",
    "3.5-linked": "Linking your Google Business Profile helps visitors find your reviews and directions.",
    "3.7": "Browsers label sites without HTTPS as \"Not secure\", which puts off people about to type their phone number or address.",
    "3.8": "These two files help Google find every page. Without them, inner pages can go unnoticed in search.",
    "4.1": "People decide fast. If the number isn't at the top of the page, some leave before they find it.",
    "4.2": "Some people would rather send a message than call, especially after hours. Without a form, those leads go elsewhere.",
    "4.3": "Each main page is a chance to ask for the call or the booking. Pages without a clear next step let visitors drift off.",
    "4.4": "Every extra step between your homepage and a way to reach you loses some of the people who started.",
    "4.5": "Saying how fast you reply makes it easier for someone to pick you over a business that doesn't say.",
    "4.6": "Visitors want to know if you're open and if you serve their area before they reach out.",
    "5.2": "Reviews on your own site reassure visitors at the moment they are deciding.",
    "6.1": "A real name and face behind the business builds trust with first-time visitors.",
    "6.2": "Customers look for license and insurance details before they let someone into their home.",
    "6.3": "Stock photos make a business look like every other one; real photos show the actual work.",
    "6.4": "Social links let visitors see recent work and that the business is active.",
    "6.5": "A stated guarantee lowers the risk of trying you for the first time.",
    "6.6": "Broken links make a site look abandoned and stop visitors who click them.",
    "7.1": "Screen readers read alt text aloud; an image without it is skipped or read as a file name, and Google uses it to understand images too.",
    "7.2": "A field without a label is announced as just \"edit text\", so screen-reader users can't tell what to type and may give up on the form.",
    "7.3": "Screen-reader users jump between headings to find their way; skipped levels make the page look like it has missing sections.",
    "7.4": "Screen readers pick their voice and pronunciation from the page language; without it, the page can be read out wrong.",
    "7.5": "Pale text on a pale background is hard to read for many people, and on phones outdoors.",
    "8.1": "Without this header, a visitor's first visit can be quietly switched to an insecure connection on public Wi-Fi.",
    "8.2": "These headers stop common tricks such as loading your site inside someone else's page or running injected scripts.",
    "8.3": "When the certificate lapses, browsers show a full-page warning and most visitors leave.",
    "8.4": "Files like these often hold passwords and keys. If anyone can download them, someone can take over the site or its accounts.",
    "8.5": "SPF tells mail servers who may send email as your domain. Without it, scam emails can pose as you and your real email is more likely to land in spam.",
    "8.6": "DMARC tells inboxes what to do with email that fails SPF/DKIM. Without it, look-alike email pretending to be you gets delivered.",
    "8.7": "DKIM signs your email so inboxes can tell it really came from you.",
}


# Neutral names for checks. Used for "What passed" and "Not measured", where the
# failure titles in FIXES would read as a problem the site does not have.
LABELS = {
    "1.1": "Server response time (time to first byte)",
    "1.5": "Homepage total download size",
    "1.6": "Image sizes and formats",
    "1.7": "Render-blocking scripts and stylesheets",
    "2.1": "Mobile viewport setting",
    "2.4": "Tap-to-call phone link",
    "3.1": "Page title",
    "3.2": "Meta description",
    "3.3": "Main heading (H1) structure",
    "3.4": "LocalBusiness schema markup",
    "3.5-linked": "Link to Google Business Profile",
    "3.7": "HTTPS and certificate",
    "3.8": "robots.txt and sitemap.xml",
    "4.1": "Phone number in header",
    "4.2": "Contact or quote form",
    "4.3": "Call-to-action coverage",
    "4.4": "Path from homepage to a quote",
    "4.5": "Response-time promise",
    "4.6": "Hours and service area",
    "5.2": "On-site reviews",
    "6.1": "Owner / about identity",
    "6.2": "License and insurance statement",
    "6.3": "Real vs stock photos",
    "6.4": "Social profile links",
    "6.5": "Guarantee or warranty",
    "6.6": "Broken links",
    "7.1": "Image alt text",
    "7.2": "Form field labels",
    "7.3": "Heading order",
    "7.4": "Page language",
    "7.5": "Text contrast",
    "8.1": "HTTPS enforced (HSTS)",
    "8.2": "Security headers",
    "8.3": "Certificate expiry",
    "8.4": "Private files not public (.env, .git)",
    "8.5": "SPF email record",
    "8.6": "DMARC email record",
    "8.7": "DKIM email signing",
}

SELFSERVE_CLOSING = [
    "## WHAT TO DO NEXT",
    "",
    "**Work down the list.** Fix the findings in order: the top of the list costs you the most. Most items marked DIY take under an hour in a site builder.",
    "",
    "**Hand it to your web person.** Every finding names what was measured and how, so a developer can check it and fix it without guessing.",
    "",
    "**Re-check for free.** After you make changes, run the free check on the Apex Forge site again to see your new grade.",
    "",
    "---",
    "",
    "*This report is automated information about your public website, not professional advice. It makes no promise about search rankings or sales.*",
]

PAGE_SEV_RANK = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}


def _md_text(s, n=120):
    """Text from a website, made safe for one Markdown line (no markup, no table breaks)."""
    s = re.sub(r"[*`\[\]<>\\]", "", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:n]


def pages_section(pages, note=None):
    """'PAGES CHECKED' lines for the Pro checkup: the extra pages, not part of the grade."""
    L = []
    A = L.append
    A("## PAGES CHECKED (shown separately, not part of the grade)")
    A("")
    if not pages:
        A("No other pages were checked" + (f": {_md_text(note, 200)}." if note else "."))
        A("")
        return L
    A(f"Besides the homepage, {len(pages)} more page(s) of your site were checked for the on-page basics: "
      "title, meta description, one main heading, image alt text, page language, mobile viewport, enough text, "
      "noindex, canonical link, and whether the page loads without an error or a redirect.")
    A("")
    for i, p in enumerate(pages, 1):
        nm = p.get("not_measured") or []
        issues = sorted(p.get("issues") or [], key=lambda x: PAGE_SEV_RANK.get(x.get("severity"), 3))
        A(f"### Page {i}: {_md_text(p.get('path') or '/', 100) or '/'}")
        title = _md_text(p.get("title"))
        title_nm = any(n.get("id") == "title" for n in nm)
        A(f"- **Title:** {title if title else ('(not checked)' if title_nm else '(none found)')}")
        if p.get("passed", 0) == 0 and not issues and nm and len({n.get('reason') for n in nm}) == 1:
            A(f"- **Not measured:** {_md_text(nm[0].get('reason'), 200)}. Not counted as a problem.")
            A("")
            continue
        A(f"- **Result:** {p.get('passed', 0)} passed, {len(issues)} problem(s), {len(nm)} not measured")
        for it in issues:
            A(f"- **[{it.get('severity')}] {_md_text(it.get('title'))}** -- {_md_text(it.get('detail'), 300)}")
        by_reason = {}
        for n in nm:
            by_reason.setdefault(n.get("reason"), []).append(_md_text(n.get("label")))
        for reason, labels in by_reason.items():
            A(f"- Not measured: {', '.join(labels)} -- {_md_text(reason, 200)}. Not counted as a problem.")
        A("")
    return L


def build_report(name, url, ev, sc, date_str, domain, variant="snapshot", pages=None, pages_note=None):
    checks = sc["check_scores"]
    sev_rank = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}

    def ranked(items):
        return sorted(items, key=lambda x: (sev_rank[fix_for(x[0], x[1]["score"])[0]], x[1]["score"], x[0]))
    all_fails = [(cid, d) for cid, d in checks.items() if d["score"] is not None and d["score"] < 1]
    fails = [f for f in all_fails if f[0] not in SECTION_IDS]       # graded checks only
    fails_sorted = ranked(fails)
    passes = [cid for cid, d in checks.items() if d["score"] == 1]
    sections = sc.get("sections") or {}

    def finding(i, cid, d):
        sev, title, effort, fix = fix_for(cid, d["score"])
        A(f"### {i}. {title} -- {sev}")
        A(f"- **Measured:** {d['why']} (method: {ev['checks'][cid]['method']}; {date_str})")
        A(f"- **Why it costs you:** {IMPACT.get(cid, 'This check is below the audit bar.')}")
        A(f"- **Fix:** {fix}")
        A(f"- **Effort:** {effort}")
        A("")

    L = []
    A = L.append
    A(f"# WEBSITE AUDIT -- Apex Forge")
    who = domain if name == domain else f"{name} | {domain}"
    A(f"**Prepared for: {who} | Audited {date_str}**")
    A(f"*Measured, not asserted. Every number below traces to a live measurement taken on the audit date.*")
    A("")
    A("---")
    A("")
    grade_name = {"A": "Excellent", "B": "Good", "C": "Needs work", "D": "Leaking badly", "F": "Emergency"}[sc["grade"]]
    A(f"## YOUR SCORE: {sc['total']}/100 -- Grade {sc['grade']} ({grade_name})")
    A("")
    A("| Category | Score | Weight | What it means |")
    A("|---|---|---|---|")
    for cname, _w, _ids in CATEGORIES:
        cs = sc['categories'][cname]['score']
        A(f"| {cname} | {'not measured' if cs is None else str(cs) + '/100'} | {sc['categories'][cname]['weight']} | {CAT_LINES[cname]} |")
    for sname, _ids in SECTIONS:
        if sname in sections and sections[sname]["checks"]:
            ss = sections[sname]["score"]
            A(f"| {sname} | {'not measured' if ss is None else str(ss) + '/100'} | not in grade | {SECTION_LINES[sname]} |")
    A("")
    # plain english
    worst = sorted([c for c in CATEGORIES if sc["categories"][c[0]]["score"] is not None],
                   key=lambda c: sc["categories"][c[0]]["score"])[:2]
    worst_txt = "; ".join(f"{c[0]} scored {sc['categories'][c[0]]['score']}/100" for c in worst)
    A("**In plain English:** " +
      f"This site scored {sc['total']}/100 (Grade {sc['grade']}). " +
      f"The biggest leaks are {worst_txt}. " +
      ("The fastest wins are below -- every one is measured, nothing guessed." if fails else "The fundamentals check out -- see the passes below."))
    A("")
    A("---")
    A("")
    if fails:
        A("## THE 3 THINGS COSTING YOU THE MOST")
        for i, (cid, d) in enumerate(fails_sorted[:3], 1):
            sev, title, effort, _fix = fix_for(cid, d["score"])
            A(f"{i}. **[{sev}] {title}** -- {d['why']} on {date_str}.")
        A("")
        A("---")
        A("")
        A("## FULL FINDINGS (ranked by severity -- biggest leaks first)")
        A("")
        for i, (cid, d) in enumerate(fails_sorted, 1):
            finding(i, cid, d)
    else:
        A("## NO FINDINGS BELOW THE BAR")
        A("Every measured check in the grade passed. Keep the fundamentals maintained." + ("" if variant == "selfserve" else " The Audit + Fix Plan adds a side-by-side with two local competitors."))
        A("")
    for sname, ids in SECTIONS:
        if sname not in sections or not sections[sname]["checks"]:
            continue                       # evidence from before these rows existed
        sfails = ranked([f for f in all_fails if f[0] in ids])
        ss = sections[sname]["score"]
        A("---")
        A("")
        A(f"## {sname.upper()}: {'not measured' if ss is None else str(ss) + '/100'} (shown separately, not part of the grade)")
        A("")
        if sfails:
            for i, (cid, d) in enumerate(sfails, 1):
                finding(i, cid, d)
        else:
            A("Nothing below the bar in what could be measured here." if ss is not None else
              "Nothing in this section could be measured on this run; see Not measured below.")
            A("")
    if passes:
        A("**What passed:**")
        for cid in passes:
            A(f"- {cid} {LABELS.get(cid, cid)}: {checks[cid]['why']}")
        A("")
    A("**Not measured:**")
    for cid, d in sorted(checks.items(), key=lambda x: x[0]):
        if d["score"] is None:
            A(f"- {cid} {LABELS.get(cid, cid)} -- {d['why']}. Excluded from the score, not estimated.")
    for cid, label, why in NOT_MEASURED:
        A(f"- {cid} {label} -- {why}. Excluded from the score, not estimated.")
    A("")
    A("---")
    A("")
    if pages is not None:                  # the Pro checkup's extra pages
        L.extend(pages_section(pages, pages_note))
        A("---")
        A("")
    if variant == "selfserve":
        L.extend(SELFSERVE_CLOSING)
        return "\n".join(L)
    A("## WHAT HAPPENS NEXT")
    A("")
    A("**Fix it yourself.** Everything above is yours to keep. The ranked list is the roadmap.")
    A("")
    A("**Audit + Walkthrough ($150).** This audit plus a check of your Google Business Profile and reviews, and a 30-minute recorded video walking through your fixes in order.")
    A("")
    A("**Audit + Fix Plan ($300).** Everything in the Walkthrough, plus a side-by-side with two local competitors measured the same way, a step-by-step fix plan, and a re-check 30 days later.")
    A("")
    A("If you already bought this $75 Snapshot Audit, it counts toward either upgrade.")
    A("")
    A("---")
    A("")
    A("*Questions? Reply to this email -- it reaches Brandon directly.*")
    A(f"*Evidence pack: `{ev.get('evidence_file','')}` (all measurements, methods, timestamps).*")
    return "\n".join(L)

def run_pipeline(name, url, jobdir):
    os.makedirs(jobdir, exist_ok=True)
    log = lambda m: print(f"[pipeline] {m}", flush=True)
    # 1. measure
    log("running f1_measure.py ...")
    r = subprocess.run([VENV_PY, os.path.join(F1, "tools", "f1_measure.py"), name, url, jobdir],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return {"ok": False, "error": "measurement failed: " + (r.stderr or r.stdout)[-500:]}
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    evpath = os.path.join(jobdir, f"{slug}-evidence.json")
    if not os.path.exists(evpath):
        legacy = os.path.join(jobdir, "evidence.json")  # older f1_measure wrote fatal runs here
        if os.path.exists(legacy):
            evpath = legacy
        else:
            return {"ok": False, "error": "evidence.json missing after measurement"}
    ev = json.load(open(evpath))
    ev["evidence_file"] = os.path.basename(evpath)
    if ev.get("fatal"):
        return {"ok": False, "error": "site unreachable: " + ev["fatal"]}
    # 2. score
    log("scoring against pinned rubric ...")
    sc = score(ev)
    json.dump(sc, open(os.path.join(jobdir, "scoring.json"), "w"), indent=2)
    log(f"score {sc['total']}/100 grade {sc['grade']}")
    # 3. report.md (audit date in America/Chicago)
    from zoneinfo import ZoneInfo
    date_str = datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    domain = urlparse(ev.get("url", url)).hostname or url
    md = build_report(name, url, ev, sc, date_str, domain)
    mdpath = os.path.join(jobdir, "report.md")
    open(mdpath, "w").write(md)
    # 4. PDF
    log("rendering PDF ...")
    pdfpath = os.path.join(jobdir, "audit.pdf")
    r = subprocess.run([VENV_PY, os.path.join(F1, "tools", "render_pdf.py"), mdpath, pdfpath],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not os.path.exists(pdfpath) or os.path.getsize(pdfpath) < 5000:
        return {"ok": False, "error": "PDF render failed: " + (r.stderr or r.stdout)[-500:]}
    log(f"PDF ok: {os.path.getsize(pdfpath)} bytes")
    return {"ok": True, "score": sc["total"], "grade": sc["grade"], "pdf": pdfpath}

if __name__ == "__main__":
    name, url, jobdir = sys.argv[1], sys.argv[2], sys.argv[3]
    print(json.dumps(run_pipeline(name, url, jobdir)))
