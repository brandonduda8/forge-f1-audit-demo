# Launch taps: Apex Forge (Checkup $29, Pro $79, Monthly Monitoring $29/month, all on Square)

Everything is built and tested against a stand-in Square. Nothing is deployed, published or connected.
These are the steps only Brandon can do, in order. Sandbox first, so no real money moves until step 6.

Card details are only ever typed on Square's own checkout page. Our site and server never see them.
Your Square token, webhook key, owner password and mail password go **only** into Render's environment
settings. Never into a chat, an email, a file or a commit.

## 1. Merge the draft PRs (5 min)
On `forge-f1-audit-demo`, merge in this order: PR 4 (Square checkup), PR 5 (accessibility and security
checks), the payments kit PR, then the v2 PR. Close the old Stripe PR 3 without merging.

## 2. Render (about 15 min)
1. Sign up at render.com with your GitHub account. The free plan costs $0.
2. Dashboard → **New → Blueprint** → pick `brandonduda8/forge-f1-audit-demo`. Check both services say **Free**.
3. Fill the blank values (each one is explained next to it in `render.yaml`):
   - API: `SITE_ORIGIN`, `SQUARE_ACCESS_TOKEN`, `SQUARE_LOCATION_ID` (sandbox values, see step 3),
     `ADMIN_TOKEN` (make up 24+ random characters; this is your owner page password).
   - Site: `APEX_API_BASE`, `APEX_SUPPORT_EMAIL`, and `APEX_SITE_ORIGIN` (the site's own address, for search engines).
   - Leave the webhook, monitoring plan and mail values blank for now.
   If you don't know the addresses yet, apply first, copy them from each service's page, set them, and press
   **Manual Deploy** on both.

## 3. Square sandbox keys, plan and webhook (about 20 min)
Follow `selfserve/SQUARE-KIT.md`. In short:
1. Developer Console → your app → **Sandbox** → copy the access token and a location id into Render.
2. On your own computer, run `square_cli.py doctor`, then `square_cli.py create-monitor-plan`, and paste the
   variation id it prints into Render as `SQUARE_MONITOR_PLAN_VARIATION_ID`. The plan charges
   `MONITOR_PRICE_CENTS` ($29) every month, and `doctor` checks it matches Render. Square's subscription checkout
   documents one price per plan, so keep `MONITOR_FIRST_CENTS` equal to it unless a sandbox test shows an
   intro price works. The monitoring plan appears on the site only after this and after step 5's emails are on.
3. Add the webhook subscription (four events) and put its signature key and URL into Render.

## 4. Test it yourself in the sandbox (about 15 min)
1. Open the site. Run the free check on any public site.
2. Buy a **Checkup**, then a **Pro**, with Square's sandbox test card. Each lands on the report page; download the PDF.
3. Open **Your dashboard** from the report page. Open `admin.html`, paste your `ADMIN_TOKEN`, and check the sale shows.
4. Buy once with website `nosuchsite.invalid` and press **Refund me automatically**.
5. Monthly Monitoring only partly works in Square's sandbox (Square's own staff say so). If it won't complete,
   test it in step 6 with real money and refund yourself. Don't sell it until that real test has passed.

## 5. Buyer emails and the monthly re-check (needed only to sell Monthly Monitoring)
The Checkup and Pro sell without this. Monthly Monitoring stays hidden until emails are on, because buyers need
a copy of the renewal terms and a way to get their cancel link back.
- **Emails** (report ready, monthly check, and "find my report" links): set `MAIL_BACKEND=smtp` and the `SMTP_*`
  and `MAIL_FROM` values (a free Gmail with an app password works), then set the site's `APEX_EMAILS=on`.
  Only buyers get them, and each has a link to manage or cancel.
- **Monthly re-check**: in GitHub → repo **Settings → Secrets and variables → Actions**, add `APEX_API_BASE` and
  `APEX_ADMIN_TOKEN` (the same owner password). The daily `apex-monitor` workflow then wakes the server and
  re-checks each monitored site once its last check is 25+ days old. Without the secrets it does nothing.
  You can also press **Run monitoring now** on the owner page.
- GitHub pauses scheduled workflows on a public repo after 60 days with no commits. If that happens, open
  **Actions → apex-monitor → Enable workflow**, or press **Run monitoring now** on the owner page.

## 6. Go live — this is the publish moment
1. Developer Console → **Production**: copy the production token and location id into Render, set
   `SQUARE_ENV=production`, run `create-monitor-plan --production`, paste the new variation id, and add the
   production webhook with its new signature key. Manual Deploy both services.
2. Do one real purchase on your own site (set `CHECKUP_PRICE_CENTS=100` first for a $1 test), check the report
   loads, then press **Refund me automatically** or refund in the Square Dashboard. Set the price back to `2900`.
3. Before selling Monthly Monitoring, do the same once with it: make a $1 test plan (`MONITOR_FIRST_CENTS=100`,
   `MONITOR_PRICE_CENTS=100`, `CHECKUP_PRICE_CENTS=100`, then `create-monitor-plan --production`), buy it, check the report and dashboard
   load and the "report ready" email arrives, press **Cancel monitoring**, and refund yourself. Then put the
   prices back, make the real plan again and paste its variation id.

## 7. Read the legal pages (10 min)
`privacy.html` and `terms.html` are drafts marked "not legal advice". They now cover the monthly plan
(renews monthly, cancel online any time, ends at the end of the paid month). Change anything that isn't true for you.

## Good to know
- Render's free disk is wiped when the service redeploys, so a buyer's scan history on the dashboard can reset.
  Money, orders, subscriptions and each site's monthly marker are kept on Square, so sales, refunds,
  cancellations and "one email a month" are never lost.
- Refunding a monitoring order stops its renewal first. If Square won't stop it, the refund waits.
- Square takes its processing fee from each payment. Check the first real payment's fee in your Square Dashboard.
