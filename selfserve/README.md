# Apex Forge Website Checkup ($29, self-serve)

A business owner types their website, sees a free grade, pays $29 on Stripe, and gets the full report on screen
and as a PDF. Nobody talks to anybody.

```
landing page (static) --POST /api/check--> API --runner.py light--> 3 requests to their site --> grade + top 3
      | Buy (Stripe Payment Link, required "Your website" field)
      v
Stripe checkout --redirect--> report.html?session_id=cs_...
report.html --GET /api/report--> API --Stripe: is this session paid, from our link?--> runner.py full --> report
                                   \--GET /api/report.pdf--> render_pdf.py --> PDF
                                   \--POST /api/refund (only if the site could not be reached)--> Stripe refund
```

| Part | Where | Needs a server? |
|---|---|---|
| Landing, free check form, report page, legal pages | `site/` (static, no frameworks, CSP self-only) | No: Render static site, free, never sleeps |
| Checker, payment check, PDF, refunds | `api/server.py` (stdlib HTTP server) | Yes: browsers can't fetch other people's sites or hold the Stripe key. Render free web service. |
| Measurement and scoring | `f1-audit-product/tools/f1_measure.py`, `audit-demo/audit_pipeline.py` (unchanged methods) | runs inside the API |

## Honesty rules the code enforces
- Every finding comes from a live measurement of the site, through the pinned rubric in `audit_pipeline.py`.
- Anything the run could not finish (time or request cap, an address we refused to visit) becomes "not measured",
  never a failure. A category with nothing measured gets no score instead of a 0.
- Checks that need a real browser render (Core Web Vitals, tap targets) are listed as not measured.

## Safety
- `api/netguard.py` refuses private, loopback, link-local and metadata addresses and every port but 80/443,
  checked on the exact address dialled (so redirects and DNS rebinding can't get around it).
- Body cap 5 MB; free check = exactly 3 requests; paid report <= 90 requests and 75 seconds.
- Each run is a separate process with no secrets in its environment and no proxy settings.
- Stripe: the session must be complete, paid, from our payment link, same test/live mode as the key, and under
  30 days old. The website comes from the paid session, so one payment can't audit other sites.
- Refunds: only when the paid run could not reach the site; idempotent per session.
- Rate limits: free check 5 per visitor and 3 per website per hour, 60 total per hour.

## Run the tests
```
pip install -r requirements.txt pytest
python -m pytest -q selfserve/api/tests selfserve/site/tests
node selfserve/site/tests/test_render.mjs
```

## Run it locally (test mode)
```
STRIPE_SECRET_KEY=rk_test_... STRIPE_PAYMENT_LINK_ID=plink_... SITE_ORIGIN=http://localhost:8000 \
  python selfserve/api/server.py
cd selfserve/site && APEX_API_BASE=https://... APEX_PAYMENT_LINK=https://buy.stripe.com/test_... python3 build_config.py
```

Launch steps for Brandon: `LAUNCH.md`.
