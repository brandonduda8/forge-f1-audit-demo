# Apex Forge Website Checkup — static site

Plain HTML, CSS and vanilla JavaScript. No framework, no build tools, no external
scripts, fonts, images, CDNs, analytics or trackers. The only build step writes
`config.js` from environment variables.

## Flow

1. `index.html` — a visitor types their website and gets a free mini-check
   (grade, score, top 3 issues) from `POST {APEX_API_BASE}/api/check`.
2. Buy (on the free-check result, or a plan card) sends the website and plan to
   `POST {APEX_API_BASE}/api/checkout` as `{"url", "plan"}`. Plans and prices come from
   `GET {APEX_API_BASE}/api/plans` at runtime: until it answers only the one-time Website Checkup
   can be bought (at `APEX_PRICE`), Pro needs the API to list it as available, and the Monthly
   Monitoring card stays hidden unless the API lists it as available (the API only sells it while buyer
   emails are on). Monitoring has two prices, shown everywhere it is described as "$29 for the first
   month, then $19 a month" from the plan's `first_cents` and `cents` (`data-plan-terms`,
   `data-plan-first-price`, `data-plan-price`; the pages' own text is the fallback). The API creates a Square payment link whose
   order holds the website, and returns its URL and the order id. The page checks the
   URL is Square's own (`square.link`, `checkout.square.site`, `squareup.com`,
   `squareupsandbox.com`), saves the order id in this browser, and goes there.
   Card details are typed only on Square's page.
3. Square redirects to `report.html` (in production with `?orderId=...`). The page takes
   `order_id`, then `orderId`, then the saved order id; calls
   `GET {APEX_API_BASE}/api/report?order_id=...`; and shows the full report, a Download PDF
   link and a Print button. If the site can't be reached the page offers an automatic refund
   via `POST {APEX_API_BASE}/api/refund`.
4. `find.html` gets a lost link back from the email paid with plus the website. When the API answers
   `{"ok": true, "emailed": true}` (buyer emails on) the links went to that inbox and the page shows none.
5. `dashboard.html?order_id=...` (linked from the report page and Find my report) shows the plan,
   the report link, check history, the latest changes and the subscription; Monthly Monitoring can
   be cancelled there after an in-page confirmation (`POST /api/cancel`); the Monitoring section then
   updates in place ("Ends on"). Monitoring orders (also refunded ones) get a printable renewal-terms
   block, also shown on the report page when the report response says `"plan": "monitor"`.
6. `admin.html` is the owner's page (noindex). Paste `ADMIN_TOKEN`; it is kept in
   `sessionStorage` only (never localStorage or a URL) and sent as `Authorization: Bearer ...` to
   `/api/admin/summary` and `/api/admin/run-monitoring`. Log out removes it.

## Files

| File | What it is |
|---|---|
| `index.html` + `app.js` | Landing page, plans and free mini-check |
| `small-business-website-audit.html`, `website-accessibility-check.html`, `ssl-certificate-check.html`, `spf-dmarc-email-check.html`, `local-seo-website-check.html`, `website-monitoring.html` | Search landing pages: what that part of the checker really checks, what it doesn't, an FAQ, and the free check (`app.js`) |
| `dashboard.html` + `dashboard.js` | Buyer dashboard (`?order_id=`) |
| `admin.html` + `admin.js` | Owner dashboard (sales, MRR, counters, run monitoring) |
| `report.html` + `report.js` | Paid report page (reads `order_id` / `orderId`) |
| `privacy.html`, `terms.html` + `legal.js` | Legal pages (DRAFTS: read before publishing; not legal advice). `legal.js` shows the support email if one is configured |
| `404.html` | Not-found page (uses absolute `/` paths so it works at any URL) |
| `common.js` | Shared helpers: config validation, safe DOM building (text only), fetch with timeout |
| `style.css` | Shared styles: mobile-first, dark mode, focus states, print styles |
| `config.example.js` | Example of the generated `config.js` |
| `build_config.py` | Writes `config.js` from env vars (python3 standard library only) |
| `robots.txt` | Allows all. `--publish` with `APEX_SITE_ORIGIN` replaces it with one that has the Sitemap line |
| `tests/` | Offline tests (pytest + node, no packages) |

`config.js` is generated; don't edit it by hand.

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `APEX_API_BASE` | yes | https URL of the checker API, e.g. the Render web service URL. No query string. |
| `APEX_CHECKOUT` | no | `on` (default) or `off`. `off` hides buying. |
| `APEX_PRICE` | no | Price label shown on the page. Default `$29`. Keep it in sync with the API's `CHECKUP_PRICE_CENTS`. |
| `APEX_SUPPORT_EMAIL` | yes | Shown on the privacy and terms pages, for refund and privacy requests. |
| `APEX_EMAILS` | no | `on` only when the API really sends buyer emails (`MAIL_BACKEND=smtp`). Default `off`: the pages then don't promise any email. |
| `APEX_SITE_ORIGIN` | no | https origin the site is served at, e.g. `https://apex-checkup.onrender.com`. With `--publish` it adds canonical links to the indexable pages, `sitemap.xml`, and the robots.txt Sitemap line. Unset: none of those. |

`build_config.py` checks that the API URL is https and the email looks like one. If anything is wrong it prints a clear message, does not write
`config.js`, and exits non-zero, which fails the Render build instead of publishing a
broken site.

```sh
APEX_API_BASE=https://your-api.onrender.com \
APEX_SUPPORT_EMAIL=help@example.com \
python3 build_config.py

python3 build_config.py --example   # placeholder config.js for local viewing
python3 build_config.py --publish public   # the publishable site in ./public (what Render serves)
```

`--publish DIR` copies only the top-level `*.html`, `*.css`, `*.js` (never `config.example.js`)
and `robots.txt`, writes a fresh `config.js`, and with `APEX_SITE_ORIGIN` adds canonical links and
`sitemap.xml`. Folders such as `tests/` and files such as this README are never published.

## Render static site

Create a Render **Static Site** pointing at this folder:

- Root directory: `selfserve/site`
- Build command: `python3 build_config.py --publish public`
- Publish directory: `public`
- Environment: set `APEX_API_BASE`, `APEX_SUPPORT_EMAIL` and optionally `APEX_PRICE`, `APEX_CHECKOUT`,
  `APEX_EMAILS`, `APEX_SITE_ORIGIN`.

Render's static-site build image includes Python 3, so no install step is needed.
Render serves `404.html` for missing paths automatically when it exists at the
publish root. Changing an env var needs a redeploy so `config.js` is rebuilt.

The API must allow CORS from the static site's origin for `/health`, `/api/check`, `/api/plans`,
`/api/checkout`, `/api/report`, `/api/find`, `/api/refund`, `/api/dashboard`, `/api/cancel` and
`/api/admin/*` (the admin routes also need the `Authorization` request header allowed), and send the PDF with `Content-Disposition` so the
Download PDF link saves a file.

## Security notes

- Each page carries a Content-Security-Policy meta tag:
  `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src https:; form-action 'self'; base-uri 'none'`.
  `connect-src` is `https:` because the API origin is runtime config. No inline scripts
  or inline style attributes are used.
- API data is only ever written with `textContent` / text nodes. No `innerHTML`.
- The order id must match `^[A-Za-z0-9_-]{10,64}$` before any request.
- The page never builds a payment form. It only follows a checkout URL whose host is Square's.

## Tests

```sh
python3 -m pytest -q tests && node tests/test_render.mjs
node tests/contract.mjs tests/fixtures/mini.json tests/fixtures/report-pro.json \
  --plans=tests/fixtures/plans.json --dashboard=tests/fixtures/dashboard.json --admin=tests/fixtures/admin.json
```

`contract.mjs` checks each API response against the shape the pages are built for, then renders
it. The API's end-to-end test feeds it real responses. `tests/fixtures/` holds copies of the server's
real shapes; `test_site.py::test_fixtures_match_what_the_server_really_returns` builds the dashboard and
admin responses with the server's own code (Square faked) and checks the fixtures still match.

The tests check HTML structure, page metadata and CSP, external URLs, inline
scripts, `innerHTML`, `build_config.py` validation, and that hostile API strings
render as text. They do not render the pages in a real browser.
