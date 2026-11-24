# Launch taps: Apex Forge Website Checkup ($29, Square)

Everything is built and tested against a fake Square. Nothing is deployed, published or connected.
These are the steps only Brandon can do, in order. About 60 to 90 minutes, plus however long Square takes
to verify you. Sandbox first, so no real money moves until step 7.

Card details are only ever typed on Square's own checkout page. Our site and server never see them.

## 1. Square account and verification (about 20 min, then Square's review)
1. Sign up at squareup.com (or sign in). Business: Apex Forge, sole proprietor if that's you.
2. Finish Square's identity check and link your bank account for payouts. This is the step that can take a day or two.
3. Note your fee: Zane's figures are 3.3% + 30¢ for no-code Payment Links ($27.74 kept per $29 sale) and
   2.9% + 30¢ through the API ($27.86). This build creates links through the API; which rate Square applies to
   those is not confirmed. Check the first real payment's fee in the dashboard.

## 2. Turn on Cash App Pay (5 min)
In the Square Dashboard, find the online checkout or payment links settings and switch on **Cash App Pay**
(and Apple Pay / Google Pay if offered). Square's menu names move around; search the dashboard for "Cash App Pay".
Whatever is switched on there is what buyers see; the code doesn't need a change.

## 3. Developer app: token and location (about 10 min)
1. Go to developer.squareup.com, sign in with the same Square account, and **create an application**
   (name it "apex-checkup").
2. Switch to **Sandbox**. Copy the **Sandbox access token** and, from **Locations**, the sandbox **location ID**.
3. Paste them only into Render (step 4). Never into a file, chat or email.

## 4. Render (about 15 min)
1. Sign up at render.com with your GitHub account. Render may ask for a card to verify you; the free plan costs $0.
2. Merge the Square draft PR on `forge-f1-audit-demo` (it adds `selfserve/` and the new `render.yaml`).
   Close the older Stripe PR without merging.
3. Dashboard → **New → Blueprint** → pick `brandonduda8/forge-f1-audit-demo`. Render shows two **Free** services:
   `apex-checkup-api` and `apex-checkup`. Check both say Free, then fill the blank values:
   - `SQUARE_ACCESS_TOKEN` = the sandbox access token
   - `SQUARE_LOCATION_ID` = the sandbox location ID
   - `SQUARE_ENV` stays `sandbox` for now
   - `SITE_ORIGIN` = the static site address, e.g. `https://apex-checkup.onrender.com` (no trailing slash)
   - `APEX_API_BASE` = the API address, e.g. `https://apex-checkup-api.onrender.com`
   - `APEX_SUPPORT_EMAIL` = an address for refund and privacy requests (required; a separate free inbox keeps
     your personal one private). The terms promise a refund within 7 days to anyone who emails it, so
     answering means one click in Square, no conversation.
   - leave `SQUARE_WEBHOOK_URL` and `SQUARE_WEBHOOK_SIGNATURE_KEY` blank until step 5
   If you don't know the addresses yet, apply first, copy them from each service's page, then set the values and
   press **Manual Deploy** on both services.

## 5. Webhook (about 5 min)
1. In the Square Developer Console, your app → **Webhooks → Subscriptions → Add subscription** (Sandbox).
2. Notification URL: `https://<your API address>/api/square/webhook`
3. Events: `payment.created` and `payment.updated`.
4. Save, then copy the subscription's **Signature key**.
5. In Render, set `SQUARE_WEBHOOK_URL` to exactly the notification URL from 5.2 and
   `SQUARE_WEBHOOK_SIGNATURE_KEY` to the signature key, then Manual Deploy the API.
   Without the webhook everything still works; the report is just built when the buyer lands instead of
   while they're still on Square's page. Use Square's "send test event" button if it's offered: the API
   answers 200 for a correctly signed event and 401 for anything else.

## 6. Test it yourself in the sandbox (about 10 min)
1. Open the static site. Run the free check on a site you own (or any public site).
2. Press **Get the full report**. You land on Square's sandbox checkout. Square's sandbox test card is
   `4111 1111 1111 1111`, CVV `111`, any future date, any ZIP.
3. You land on the report page. The first load can take up to a minute while the free server wakes.
4. Press **Download PDF**.
5. Open **Find my report** (bottom of any page), enter the email you used at checkout and your website: it should
   link to the report.
6. Buy once more with website `nosuchsite.invalid` and press **Refund me automatically**: the refund should
   show in the sandbox dashboard, and reloading that report link should say it was refunded.

**If the sandbox checkout won't complete:** Square's own staff say payment links aren't fully supported in the
sandbox. Then skip to step 7 with `CHECKUP_PRICE_CENTS` set to `100`, so the live test costs $1, and refund it.

## 7. Go live (about 10 min) — this is the publish moment
1. In the Developer Console, switch to **Production**: copy the production access token and production
   location ID, and add the same webhook subscription for production (copy its new signature key).
2. In Render set `SQUARE_ENV=production` and replace `SQUARE_ACCESS_TOKEN`, `SQUARE_LOCATION_ID` and
   `SQUARE_WEBHOOK_SIGNATURE_KEY` with the production values. Manual Deploy both services.
3. Do one real purchase on your own site (at $1 if you set `CHECKUP_PRICE_CENTS=100`), check the report loads,
   then refund yourself in the Square Dashboard.
4. If you tested at $1, set `CHECKUP_PRICE_CENTS` back to `2900` and Manual Deploy the API.

## 8. Read the legal pages (10 min)
`privacy.html` and `terms.html` are drafts marked "not legal advice". Read them and change anything that isn't true for you.

## 9. Start getting buyers
Follow `NO-CONTACT-PLAN.md`: directory listings and SEO pages. No emails, DMs or calls.

## Not verified until you set up Square
These can only be confirmed against a real Square account; each has a fallback in the code.
- Square adds `orderId` to the redirect in production but not always in the sandbox. Fallback: the page saved the
  order id before going to checkout, and **Find my report** works by email.
- The webhook event names (`payment.created`, `payment.updated`). The code accepts any `payment.*` event whose
  payment is COMPLETED.
- `SQUARE_VERSION` defaults to `2025-01-23`. If Square rejects it, set `SQUARE_VERSION` in Render to the version
  the Developer Console shows.
- Writing the "report delivered" mark onto a completed order. If Square refuses, the server logs a warning and
  falls back to re-checking the site before any automatic refund.
- Which fee applies to links created through the API (see step 1.3).

## Limits to know
- Free Render web services sleep after 15 minutes idle and take about a minute to wake. The page wakes the
  server as soon as someone lands, and tells them if it is waking. If sales come, the $7/month paid instance
  removes the sleep.
- Reports live in server memory. If Render restarts the server, the report link simply re-runs the check
  (links work for 30 days).
- The report is shown on screen and as a PDF download. There is no email delivery, because that would need an
  email-sending account; Square's own receipt goes to the buyer, and **Find my report** recovers a lost link.
- The checker makes at most 3 page requests for a free check and at most 90 for a paid report (plus any
  redirects the site itself sends, at most 5 each), only to the website entered, never to private network addresses.
- The free check is limited per visitor address, per website and in total (60 an hour). On Render the visitor
  address comes from a header a visitor could fake; that only weakens the per-visitor limit, the other two still cap it.
- One free check and one paid report run at a time; others wait their turn (up to 30 and 90 seconds).
- A homepage that answers with an error, or a bot-check page, is not graded: the buyer gets the automatic refund.
