# Square kit: setting up payments for Apex Forge

You already have a Square account. This guide connects it to the Apex Forge server.
You do every step yourself. Nothing here signs you up for anything new, and nothing costs money until a real buyer pays.

**The one rule:** your Square access token and webhook signature key go **only** into Render's
environment settings. Never paste them into a chat, an email, a document, a commit or a file.
If one ever leaks, open the Developer Console and replace it.

## 1. Get your sandbox token and location id

Sandbox is Square's practice mode. It uses test cards and no real money moves.

1. Go to the Square Developer Console (developer.squareup.com) and sign in with your Square account.
2. Open your application, or create one if there isn't one yet. Creating an application is free.
3. At the top of the page, switch to **Sandbox**.
4. Under **Credentials**, copy the **Sandbox Access token**.
5. Under **Locations**, copy the id of the location you want sales to go to.

## 2. Put them into Render (and nowhere else)

In Render, open the Apex Forge API service, go to **Environment**, and add:

| Key | Value |
|---|---|
| `SQUARE_ENV` | `sandbox` |
| `SQUARE_ACCESS_TOKEN` | the sandbox access token |
| `SQUARE_LOCATION_ID` | the location id |

Prices are optional. The defaults are `CHECKUP_PRICE_CENTS=2900` ($29), `PRO_PRICE_CENTS=7900` ($79) and
`MONITOR_PRICE_CENTS=1900` ($19 a month). Prices are in cents.

Save. Render restarts the service.

## 3. Check the setup

Render's free plan has no Shell, so run the kit on your own computer, inside the `forge-f1-audit-demo` folder.
`read -rs` asks for the token without showing it on screen or saving it in your shell history:

```
read -rs SQUARE_ACCESS_TOKEN && export SQUARE_ACCESS_TOKEN
export SQUARE_ENV=sandbox SQUARE_LOCATION_ID=your-location-id
python3 selfserve/api/square_cli.py doctor
```

Close the terminal when you're done, so the token goes away with it.

Each line starts with OK, WARN or FAIL. The command never prints your token or key.
Fix any FAIL line, then run it again. It's normal to see WARN lines for the monitoring plan and the webhook until you finish steps 4 and 5.

## 4. Create the monthly monitoring plan (sandbox first)

In the same terminal:

```
python3 selfserve/api/square_cli.py create-monitor-plan
```

This creates one subscription plan in your Square catalog, "Apex Forge Monthly Monitoring". It has one monthly
phase at `MONITOR_PRICE_CENTS`. The last line the command prints is the plan **variation id**.
Copy it into Render as `SQUARE_MONITOR_PLAN_VARIATION_ID`, then save. Run `doctor` again. It should now say
"Monthly Monitoring plan found". The Monthly Monitoring option appears on the site only after this step.

If you change `MONITOR_PRICE_CENTS` later, `doctor` reports that the plan's price no longer matches.
Run `create-monitor-plan --force` to make a plan at the new price, then paste the new variation id into Render.
Existing subscribers stay on the old plan.

Other commands:

```
python3 selfserve/api/square_cli.py plans             # what the site offers, and at what price
python3 selfserve/api/square_cli.py sales --days 30   # orders, gross, refunds, net, active monitors, monthly recurring
```

## 5. Webhooks

In the Developer Console, under **Webhooks**, choose **Add subscription** (sandbox first). Fill it in:

- **URL**: `https://<your API service>/api/square/webhook`
- **Events**:
  - `payment.created`
  - `payment.updated`
  - `subscription.created`
  - `subscription.updated`

Save the subscription, then copy its **Signature key**. In Render, add two settings:

- `SQUARE_WEBHOOK_SIGNATURE_KEY`: the signature key
- `SQUARE_WEBHOOK_URL`: the exact URL you entered above, character for character

Run `doctor` once more. If you skip webhooks, everything still works, but a report only starts building once the buyer lands on the report page.

## 6. Try it in sandbox

Buy a Checkup on the site with one of Square's sandbox test cards. Then run `sales` to see the sale.
Monthly Monitoring subscriptions only partly work in sandbox (see below).

## 7. Production, later

When sandbox works, do the same steps with **Production** selected in the Developer Console:

1. Copy the production access token and the production location id into Render. Set `SQUARE_ENV=production`.
2. Run `doctor`.
3. Run `python3 selfserve/api/square_cli.py create-monitor-plan --production`. The command asks you to type `yes`,
   because this creates a real plan. Paste the new variation id into Render. Sandbox and production ids are
   different, so the sandbox variation id won't work in production.
4. Add the production webhook subscription with the same four events. Put its signature key into Render.
5. Run `doctor` until there are no FAIL lines.

## How it works (short)

- Buyers pay on Square's own checkout page. Card details never touch the Apex Forge server.
- One-time plans (Checkup, Pro) are a Square order that we price. The website and plan are saved on the order.
- Monthly Monitoring uses Square's subscription payment link. Square bills the card every month.
  Buyers can cancel from their dashboard. Square ends the subscription at the end of the month they've paid for.
- All money numbers (sales, refunds, active subscriptions, monthly recurring) are read live from Square.
  They are right even if the server restarts or Render wipes its disk.
- The `sales` gross counts checkout orders only. Monthly renewals are billed by Square on its own orders,
  so they show up as "active monitors" and "monthly recurring", not in gross.
  Square Dashboard's own sales reports include everything.

## What I couldn't confirm

I checked these against Square's developer docs and forum answers from Square staff. Some code examples in the docs didn't load, so these points are **not confirmed**. The code handles each one safely, and the first real sandbox run will settle them:

1. **The order behind a subscription checkout.** Square staff say a subscription payment link takes only `quick_pay`, not your own order, so the website can't go on the order when the link is made. The kit writes the website and plan onto the link's order right after creating the link, with UpdateOrder. The docs don't say whether Square allows that on a payment link's order (the order starts out as DRAFT). If Square refuses it, the kit takes the link down and the buyer sees "try again" instead of paying. **First sandbox test:** start a Monthly Monitoring checkout and check that it opens Square's page.
2. **Linking the subscription to its order.** Square says the subscription appears under the buyer's customer profile. It doesn't say the subscription points back to the order. The kit pairs them by customer and time, then records the pair on the order. If one buyer starts two monitoring subscriptions within about 3 days, the pairing goes by time order and could swap them.
3. **Which customer id Square records.** The kit reads it from the payment, or from the order if the payment has none. If Square puts it on neither, the kit can't tie the subscription to its website. The dashboard then shows no subscription, the monthly re-check skips that site, and the buyer's report link expires after 30 days like a one-time purchase. Square still bills the subscription, so you'd have to cancel it in Square Dashboard. `sales` would still count it under active monitors.
4. **Sandbox subscriptions.** A Square staff answer says subscription payment links don't fully show up in sandbox, and that their own testing used real $1 plans in production. The monthly re-check and cancel flow may need a small real test in production.
5. **Webhooks at the end of a subscription.** Square has said it may send no `subscription.updated` event when a subscription simply reaches its end date. The kit always reads the status from Square instead of relying on that event.
6. **Plan price field.** The kit sets the plan price in the phase's `pricing.price_money`, as Square's SubscriptionPricing reference says. An older doc example shows `price` instead. `doctor` accepts either when it reads the plan back.
7. **Search details.** The docs pages for SearchOrders and SearchSubscriptions hide some filter details, so two of the filters the kit uses are unconfirmed: SearchOrders' `customer_filter`, and SearchSubscriptions' `location_ids` and `customer_ids`. The kit re-checks location and plan on every result anyway. It's also unclear whether SearchOrders returns orders still in DRAFT. Paid orders aren't affected.
8. **Old checkouts.** A payment counts in `sales` only if its order was created up to 3 days before the report window starts. A checkout page left open longer than that before paying would be missed in that window.
