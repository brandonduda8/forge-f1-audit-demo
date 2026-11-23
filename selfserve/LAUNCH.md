# Launch taps: Apex Forge Website Checkup ($29)

Everything is built and tested. Nothing is deployed, published or connected.
These are the steps only Brandon can do, in order. About 60 to 90 minutes in all.
Test mode first, so no real money moves until step 7.

## 1. Stripe account (about 20 min)
1. Sign up at stripe.com (or sign in if you already have one).
2. Activate payments: business type "Individual / sole proprietor", Apex Forge, your bank account for payouts.
   Stripe shows a support email on receipts; any refund email gets a written answer only.
3. Settings → Customer emails: turn on "Successful payments" receipts.

## 2. Make the $29 payment link in TEST mode (about 10 min)
Turn on "Test mode" (top right of the Stripe dashboard), then Payment Links → New:
1. Product: "Website Checkup", one-time price **$29.00 USD**. Leave "Let customers adjust quantity" off.
2. Add custom field: type **Text**, label **Your website**, required, key **website** if the form lets you set it
   (if it doesn't, any key works: the server uses the only text field on the link).
3. After payment: choose **Don't show confirmation page → Redirect customers to your website**:
   `https://<your static site>/report.html?session_id={CHECKOUT_SESSION_ID}`
   (you get the static site address in step 4; come back and fill it in).
4. Create the link. Copy two things: the link URL (`https://buy.stripe.com/test_...`) and the link id (`plink_...`, shown in the link's details / URL).

## 3. Restricted API key in TEST mode (about 5 min)
Developers → API keys → **Create restricted key**, name it "apex-checkup":
- Checkout Sessions: **Read** (checks the payment and finds lost report links)
- PaymentIntents: **Write** (marks a payment "report delivered", so no refund after delivery, even after a restart)
- Charges and Refunds: **Write** (the automatic refund for a site that can't be reached)
- Everything else: None.
Stripe's permission names can shift a little; pick the closest match. Step 5 tests all three.
Copy the `rk_test_...` key. Paste it only into Render (step 4). Never into a file, chat or email.

## 4. Render (about 15 min)
1. Sign up at render.com with your GitHub account. Render may ask for a card to verify you; the free plan costs $0.
2. Merge the draft PR on `forge-f1-audit-demo` (it adds `selfserve/` and the new `render.yaml`).
3. Dashboard → **New → Blueprint** → pick `brandonduda8/forge-f1-audit-demo`. Render shows two **Free** services:
   `apex-checkup-api` and `apex-checkup`. Check both say Free, then fill the blank values:
   - `STRIPE_SECRET_KEY` = the `rk_test_...` key
   - `STRIPE_PAYMENT_LINK_ID` = the `plink_...` id
   - `SITE_ORIGIN` = the static site address, e.g. `https://apex-checkup.onrender.com` (no trailing slash)
   - `APEX_API_BASE` = the API address, e.g. `https://apex-checkup-api.onrender.com`
   - `APEX_PAYMENT_LINK` = the `https://buy.stripe.com/test_...` link
   - `APEX_SUPPORT_EMAIL` = an address for refund and privacy requests (required; a separate free inbox keeps
     your personal one private). The terms promise a refund within 7 days to anyone who emails it, so
     answering means one click in Stripe, no conversation.
   If you don't know the addresses yet, apply first, copy them from each service's page, then set the values and
   press **Manual Deploy** on both services.
4. Back in Stripe, put the static site address into the payment link's redirect (step 2.3).

## 5. Test it yourself, in test mode (about 10 min)
1. Open the static site. Run the free check on a site you own (or any public site).
2. Press Buy. On Stripe's test checkout use card `4242 4242 4242 4242`, any future date, any CVC, and type your website.
3. You land on the report page. The first load can take up to a minute while the free server wakes.
4. Press **Download PDF**. In Stripe's test Payments, open that payment: its metadata should show
   `apex_report_delivered = 1`.
5. Open **Find my report** (bottom of any page), enter the test email and your website: it should link to the report.
6. Try a test purchase with website `nosuchsite.invalid` and press **Refund me automatically**: the refund
   should show in Stripe's test Payments, and reloading that report link should now say it was refunded.
If any of these three fails with a permissions error, fix the key's permissions in step 3.

## 6. Read the legal pages (10 min)
`privacy.html` and `terms.html` are drafts marked "not legal advice". Read them and change anything that isn't true for you.

## 7. Go live (about 10 min) — this is the publish moment
1. In Stripe, turn Test mode off and repeat steps 2 and 3 in live mode (live link + `rk_live_...` key).
2. In Render, replace `STRIPE_SECRET_KEY`, `STRIPE_PAYMENT_LINK_ID` and `APEX_PAYMENT_LINK` with the live values,
   then Manual Deploy both services.
3. Do one real $29 purchase on your own site, then refund yourself in the Stripe dashboard.

## 8. Start getting buyers
Follow `NO-CONTACT-PLAN.md`: directory listings and SEO pages. No emails, DMs or calls.

## Limits to know
- Free Render web services sleep after 15 minutes idle and take about a minute to wake. The page wakes the
  server as soon as someone lands, and tells them if it is waking. Render says free instances are not meant for
  production; if sales come, the $7/month paid instance removes the sleep.
- Reports live in server memory. If Render restarts the server, the report link simply re-runs the check
  (links work for 30 days).
- The checker makes at most 3 page requests for a free check and at most 90 for a paid report (plus any
  redirects the site itself sends, at most 5 each), only to the website entered, never to private network addresses.
- The free check is limited per visitor address, per website and in total (60 an hour). On Render the visitor
  address comes from a header a visitor could fake; that only weakens the per-visitor limit, the other two still cap it.
- One free check and one paid report run at a time; others wait their turn (up to 30 and 90 seconds).
- A homepage that answers with an error, or a bot-check page, is not graded: the buyer gets the automatic refund.
