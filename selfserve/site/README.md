# Apex Forge Website Checkup — static site

Plain HTML, CSS and vanilla JavaScript. No framework, no build tools, no external
scripts, fonts, images, CDNs, analytics or trackers. The only build step writes
`config.js` from environment variables.

## Flow

1. `index.html` — a visitor types their website and gets a free mini-check
   (grade, score, top 3 issues) from `POST {APEX_API_BASE}/api/check`.
2. The Buy button goes to the Stripe Payment Link (same tab). On Stripe's checkout
   the buyer types their website into the Payment Link's required "Website" field.
3. Stripe redirects to `report.html?session_id={CHECKOUT_SESSION_ID}`.
   The page calls `GET {APEX_API_BASE}/api/report?session_id=...` and shows the full
   report, a Download PDF link and a Print button. If the site can't be reached the
   page offers an automatic refund via `POST {APEX_API_BASE}/api/refund`.

Set the Payment Link's after-payment redirect (in Stripe) to
`https://YOUR-DOMAIN/report.html?session_id={CHECKOUT_SESSION_ID}`.

## Files

| File | What it is |
|---|---|
| `index.html` + `app.js` | Landing page and free mini-check |
| `report.html` + `report.js` | Paid report page (reads `session_id`) |
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
| `APEX_PAYMENT_LINK` | yes | The Stripe Payment Link. Must be `https://buy.stripe.com/...` |
| `APEX_PRICE` | no | Price label shown on the page. Default `$29`. Keep it in sync with the Payment Link price. |
| `APEX_SUPPORT_EMAIL` | no | If set, shown on the privacy and terms pages. If not set, those pages point to the refund button. |

`build_config.py` checks that both URLs are https (and that the payment link host is
`buy.stripe.com`). If anything is wrong it prints a clear message, does not write
`config.js`, and exits non-zero, which fails the Render build instead of publishing a
broken site.

```sh
APEX_API_BASE=https://your-api.onrender.com \
APEX_PAYMENT_LINK=https://buy.stripe.com/xxxx \
python3 build_config.py

python3 build_config.py --example   # placeholder config.js for local viewing
```

## Render static site

Create a Render **Static Site** pointing at this folder:

- Root directory: `selfserve/site`
- Build command: `python3 build_config.py`
- Publish directory: `.`
- Environment: set `APEX_API_BASE`, `APEX_PAYMENT_LINK` and optionally `APEX_PRICE`,
  `APEX_SUPPORT_EMAIL`.

Render's static-site build image includes Python 3, so no install step is needed.
Render serves `404.html` for missing paths automatically when it exists at the
publish root. Changing an env var needs a redeploy so `config.js` is rebuilt.

The API must allow CORS from the static site's origin for `/health`, `/api/check`,
`/api/report` and `/api/refund`, and send the PDF with `Content-Disposition` so the
Download PDF link saves a file.

## Security notes

- Each page carries a Content-Security-Policy meta tag:
  `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src https:; form-action 'self' https://buy.stripe.com; base-uri 'none'`.
  `connect-src` is `https:` because the API origin is runtime config. No inline scripts
  or inline style attributes are used.
- API data is only ever written with `textContent` / text nodes. No `innerHTML`.
- `session_id` must match `^cs_(test|live)_[A-Za-z0-9]{10,200}$` before any request.

## Tests

```sh
python3 -m pytest -q tests && node tests/test_render.mjs
```

The tests check HTML structure, page metadata and CSP, external URLs, inline
scripts, `innerHTML`, `build_config.py` validation, and that hostile API strings
render as text. They do not render the pages in a real browser.
