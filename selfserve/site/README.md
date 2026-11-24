# Apex Forge Website Checkup — static site

Plain HTML, CSS and vanilla JavaScript. No framework, no build tools, no external
scripts, fonts, images, CDNs, analytics or trackers. The only build step writes
`config.js` from environment variables.

## Flow

1. `index.html` — a visitor types their website and gets a free mini-check
   (grade, score, top 3 issues) from `POST {APEX_API_BASE}/api/check`.
2. Buy (on the free-check result, or the price box) sends the website to
   `POST {APEX_API_BASE}/api/checkout`. The API creates a Square payment link whose
   order holds the website, and returns its URL and the order id. The page checks the
   URL is Square's own (`square.link`, `checkout.square.site`, `squareup.com`,
   `squareupsandbox.com`), saves the order id in this browser, and goes there.
   Card details are typed only on Square's page.
3. Square redirects to `report.html` (in production with `?orderId=...`). The page takes
   `order_id`, then `orderId`, then the saved order id; calls
   `GET {APEX_API_BASE}/api/report?order_id=...`; and shows the full report, a Download PDF
   link and a Print button. If the site can't be reached the page offers an automatic refund
   via `POST {APEX_API_BASE}/api/refund`.
4. `find.html` gets a lost link back from the email paid with plus the website.

## Files

| File | What it is |
|---|---|
| `index.html` + `app.js` | Landing page and free mini-check |
| `report.html` + `report.js` | Paid report page (reads `order_id` / `orderId`) |
| `privacy.html`, `terms.html` + `legal.js` | Legal pages (DRAFTS: read before publishing; not legal advice). `legal.js` shows the support email if one is configured |
| `404.html` | Not-found page (uses absolute `/` paths so it works at any URL) |
| `common.js` | Shared helpers: config validation, safe DOM building (text only), fetch with timeout |
| `style.css` | Shared styles: mobile-first, dark mode, focus states, print styles |
| `config.example.js` | Example of the generated `config.js` |
| `build_config.py` | Writes `config.js` from env vars (python3 standard library only) |
| `robots.txt` | Allows all; Sitemap line is a commented placeholder to fill in |
| `tests/` | Offline tests (pytest + node, no packages) |

`config.js` is generated; don't edit it by hand.

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `APEX_API_BASE` | yes | https URL of the checker API, e.g. the Render web service URL. No query string. |
| `APEX_CHECKOUT` | no | `on` (default) or `off`. `off` hides buying. |
| `APEX_PRICE` | no | Price label shown on the page. Default `$29`. Keep it in sync with the API's `CHECKUP_PRICE_CENTS`. |
| `APEX_SUPPORT_EMAIL` | yes | Shown on the privacy and terms pages, for refund and privacy requests. |

`build_config.py` checks that the API URL is https and the email looks like one. If anything is wrong it prints a clear message, does not write
`config.js`, and exits non-zero, which fails the Render build instead of publishing a
broken site.

```sh
APEX_API_BASE=https://your-api.onrender.com \
APEX_SUPPORT_EMAIL=help@example.com \
python3 build_config.py

python3 build_config.py --example   # placeholder config.js for local viewing
```

## Render static site

Create a Render **Static Site** pointing at this folder:

- Root directory: `selfserve/site`
- Build command: `python3 build_config.py`
- Publish directory: `.`
- Environment: set `APEX_API_BASE`, `APEX_SUPPORT_EMAIL` and optionally `APEX_PRICE`, `APEX_CHECKOUT`.

Render's static-site build image includes Python 3, so no install step is needed.
Render serves `404.html` for missing paths automatically when it exists at the
publish root. Changing an env var needs a redeploy so `config.js` is rebuilt.

The API must allow CORS from the static site's origin for `/health`, `/api/check`,
`/api/checkout`, `/api/report`, `/api/find` and `/api/refund`, and send the PDF with `Content-Disposition` so the
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
```

The tests check HTML structure, page metadata and CSP, external URLs, inline
scripts, `innerHTML`, `build_config.py` validation, and that hostile API strings
render as text. They do not render the pages in a real browser.
