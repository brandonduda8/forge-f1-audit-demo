# Apex Forge Website Checkup ($29, self-serve)

A business owner types their website, sees a free grade, pays $29 on Square's checkout (card, Cash App Pay,
Apple Pay or Google Pay, as the Square account allows), and gets the full report on screen and as a PDF.
Nobody talks to anybody.

```
landing page (static) --POST /api/check--> API --runner.py light--> 3 requests to their site --> grade + top 3
      | Buy: POST /api/checkout {url}
      v
API --Square: create payment link (the order holds the website)--> checkout URL
      (the page saves the order id in the browser, then goes to Square)
Square hosted checkout (card data stays with Square) --redirect--> report.html?orderId=...
Square --signed webhook, payment COMPLETED--> API starts building the report before the buyer lands
report.html --GET /api/report?order_id=--> API --Square: paid order, our location and marker?--> runner.py full --> report
                                   \--GET /api/report.pdf--> render_pdf.py --> PDF
                                   \--POST /api/refund (only if the site could not be reached)--> Square Refunds API
```

| Part | Where | Needs a server? |
|---|---|---|
| Landing, free check form, report page, legal pages | `site/` (static, no frameworks, CSP self-only) | No: Render static site, free, never sleeps |
| Checker, checkout, payment check, PDF, refunds, webhook | `api/server.py`, `api/square_api.py` (stdlib only) | Yes: browsers can't fetch other people's sites or hold the Square token. Render free web service. |
| Measurement and scoring | `f1-audit-product/tools/f1_measure.py`, `audit-demo/audit_pipeline.py` (unchanged methods) | runs inside the API |

## Honesty rules the code enforces
- Every finding comes from a live measurement of the site, through the pinned rubric in `audit_pipeline.py`.
- Anything the run could not finish (time or request cap, an address we refused to visit) becomes "not measured",
  never a failure. A category with nothing measured gets no score instead of a 0.
- Checks that need a real browser render (Core Web Vitals, tap targets) are listed as not measured.

## Safety
- Card data never touches our pages or server: buyers pay on Square's hosted checkout. The site only follows a
  checkout URL whose host is Square's (`square.link`, `checkout.square.site`, `squareup.com`, `squareupsandbox.com`).
- `api/netguard.py` refuses private, loopback, link-local and metadata addresses and every port but 80/443,
  checked on the exact address dialled (so redirects and DNS rebinding can't get around it).
- Body cap 2 MB; free check = exactly 3 requests; paid report <= 90 requests and 75 seconds, enforced by a
  timer too, so a site that drips bytes slowly can't hold a worker. Each run is capped at 300 MB of memory.
- A homepage that answers with an HTTP error or a bot-check page is never graded; the paid run becomes refundable.
- Each run is a separate process with no secrets in its environment and no proxy settings.
- Payment check: the order must be at our Square location and carry our marker (`apex-checkup-v1`), have a
  COMPLETED payment for that order of at least the price in USD, and be under 30 days old. The website is the
  one normalized and stored on the order before checkout, so one payment can't audit other sites.
- Webhook: `x-square-hmacsha256-signature` is checked (HMAC-SHA256 of notification URL + raw body, constant-time
  compare) before anything is read. A good event only starts building the report early; the report itself still
  re-checks the payment with Square.
- Refunds: only when the paid run could not reach the site; one idempotency key per order. Refund and delivery
  state live on Square (payment `refunded_money`; order metadata `apex_report_delivered`), so a restart can't
  reopen a refund for a delivered report, and a refunded link stops working.
- Free checks and paid reports have separate one-at-a-time worker pools; parallel loads of one report share one run.
- `POST /api/find` (email + website) finds a lost report link from Square's payments, so a buyer never has to write in.
- Rate limits: free check 5 per visitor and 3 per website per hour, 60 total per hour; checkouts 10 per visitor
  and 200 total per hour.

## Run the tests
```
pip install -r requirements.txt pytest
python -m pytest -q selfserve/api/tests selfserve/site/tests
node selfserve/site/tests/test_render.mjs
```
The end-to-end tests run the real server against a fake Square API and local fixture websites.

## Run it locally (Square sandbox)
```
SQUARE_ENV=sandbox SQUARE_ACCESS_TOKEN=... SQUARE_LOCATION_ID=... SITE_ORIGIN=http://localhost:8000 \
  python selfserve/api/server.py
cd selfserve/site && APEX_API_BASE=https://... APEX_SUPPORT_EMAIL=you@example.com python3 build_config.py
```

Launch steps for Brandon: `LAUNCH.md`.
