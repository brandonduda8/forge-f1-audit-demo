"""The payments kit: plans, checkout bodies, paid-order checks, subscriptions, sales
numbers, webhook kinds and the owner's CLI. Everything runs against FakeSquare on
loopback; nothing here talks to Square."""
import io
import os
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, API)
from helpers import LOC, FakeSquare  # noqa: E402

import square_api as pay  # noqa: E402
import square_cli  # noqa: E402

TOKEN = "EAAAtest_fake_token_for_kit_tests"
SIG_KEY = "test_kit_signature_key_456"
SITE = "https://bobs-roofing.example/"
DAY = 86400
ENV_KEYS = ("SQUARE_ACCESS_TOKEN", "SQUARE_ENV", "SQUARE_LOCATION_ID", "SQUARE_API_BASE",
            "SQUARE_MONITOR_PLAN_VARIATION_ID", "SQUARE_WEBHOOK_SIGNATURE_KEY", "SQUARE_WEBHOOK_URL",
            "CHECKUP_PRICE_CENTS", "PRO_PRICE_CENTS", "MONITOR_PRICE_CENTS", "MONITOR_FIRST_CENTS",
            "SELFSERVE_TEST_ALLOW_LOOPBACK")


def oid(tag):
    """A distinct order id ending in the tag (the fake derives payment ids from the last 20 chars)."""
    return "ORD" + "0" * (25 - len(tag)) + tag


@pytest.fixture
def sq(monkeypatch):
    fake = FakeSquare(page_size=3, token=TOKEN)
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SQUARE_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("SQUARE_ENV", "sandbox")
    monkeypatch.setenv("SQUARE_LOCATION_ID", LOC)
    monkeypatch.setenv("SQUARE_API_BASE", fake.base)
    monkeypatch.setenv("SELFSERVE_TEST_ALLOW_LOOPBACK", "1")
    yield fake
    fake.srv.shutdown()


@pytest.fixture
def var(sq, monkeypatch):
    v = sq.add_plan_variation(1900)
    monkeypatch.setenv("SQUARE_MONITOR_PLAN_VARIATION_ID", v)
    return v


# ------------------------------------------------------------------ plans

def test_plans_table_and_prices(sq, monkeypatch):
    assert list(pay.PLANS) == ["checkup", "pro", "monitor"]
    assert pay.plan_info("checkup") == {"id": "checkup", "label": "Website Checkup", "cents": 2900,
                                        "first_cents": 2900, "kind": "one_time", "mode": "full",
                                        "available": True}
    assert pay.plan_info("pro")["cents"] == 7900 and pay.plan_info("pro")["mode"] == "pro"
    m = pay.plan_info("monitor")
    assert m["cents"] == 1900 and m["first_cents"] == 2900                   # $29 first month, then $19
    assert m["kind"] == "subscription" and m["mode"] == "full" and not m["available"]
    for bad in ("", "PRO", "enterprise", None, 7, ["pro"]):
        assert pay.plan_info(bad) is None
    monkeypatch.setenv("PRO_PRICE_CENTS", "9900")
    monkeypatch.setenv("MONITOR_PRICE_CENTS", "abc")        # unusable -> default
    monkeypatch.setenv("CHECKUP_PRICE_CENTS", "5")           # out of range -> default
    assert pay.plan_info("pro")["cents"] == 9900
    assert pay.plan_info("monitor")["cents"] == 1900 and pay.price_cents() == 2900
    monkeypatch.setenv("MONITOR_FIRST_CENTS", "3900")
    assert pay.plan_info("monitor")["first_cents"] == 3900 and pay.plan_info("pro")["first_cents"] == 9900


def test_public_plans_shape_and_monitor_switch(sq, monkeypatch):
    rows = pay.public_plans()
    assert [r["id"] for r in rows] == ["checkup", "pro", "monitor"]
    assert all(set(r) == {"id", "label", "cents", "first_cents", "kind", "available"} for r in rows)
    assert (rows[2]["first_cents"], rows[2]["cents"]) == (2900, 1900)
    assert [r["available"] for r in rows] == [True, True, False]
    monkeypatch.setenv("SQUARE_MONITOR_PLAN_VARIATION_ID", "not an id!")
    assert not pay.plan_info("monitor")["available"]
    monkeypatch.setenv("SQUARE_MONITOR_PLAN_VARIATION_ID", "CATVAR000000000000000001")
    assert pay.public_plans()[2]["available"]
    assert "CATVAR" not in repr(pay.public_plans())          # never exposes catalog ids


# ------------------------------------------------------------------ checkout

def test_checkup_checkout_body_unchanged(sq):
    url, order_id = pay.create_checkout(SITE, "https://site.example/report.html")
    body = sq.links[-1]
    assert url.startswith(sq.base + "/checkout/") and url.endswith(order_id)
    assert body["order"]["metadata"] == {"apex": pay.MARKER, "website": SITE}   # no plan key: a checkup
    assert body["order"]["line_items"][0]["base_price_money"] == {"amount": 2900, "currency": "USD"}
    assert body["order"]["location_id"] == LOC and "quick_pay" not in body
    assert "subscription_plan_id" not in body["checkout_options"]
    assert sq.order_puts == []


def test_pro_checkout_prices_and_tags_plan(sq):
    _, order_id = pay.create_checkout(SITE, "https://site.example/report.html", plan="pro")
    body = sq.links[-1]
    assert body["order"]["metadata"] == {"apex": pay.MARKER, "website": SITE, "plan": "pro"}
    assert body["order"]["line_items"][0]["base_price_money"]["amount"] == 7900
    assert body["order"]["line_items"][0]["name"] == "Pro Checkup"
    assert sq.orders[order_id]["metadata"]["plan"] == "pro"


@pytest.mark.parametrize("plan", ["enterprise", "", None, "monitor"])
def test_unknown_or_unavailable_plan_never_reaches_square(sq, plan):
    with pytest.raises(pay.PaymentError):
        pay.create_checkout(SITE, "https://site.example/report.html", plan=plan)
    assert sq.links == []


def test_monitor_checkout_uses_subscription_link_then_tags_order(sq, var):
    url, order_id = pay.create_checkout(SITE, "https://site.example/report.html", plan="monitor")
    body = sq.links[-1]
    assert "order" not in body                                # Square: quick_pay only for subscriptions
    assert body["quick_pay"] == {"name": "Apex Forge Monthly Monitoring",
                                 "price_money": {"amount": 2900, "currency": "USD"}, "location_id": LOC}
    assert body["checkout_options"]["subscription_plan_id"] == var         # the VARIATION id
    assert body["checkout_options"]["redirect_url"] == "https://site.example/report.html"
    assert not any(k in repr(body) for k in ("card_number", "cvv", "source_id"))
    # the website + plan were written onto the link's order right away
    put_oid, put = sq.order_puts[-1]
    assert put_oid == order_id and put["order"]["version"] == 1 and put["idempotency_key"]
    assert sq.orders[order_id]["metadata"] == {"apex": pay.MARKER, "website": SITE, "plan": "monitor"}
    assert sq.link_plans[order_id] == var and url.endswith(order_id)


def test_monitor_checkout_refused_when_order_cant_be_tagged(sq, var):
    sq.fail_order_put = True
    with pytest.raises(pay.SquareUnavailable):
        pay.create_checkout(SITE, "https://site.example/report.html", plan="monitor")
    assert sq.deleted_links == ["PLINK%010d" % sq._n]         # the unusable link is taken down


def test_monitor_link_with_unknown_variation_fails_cleanly(sq, monkeypatch):
    monkeypatch.setenv("SQUARE_MONITOR_PLAN_VARIATION_ID", "CATVAR999999999999999999")
    with pytest.raises(pay.SquareUnavailable):
        pay.create_checkout(SITE, "https://site.example/report.html", plan="monitor")


# ------------------------------------------------------------------ paid orders

def test_price_check_uses_each_plans_price(sq, var):
    sq.add(oid("PROLOW"), SITE, plan="pro", amount=2900)
    sq.add(oid("PROOK"), SITE, plan="pro", amount=7900, customer_id="CUST000001")
    sq.add(oid("MONOK"), SITE, plan="monitor", amount=2900)
    sq.add(oid("MONLOW"), SITE, plan="monitor", amount=1900)      # the monthly price, but not the first month
    sq.add(oid("OLDCHK"), SITE, amount=2900)                          # no plan field
    sq.add(oid("WEIRD"), SITE, plan="platinum", amount=99900)
    with pytest.raises(pay.PaymentError):
        pay.get_paid_order(oid("PROLOW"))
    p = pay.get_paid_order(oid("PROOK"))
    assert p["plan"] == "pro" and p["customer_id"] == "CUST000001" and p["amount"] == 7900
    assert pay.get_paid_order(oid("MONOK"))["plan"] == "monitor"
    with pytest.raises(pay.PaymentError):
        pay.get_paid_order(oid("MONLOW"))
    old = pay.get_paid_order(oid("OLDCHK"))
    assert old["plan"] == "checkup" and old["customer_id"] == ""
    with pytest.raises(pay.PaymentError, match="not for this site"):
        pay.get_paid_order(oid("WEIRD"))


def test_customer_id_falls_back_to_the_order(sq):
    sq.add(oid("ORDCUST"), SITE)
    sq.orders[oid("ORDCUST")]["customer_id"] = "CUSTONORDER1"
    assert pay.get_paid_order(oid("ORDCUST"))["customer_id"] == "CUSTONORDER1"


def test_monitor_expiry_waived_only_while_subscription_active(sq, var):
    now = time.time()
    old = now - 40 * DAY
    for tag, cust, status in (("ACT", "CUSTACTIVE01", "ACTIVE"), ("CAN", "CUSTCANCEL01", "CANCELED"),
                              ("NOSUB", "CUSTNOSUB001", None)):
        sq.add(oid(tag), SITE, plan="monitor", amount=2900, created=old, customer_id=cust)
        if status:
            sq.subscribe("sub-" + tag.lower() + "-0001", cust, variation=var, status=status, created=old + 120)
    p = pay.get_paid_order(oid("ACT"))
    assert p["plan"] == "monitor"
    assert sq.orders[oid("ACT")]["metadata"]["apex_subscription_id"] == "sub-act-0001"   # pinned
    for tag in ("CAN", "NOSUB"):
        with pytest.raises(pay.PaymentError, match="expired"):
            pay.get_paid_order(oid(tag))
    sq.add(oid("OLDCHECK"), SITE, created=old, customer_id="CUSTACTIVE01")
    with pytest.raises(pay.PaymentError, match="expired"):       # one-time plans still expire
        pay.get_paid_order(oid("OLDCHECK"))


def test_subscription_for_order_pairs_by_customer_and_time(sq, var):
    t0 = time.time() - 10 * DAY
    cust = "CUSTTWOSITES"
    sq.add(oid("SITEA"), "https://a.example/", plan="monitor", amount=2900, created=t0, customer_id=cust)
    sq.add(oid("SITEB"), "https://b.example/", plan="monitor", amount=2900, created=t0 + 2 * DAY, customer_id=cust)
    sq.subscribe("sub-b-000001", cust, variation=var, created=t0 + 2 * DAY + 90)
    sq.subscribe("sub-a-000001", cust, variation=var, created=t0 + 60)
    sq.subscribe("sub-other-var", cust, variation="CATVAROTHER0000000000001", created=t0 + 61)
    sq.subscribe("sub-other-loc", cust, variation=var, location="LSOMEONEELSE01", created=t0 + 59)
    a = pay.subscription_for_order(pay.get_paid_order(oid("SITEA")))
    b = pay.subscription_for_order(pay.get_paid_order(oid("SITEB")))
    assert a["id"] == "sub-a-000001" and b["id"] == "sub-b-000001"
    assert set(a) == {"id", "status", "start_date", "charged_through_date", "canceled_date", "current"}
    assert a["status"] == "ACTIVE" and a["canceled_date"] is None and a["current"] is True
    # pinned on the order, and a pin wins over timing afterwards
    assert sq.orders[oid("SITEA")]["metadata"]["apex_subscription_id"] == "sub-a-000001"
    assert pay.subscription_for_order(pay.get_paid_order(oid("SITEA")))["id"] == "sub-a-000001"
    assert sq.sub_searches[-1]["query"]["filter"]["location_ids"] == [LOC]
    assert sq.sub_searches[-1]["query"]["filter"]["customer_ids"] == [cust]


def test_subscription_for_order_none_cases(sq, var, monkeypatch):
    sq.add(oid("CHK"), SITE, customer_id="CUSTX0000001")
    sq.add(oid("MONNOCUST"), SITE, plan="monitor", amount=2900)
    sq.add(oid("MONLATE"), SITE, plan="monitor", amount=2900, customer_id="CUSTLATE0001", created=time.time() - 20 * DAY)
    sq.subscribe("sub-late-0001", "CUSTLATE0001", variation=var, created=time.time())   # 20 days later: no pair
    assert pay.subscription_for_order(pay.get_paid_order(oid("CHK"))) is None
    assert pay.subscription_for_order(pay.get_paid_order(oid("MONNOCUST"))) is None
    assert pay.subscription_for_order(pay.get_paid_order(oid("MONLATE"))) is None
    assert pay.subscription_for_order(None) is None
    monkeypatch.delenv("SQUARE_MONITOR_PLAN_VARIATION_ID")
    assert pay.subscription_for_order({"plan": "monitor", "customer_id": "CUSTLATE0001", "order_id": oid("MONLATE")}) is None


def test_cancel_at_period_end(sq, var):
    sq.subscribe("sub-cancel-001", "CUSTC0000001", variation=var, charged_through_date="2026-12-14")
    out = pay.cancel_subscription("sub-cancel-001")
    assert out == {"status": "ACTIVE", "ends": "2026-12-14"}           # still ACTIVE until it ends
    assert sq.cancels == ["sub-cancel-001"]
    assert pay.cancel_subscription("sub-cancel-001") == out              # again: no second cancel call
    assert sq.cancels == ["sub-cancel-001"]
    sq.subscribe("sub-done-0001", "CUSTC0000001", variation=var, status="CANCELED", canceled_date="2026-11-01")
    assert pay.cancel_subscription("sub-done-0001") == {"status": "CANCELED", "ends": "2026-11-01"}
    assert sq.cancels == ["sub-cancel-001"]


def test_cancel_refuses_foreign_or_bad_ids(sq, var):
    sq.subscribe("sub-foreign-01", "CUSTF0000001", variation="CATVAROTHER0000000000001")
    sq.subscribe("sub-otherloc-1", "CUSTF0000001", variation=var, location="LSOMEONEELSE01")
    for sid in ("sub-foreign-01", "sub-otherloc-1", "sub-missing-01", "../etc", "", None, "a b c d e f g"):
        with pytest.raises(pay.PaymentError):
            pay.cancel_subscription(sid)
    assert sq.cancels == []


def test_monitored_sites(sq, var):
    t0 = time.time() - 5 * DAY
    sq.add(oid("MS1"), "https://one.example/", plan="monitor", amount=2900, created=t0,
           customer_id="CUSTMS000001", email="one@one.example")
    sq.subscribe("sub-ms1-00001", "CUSTMS000001", variation=var, created=t0 + 30)
    sq.add(oid("MS2"), "https://two.example/", plan="monitor", amount=2900, created=t0,
           customer_id="CUSTMS000002", email="")
    sq.add_customer("CUSTMS000002", "two@two.example")
    sq.subscribe("sub-ms2-00001", "CUSTMS000002", variation=var, created=t0 + 30)
    sq.add(oid("MS3"), "https://three.example/", plan="monitor", amount=2900, created=t0, customer_id="CUSTMS000003")
    sq.subscribe("sub-ms3-00001", "CUSTMS000003", variation=var, created=t0 + 30, status="CANCELED")
    sq.subscribe("sub-orphan-001", "CUSTMS000004", variation=var, created=t0)        # no order of ours
    sq.add(oid("MSCHK"), "https://chk.example/", created=t0, customer_id="CUSTMS000001")  # a checkup, not monitored
    sites = sorted(pay.monitored_sites(), key=lambda r: r["website"])
    assert [s["website"] for s in sites] == ["https://one.example/", "https://two.example/"]
    assert sites[0] == {"subscription_id": "sub-ms1-00001", "customer_id": "CUSTMS000001",
                        "order_id": oid("MS1"), "website": "https://one.example/", "email": "one@one.example",
                        "state": pay.empty_monitor_state()}
    assert sites[1]["email"] == "two@two.example"            # from the customer profile when the payment has none
    assert sq.order_searches[-1]["query"]["filter"]["customer_filter"]["customer_ids"]
    assert all(b["location_ids"] == [LOC] for b in sq.order_searches)


def test_monitored_sites_when_square_leaves_customer_off_the_order(sq, var):
    t0 = time.time() - 2 * DAY
    sq.add(oid("NOCUST"), "https://nocust.example/", plan="monitor", amount=2900, created=t0,
           customer_id="CUSTONPAYMT1", email="p@nocust.example")
    sq.add(oid("NEIGHBOR"), "https://neighbor.example/", plan="monitor", amount=2900, created=t0,
           customer_id="CUSTSOMEONE2")
    for o in (oid("NOCUST"), oid("NEIGHBOR")):
        del sq.orders[o]["customer_id"]                        # only the payment names the buyer
    sq.subscribe("sub-nocust-001", "CUSTONPAYMT1", variation=var, created=t0 + 45)
    sites = pay.monitored_sites()
    assert [(s["order_id"], s["subscription_id"], s["email"]) for s in sites] == [
        (oid("NOCUST"), "sub-nocust-001", "p@nocust.example")]


def test_monitored_sites_empty_without_plan(sq):
    assert pay.monitored_sites() == []


# ------------------------------------------------------------------ sales

def test_sales_summary_math(sq, var):
    now = time.time()
    sq.add(oid("S1"), "https://www.alpha.example/x", created=now - 0.5 * DAY, email="a@alpha.example")
    sq.add(oid("S2"), "https://beta.example/", plan="pro", amount=7900, created=now - 1.5 * DAY, refund_cents=7900)
    sq.add(oid("S3"), "https://gamma.example/", plan="monitor", amount=1900, created=now - 2.5 * DAY,
           customer_id="CUSTS3000001")
    sq.add(oid("S4"), "https://delta.example/", plan="pro", amount=7900, created=now - 3.5 * DAY, refund_cents=1000)
    sq.add(oid("S5"), "https://old.example/", created=now - 45 * DAY)                    # outside 30 days
    sq.add(oid("S6"), "https://unpaid.example/", paid=False, created=now - DAY)
    sq.add(oid("S7"), "https://other.example/", location="LSOMEONEELSE01", created=now - DAY)
    sq.add(oid("S8"), "https://foreign.example/", marker=None, created=now - DAY)        # not our order
    sq.add(oid("S9"), "https://euro.example/", currency="EUR", created=now - DAY)
    sq.subscribe("sub-s3-000001", "CUSTS3000001", variation=var)
    sq.subscribe("sub-s-sched01", "CUSTS0000002", variation=var, canceled_date="2026-12-20")   # active, ending
    sq.subscribe("sub-s-over001", "CUSTS0000003", variation=var, price_override=1500)
    sq.subscribe("sub-s-gone001", "CUSTS0000004", variation=var, status="CANCELED")
    sq.subscribe("sub-s-otherv1", "CUSTS0000005", variation="CATVAROTHER0000000000001")
    s = pay.sales_summary(days=30, now=now)
    assert s["days"] == 30 and s["orders"] == 4
    assert s["gross_cents"] == 2900 + 7900 + 1900 + 7900
    assert s["refunded_cents"] == 7900 + 1000
    assert s["net_cents"] == s["gross_cents"] - s["refunded_cents"] == 11700
    assert s["by_plan"] == {"checkup": {"count": 1, "gross_cents": 2900},
                            "pro": {"count": 2, "gross_cents": 15800},
                            "monitor": {"count": 1, "gross_cents": 1900}}
    assert s["active_subscriptions"] == 2 and s["cancelling"] == 1   # the one scheduled to end isn't "active"
    assert s["mrr_cents"] == 1900 + 1500                    # catalog's monthly price + an override
    assert s["truncated"] is False
    assert [r["site"] for r in s["recent"]] == ["alpha.example", "beta.example", "gamma.example", "delta.example"]
    assert s["recent"][0] == {"created": sq.payments["PAY" + oid("S1")[-20:]]["created_at"], "plan": "checkup",
                              "amount_cents": 2900, "site": "alpha.example", "refunded": False}
    assert [r["refunded"] for r in s["recent"]] == [False, True, False, True]
    assert "@" not in repr(s) and oid("S1") not in repr(s)          # no emails, no order ids
    assert len(sq.lists) >= 2                                       # followed the payments cursor
    q = sq.lists[0]
    assert q["location_id"] == [LOC] and q["begin_time"] and q["end_time"]
    s7 = pay.sales_summary(days=7, now=now)
    assert s7["orders"] == 4 and pay.sales_summary(days=2, now=now)["orders"] == 2
    assert pay.sales_summary(days=1, now=now)["by_plan"]["checkup"] == {"count": 1, "gross_cents": 2900}


def test_sales_summary_recent_capped_and_days_clamped(sq):
    now = time.time()
    for i in range(23):
        sq.add(oid("R%02d" % i), f"https://s{i}.example/", created=now - (i + 1) * 3600)
    s = pay.sales_summary(days=0, now=now)
    assert s["days"] == 1 and s["orders"] == 23 and len(s["recent"]) == 20
    assert s["recent"][0]["site"] == "s0.example" and s["mrr_cents"] == 0 and s["active_subscriptions"] == 0
    assert pay.sales_summary(days=5000, now=now)["days"] == 366


# ------------------------------------------------------------------ webhooks

def test_event_kind():
    o = "ORDWEBHOOK0000000001"
    pe = {"type": "payment.updated", "data": {"type": "payment", "object": {"payment": {"order_id": o, "status": "COMPLETED"}}}}
    assert pay.event_kind(pe) == ("payment_completed", o)
    assert pay.paid_order_id_from_event(pe) == o
    pe["data"]["object"]["payment"]["status"] = "APPROVED"
    assert pay.event_kind(pe) == (None, None)
    su = {"type": "subscription.updated", "data": {"type": "subscription", "id": "592b9720-d2ef-4ee4-b3fd-9d98e4f829d2",
                                                   "object": {"subscription": {"id": "592b9720-d2ef-4ee4-b3fd-9d98e4f829d2",
                                                                               "status": "ACTIVE"}}}}
    assert pay.event_kind(su) == ("subscription_changed", "592b9720-d2ef-4ee4-b3fd-9d98e4f829d2")
    sc = {"type": "subscription.created", "data": {"type": "subscription", "id": "sub-created-0001", "object": {}}}
    assert pay.event_kind(sc) == ("subscription_changed", "sub-created-0001")
    bad = {"type": "subscription.updated", "data": {"id": "../../x", "object": {"subscription": {"id": "../../x"}}}}
    for ev in (bad, {"type": "invoice.payment_made", "data": {"id": "inv-00000000001"}}, None, "x", [], {"type": 5}):
        assert pay.event_kind(ev) == (None, None)


# ------------------------------------------------------------------ CLI

def run_cli(*argv, stdin=""):
    out = io.StringIO()
    code = square_cli.main(list(argv), out=out, stdin=io.StringIO(stdin))
    return code, out.getvalue()


def test_cli_doctor_all_good(sq, var, monkeypatch):
    monkeypatch.setenv("SQUARE_WEBHOOK_SIGNATURE_KEY", SIG_KEY)
    monkeypatch.setenv("SQUARE_WEBHOOK_URL", "https://api.example/api/square/webhook")
    code, out = run_cli("doctor")
    assert code == 0, out
    assert "FAIL" not in out and "All good." in out
    assert "location found: Apex Forge Test" in out
    assert "Monthly Monitoring plan found: $29.00 for the first month, then $19.00 a month" in out
    assert TOKEN not in out and SIG_KEY not in out and var not in out and LOC not in out
    assert any(h[0] == "Bearer " + TOKEN for h in sq.headers_seen)


def test_cli_doctor_finds_problems(sq, monkeypatch):
    code, out = run_cli("doctor")                     # no plan, no webhook: warnings only
    assert code == 0 and "WARN SQUARE_MONITOR_PLAN_VARIATION_ID is not set" in out
    assert "WARN SQUARE_WEBHOOK_SIGNATURE_KEY is not set" in out
    monkeypatch.setenv("SQUARE_MONITOR_PLAN_VARIATION_ID", sq.add_plan_variation(2500))
    code, out = run_cli("doctor")
    assert code == 1 and "FAIL the plan variation charges 2500 cents a month after the first but " \
                         "MONITOR_PRICE_CENTS is 1900" in out
    monkeypatch.setenv("SQUARE_MONITOR_PLAN_VARIATION_ID", "CATVARNOPE00000000000001")
    code, out = run_cli("doctor")
    assert code == 1 and "is not a subscription plan variation" in out
    monkeypatch.setenv("SQUARE_LOCATION_ID", "LNOTMINE00001")
    code, out = run_cli("doctor")
    assert code == 1 and "not one of this account's locations" in out and "LNOTMINE00001" not in out
    monkeypatch.setenv("SQUARE_ACCESS_TOKEN", "EAAAwrong_token_value_123")
    code, out = run_cli("doctor")
    assert code == 1 and "Square refused the access token" in out and "EAAAwrong" not in out
    monkeypatch.delenv("SQUARE_ACCESS_TOKEN")
    code, out = run_cli("doctor")
    assert code == 1 and "FAIL SQUARE_ACCESS_TOKEN is not set" in out and "skipped the Square checks" in out


def test_cli_doctor_square_down(sq, monkeypatch):
    monkeypatch.setenv("SQUARE_API_BASE", "http://127.0.0.1:9")
    code, out = run_cli("doctor")
    assert code == 1 and "could not reach Square" in out and TOKEN not in out


def test_cli_plans(sq, var, monkeypatch):
    code, out = run_cli("plans")
    assert code == 0
    assert "checkup  $29.00" in out and "pro      $79.00" in out and "$19.00 a month" in out
    assert "hidden" not in out and var not in out
    monkeypatch.delenv("SQUARE_MONITOR_PLAN_VARIATION_ID")
    assert "hidden" in run_cli("plans")[1]


def test_cli_sales(sq, var):
    sq.add(oid("CS1"), "https://shop.example/", plan="pro", amount=7900, email="who@shop.example")
    code, out = run_cli("sales", "--days", "7")
    assert code == 0 and "Last 7 days" in out and "$79.00" in out and "shop.example" in out
    assert "who@shop.example" not in out and oid("CS1") not in out and TOKEN not in out
    assert run_cli("sales", "--days", "0")[0] == 2
    assert run_cli("sales", "--days", "x")[0] == 2


def test_cli_create_monitor_plan_sandbox(sq):
    code, out = run_cli("create-monitor-plan")
    assert code == 0, out
    var_id = out.strip().splitlines()[-1]
    assert pay.CATALOG_ID_RE.match(var_id) and sq.catalog[var_id]["type"] == "SUBSCRIPTION_PLAN_VARIATION"
    plan_body, var_body = sq.catalog_upserts
    assert plan_body["object"]["type"] == "SUBSCRIPTION_PLAN" and plan_body["object"]["id"].startswith("#")
    d = var_body["object"]["subscription_plan_variation_data"]
    assert sq.catalog[d["subscription_plan_id"]]["type"] == "SUBSCRIPTION_PLAN"
    assert d["phases"] == [
        {"cadence": "MONTHLY", "ordinal": 0, "periods": 1,
         "pricing": {"type": "STATIC", "price_money": {"amount": 2900, "currency": "USD"}}},
        {"cadence": "MONTHLY", "ordinal": 1,
         "pricing": {"type": "STATIC", "price_money": {"amount": 1900, "currency": "USD"}}}]
    assert plan_body["idempotency_key"] != var_body["idempotency_key"]
    assert TOKEN not in out and "sandbox" in out and "$29.00 for the first month, then $19.00 a month" in out


def test_cli_create_monitor_plan_refusals(sq, monkeypatch):
    monkeypatch.setenv("SQUARE_ENV", "production")
    code, out = run_cli("create-monitor-plan")
    assert code == 2 and "REFUSED" in out and sq.catalog_upserts == []
    code, out = run_cli("create-monitor-plan", "--production", stdin="no\n")
    assert code == 2 and "nothing was created" in out and sq.catalog_upserts == []
    code, out = run_cli("create-monitor-plan", "--production", stdin="YES please\n")
    assert code == 2 and sq.catalog_upserts == []
    code, out = run_cli("create-monitor-plan", "--production", stdin="")
    assert code == 2 and sq.catalog_upserts == []
    code, out = run_cli("create-monitor-plan", "--production", stdin="yes\n")
    assert code == 0 and "PRODUCTION" in out and len(sq.catalog_upserts) == 2
    monkeypatch.setenv("SQUARE_MONITOR_PLAN_VARIATION_ID", out.strip().splitlines()[-1])
    code, out = run_cli("create-monitor-plan", "--production", stdin="yes\n")
    assert code == 2 and "already set" in out and len(sq.catalog_upserts) == 2
    monkeypatch.setenv("SQUARE_ENV", "staging")
    assert run_cli("create-monitor-plan")[0] == 2


def test_cli_create_monitor_plan_custom_price_and_force(sq, monkeypatch):
    monkeypatch.setenv("MONITOR_PRICE_CENTS", "2400")
    monkeypatch.setenv("SQUARE_MONITOR_PLAN_VARIATION_ID", "CATVAROLD000000000000001")
    code, out = run_cli("create-monitor-plan", "--force")
    assert code == 0 and "$24.00 a month" in out
    phases = sq.catalog_upserts[-1]["object"]["subscription_plan_variation_data"]["phases"]
    assert [p["pricing"]["price_money"]["amount"] for p in phases] == [2900, 2400]


def test_cli_runs_as_a_script_without_secrets_in_output(sq, var):
    env = {"PATH": os.environ["PATH"], "SQUARE_ACCESS_TOKEN": TOKEN, "SQUARE_ENV": "sandbox",
           "SQUARE_LOCATION_ID": LOC, "SQUARE_API_BASE": sq.base, "SELFSERVE_TEST_ALLOW_LOOPBACK": "1",
           "SQUARE_MONITOR_PLAN_VARIATION_ID": var, "SQUARE_WEBHOOK_SIGNATURE_KEY": SIG_KEY}
    for args in (["doctor"], ["plans"], ["sales"]):
        r = subprocess.run([sys.executable, "-I", os.path.join(API, "square_cli.py"), *args], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        for secret in (TOKEN, SIG_KEY):
            assert secret not in r.stdout + r.stderr
    r = subprocess.run([sys.executable, "-I", os.path.join(API, "square_cli.py")], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2


def test_square_errors_are_logged_without_ids_or_tokens(sq, var, capsys):
    with pytest.raises(pay.PaymentError):
        pay.cancel_subscription("sub-not-there-01")
    with pytest.raises(pay.PaymentError):
        pay.get_paid_order(oid("NOPE"))
    err = capsys.readouterr().err
    assert "-> 404" in err and "sub-not-there-01" not in err and oid("NOPE") not in err and TOKEN not in err


# ------------------------------------------------------------------ review round 1 regressions

def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def _day(offset_days):
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + offset_days * DAY))


def _pins(sq):
    return [(o, b["order"]["metadata"]) for o, b in sq.order_puts if "apex_subscription_id" in b["order"]["metadata"]]


def test_two_links_paid_out_of_order_are_ambiguous_and_never_pinned(sq, var):
    """Buyer opens link A then link B, pays B first, then A, minutes apart: who is whose
    can't be told, so neither pairs, nothing is pinned, nothing is monitored."""
    t0 = time.time() - 2 * DAY
    cust = "CUSTOUTORDER1"
    sq.add(oid("LINKA"), "https://a.example/", plan="monitor", amount=2900, created=t0 + 720, customer_id=cust)
    sq.add(oid("LINKB"), "https://b.example/", plan="monitor", amount=2900, created=t0 + 600, customer_id=cust)
    sq.orders[oid("LINKA")]["created_at"] = _iso(t0)              # A's link was made first...
    sq.orders[oid("LINKB")]["created_at"] = _iso(t0 + 300)
    sq.subscribe("sub-b-out-0001", cust, variation=var, created=t0 + 605)     # ...but B was paid first
    sq.subscribe("sub-a-out-0001", cust, variation=var, created=t0 + 725)
    for tag in ("LINKA", "LINKB"):
        p = pay.get_paid_order(oid(tag))                            # the purchase itself is fine
        assert pay.subscription_for_order(p) is None
    assert pay.monitored_sites() == []
    assert _pins(sq) == []
    assert all("apex_subscription_id" not in sq.orders[oid(t)]["metadata"] for t in ("LINKA", "LINKB"))


def test_far_apart_purchases_pair_by_payment_time_not_order_time(sq, var):
    """The link order may be made long before the buyer pays: pairing goes by the payment."""
    t0 = time.time() - 3 * DAY
    cust = "CUSTPAYTIME01"
    sq.add(oid("SLOWPAY"), "https://slow.example/", plan="monitor", amount=2900, created=t0 + DAY, customer_id=cust)
    sq.orders[oid("SLOWPAY")]["created_at"] = _iso(t0)              # link opened a day before paying
    sq.subscribe("sub-slow-00001", cust, variation=var, created=t0 + DAY + 40)
    assert pay.subscription_for_order(pay.get_paid_order(oid("SLOWPAY")))["id"] == "sub-slow-00001"
    assert sq.orders[oid("SLOWPAY")]["metadata"]["apex_subscription_id"] == "sub-slow-00001"


def test_cancelled_old_site_keeps_its_own_subscription(sq, var):
    """Pairing looks at ALL of the customer's subscriptions, so an old order whose
    subscription was cancelled can't be left free to grab the new site's subscription."""
    t1 = time.time() - 10 * DAY
    cust = "CUSTOLDNEW001"
    sq.add(oid("OLDSITE"), "https://old.example/", plan="monitor", amount=2900, created=t1, customer_id=cust)
    sq.subscribe("sub-old-000001", cust, variation=var, created=t1 + 60, status="CANCELED",
                 canceled_date=_day(-1), charged_through_date=_day(-1))
    sq.orders[oid("OLDSITE")]["metadata"]["apex_subscription_id"] = "sub-old-000001"     # pinned earlier
    sq.add(oid("NEWSITE"), "https://new.example/", plan="monitor", amount=2900, created=t1 + 20 * 60,
           customer_id=cust)
    sq.subscribe("sub-new-000001", cust, variation=var, created=t1 + 21 * 60)
    sites = pay.monitored_sites()
    assert [(s["order_id"], s["subscription_id"], s["website"]) for s in sites] == [
        (oid("NEWSITE"), "sub-new-000001", "https://new.example/")]
    new = pay.subscription_for_order(pay.get_paid_order(oid("NEWSITE")))
    old = pay.subscription_for_order(pay.get_paid_order(oid("OLDSITE")))
    assert new["id"] == "sub-new-000001" and new["current"]
    assert old["id"] == "sub-old-000001" and old["status"] == "CANCELED" and not old["current"]


def test_active_but_not_charged_through_today_is_not_current(sq, var):
    now = time.time()
    assert pay.subscription_is_current({"status": "ACTIVE", "charged_through_date": _day(0)}, now)
    assert pay.subscription_is_current({"status": "ACTIVE", "charged_through_date": _day(5)}, now)
    for bad in ({"status": "ACTIVE", "charged_through_date": _day(-1)}, {"status": "ACTIVE"},
                {"status": "ACTIVE", "charged_through_date": "soon"}, {"status": "PAUSED", "charged_through_date": _day(5)},
                {"status": "CANCELED", "charged_through_date": _day(5)}, None, {}):
        assert not pay.subscription_is_current(bad, now)
    old = now - 40 * DAY
    cust = "CUSTUNPAID001"
    sq.add(oid("UNPAIDMON"), SITE, plan="monitor", amount=2900, created=old, customer_id=cust)
    sq.subscribe("sub-unpaid-001", cust, variation=var, created=old + 30, charged_through_date=_day(-3))
    with pytest.raises(pay.PaymentError, match="expired"):         # no 30-day waiver while unpaid
        pay.get_paid_order(oid("UNPAIDMON"))
    assert pay.monitored_sites() == []
    s = pay.sales_summary(days=30)
    assert s["active_subscriptions"] == 0 and s["mrr_cents"] == 0 and s["cancelling"] == 0


def test_mark_delivered_retries_once_after_a_version_bump(sq):
    sq.add(oid("BUMP"), SITE)
    p = pay.get_paid_order(oid("BUMP"))
    sq.bump_version(oid("BUMP"))                                   # someone wrote the order meanwhile
    assert pay.mark_delivered(p) is True
    assert sq.orders[oid("BUMP")]["metadata"]["apex_report_delivered"] == "1"
    assert p["delivered"] is True and p["order_version"] == sq.orders[oid("BUMP")]["version"]
    (_, body), = sq.order_puts
    assert f"-v{body['order']['version']}-" in body["idempotency_key"]     # the key names the version
    assert pay.mark_delivered(p) is True and len(sq.order_puts) == 1        # already done: no write
    assert pay.get_paid_order(oid("BUMP"))["delivered"] is True


def test_metadata_writes_give_up_quietly(sq, monkeypatch):
    sq.add(oid("STUCK"), SITE)
    p = pay.get_paid_order(oid("STUCK"))
    sq.fail_order_put = True
    assert pay.mark_delivered(p) is False and pay.mark_ready_mailed(p) is False
    assert pay.set_monitor_state(p, last=1.0, month="2026-12", failing=[], not_measured=[]) is False
    sq.fail_order_put = False
    real = pay._request

    def always_moved(method, path, body=None):                     # every PUT sees a newer version
        if method == "PUT":
            sq.bump_version(oid("STUCK"))
        return real(method, path, body)
    monkeypatch.setattr(pay, "_request", always_moved)
    assert pay.mark_delivered(dict(p)) is False                     # one retry, then give up
    sq.fail_paths["/v2/orders/"] = 503
    assert pay.mark_ready_mailed(dict(p)) is False                  # Square down: False, no raise
    assert "apex_report_delivered" not in sq.orders[oid("STUCK")]["metadata"]


def test_idempotency_key_reuse_with_a_new_body_is_refused_by_the_fake(sq):
    """The fake behaves like Square, so a fixed per-order key (the old bug) would be caught."""
    sq.add(oid("IDEM"), SITE)
    o = sq.orders[oid("IDEM")]
    first = {"idempotency_key": "same-key", "order": {"version": o["version"], "metadata": {"a": "1"}}}
    assert pay._request("PUT", "/v2/orders/" + oid("IDEM"), first)[0] == 200
    assert pay._request("PUT", "/v2/orders/" + oid("IDEM"), first)[0] == 200          # replay
    second = {"idempotency_key": "same-key", "order": {"version": o["version"], "metadata": {"b": "1"}}}
    code, out = pay._request("PUT", "/v2/orders/" + oid("IDEM"), second)
    assert code == 400 and pay._error_codes(out) == {"IDEMPOTENCY_KEY_REUSED"}


def test_ready_mailed_flag_round_trip(sq):
    sq.add(oid("MAILED"), SITE)
    p = pay.get_paid_order(oid("MAILED"))
    assert p["ready_mailed"] is False
    assert pay.mark_ready_mailed(p) is True and p["ready_mailed"] is True
    assert sq.orders[oid("MAILED")]["metadata"]["apex_ready_mailed"] == "1"
    n = len(sq.order_puts)
    assert pay.mark_ready_mailed(p) is True and len(sq.order_puts) == n
    again = pay.get_paid_order(oid("MAILED"))
    assert again["ready_mailed"] is True and again["delivered"] is False


def test_monitor_state_round_trip_on_the_order(sq, var):
    cust = "CUSTSTATE0001"
    t0 = time.time() - DAY
    sq.add(oid("STATE"), SITE, plan="monitor", amount=2900, created=t0, customer_id=cust)
    sq.subscribe("sub-state-0001", cust, variation=var, created=t0 + 20)
    p = pay.get_paid_order(oid("STATE"))
    assert p["monitor_state"] == {"last": None, "month": None, "failing": [], "not_measured": [], "truncated": False}
    assert pay.set_monitor_state(p, last=1795731516.7, month="2026-12", failing=["3.4", "1.2", "1.2"],
                                 not_measured=["6.6"]) is True
    assert sq.orders[oid("STATE")]["metadata"]["apex_mon"] == "t=1795731516;m=2026-12;f=1.2,3.4;n=6.6"
    want = {"last": 1795731516.0, "month": "2026-12", "failing": ["1.2", "3.4"], "not_measured": ["6.6"],
            "truncated": False}
    assert pay.get_paid_order(oid("STATE"))["monitor_state"] == want
    row, = pay.monitored_sites()
    assert row["state"] == want
    assert pay.set_monitor_state(row, last=1795800000, month="2027-01", failing=[], not_measured=[]) is True
    assert row["state"]["month"] == "2027-01"                     # a monitored_sites row is updated in place
    assert pay.get_paid_order(oid("STATE"))["monitor_state"]["failing"] == []


def test_monitor_state_truncates_to_255_and_ignores_garbage():
    failing = ["fail.check.%03d" % i for i in range(40)]
    nm = ["nm.%03d" % i for i in range(40)]
    v = pay.encode_monitor_state(last=1795731516, month="2026-12", failing=failing, not_measured=nm)
    assert len(v) <= 255 and v.endswith("x=1")
    st = pay.decode_monitor_state(v)
    assert st["truncated"] and st["last"] == 1795731516.0 and st["month"] == "2026-12"
    assert st["failing"] == sorted(failing)[:len(st["failing"])] and len(st["failing"]) >= 10
    assert set(st["not_measured"]) <= set(nm)
    small = pay.encode_monitor_state(last=None, month=None, failing=["a,b", "c;d", "ok-1", "x" * 41, 7], not_measured=None)
    assert small == "f=7,ok-1;n="                                  # unsafe ids are dropped, not mangled
    for junk in (None, 5, "", ";;;", "t=abc;m=2026-13;f=;n=", "t=-5", "zzz", "t=" + "9" * 20, "m=12-2026", "x" * 2000):
        st = pay.decode_monitor_state(junk)
        assert st["last"] is None and st["month"] is None and st["failing"] == [] and st["not_measured"] == []
    part = pay.decode_monitor_state("t=1700000000;m=bad;f=1.1,bad id;q=1")
    assert part["last"] == 1700000000.0 and part["month"] is None and part["failing"] == ["1.1"]
    assert pay.encode_monitor_state(last="nope", month=5, failing=[], not_measured=[]) == "f=;n="


def test_find_monitor_orders_has_no_30_day_limit(sq, var):
    now = time.time()
    cust, other = "CUSTFINDME001", "CUSTFINDME002"
    sq.add_customer(cust, "owner@shop.example")
    sq.add_customer(other, "someone@else.example")
    sq.add(oid("MONOLD"), "https://shop.example/", plan="monitor", amount=2900, created=now - 90 * DAY,
           customer_id=cust, email="owner@shop.example")
    sq.subscribe("sub-find-00001", cust, variation=var, created=now - 90 * DAY + 50)           # still current
    sq.add(oid("MONENDED"), "https://ended.example/", plan="monitor", amount=2900, created=now - 200 * DAY,
           customer_id=cust)
    sq.subscribe("sub-find-ended", cust, variation=var, created=now - 200 * DAY + 50, status="CANCELED",
                 charged_through_date=_day(-100), canceled_date=_day(-100))                  # long over
    sq.add(oid("MONREF"), "https://refunded.example/", plan="monitor", amount=2900, created=now - 3 * DAY,
           customer_id=cust, refund_cents=2900)
    sq.subscribe("sub-find-refnd", cust, variation=var, created=now - 3 * DAY + 50)
    sq.add(oid("NOTMINE"), "https://else.example/", plan="monitor", amount=2900, created=now - 50 * DAY,
           customer_id=other)
    sq.subscribe("sub-find-other", other, variation=var, created=now - 50 * DAY + 50)
    found = pay.find_monitor_orders("Owner@Shop.Example", now=now)       # typed with other capitals
    assert [f["order_id"] for f in found] == [oid("MONREF"), oid("MONOLD")]    # refunded kept: dashboard + cancel
    assert found[1]["website"] == "https://shop.example/" and set(found[1]) == {"order_id", "website", "created"}
    assert pay.find_orders("owner@shop.example", now=now) == []                # the 30-day finder can't see it
    assert any(b["query"]["filter"]["email_address"]["exact"] == "owner@shop.example" for b in sq.customer_searches)
    assert pay.find_monitor_orders("nobody@shop.example", now=now) == []
    with pytest.raises(pay.PaymentError):
        pay.find_monitor_orders("not an email")


def test_find_monitor_orders_without_a_plan(sq):
    sq.add_customer("CUSTNOPLAN001", "a@b.example")
    assert pay.find_monitor_orders("a@b.example") == [] and sq.customer_searches == []


def test_monitored_sites_skip_refunded_and_short_paid_orders(sq, var):
    t0 = time.time() - 4 * DAY
    for tag, cust, kw in (("OKAY", "CUSTSKIP00001", {}), ("REFUNDED", "CUSTSKIP00002", {"refund_cents": 2900}),
                          ("SHORT", "CUSTSKIP00003", {"amount": 1900})):
        sq.add(oid(tag), f"https://{tag.lower()}.example/", plan="monitor", created=t0, customer_id=cust,
               **({"amount": 2900} | kw))
        sq.subscribe("sub-skip-" + tag.lower(), cust, variation=var, created=t0 + 10)
    assert [s["order_id"] for s in pay.monitored_sites()] == [oid("OKAY")]


def test_doctor_checks_both_phase_prices(sq, monkeypatch):
    monkeypatch.setenv("SQUARE_MONITOR_PLAN_VARIATION_ID", sq.add_plan_variation(1900, first_cents=2500))
    code, out = run_cli("doctor")
    assert code == 1 and "FAIL the plan variation charges 2500 cents for the first month but " \
                         "MONITOR_FIRST_CENTS is 2900" in out
    monkeypatch.setenv("MONITOR_FIRST_CENTS", "2500")
    code, out = run_cli("doctor")
    assert code == 0 and "$25.00 for the first month, then $19.00 a month" in out
    assert "WARN the first month of monitoring costs less than a Website Checkup" in out
    monkeypatch.delenv("MONITOR_FIRST_CENTS")
    monkeypatch.setenv("SQUARE_MONITOR_PLAN_VARIATION_ID", sq.add_plan_variation(1900, phases=1))   # older plan
    code, out = run_cli("doctor")
    assert code == 1 and "isn't this version's two-phase plan" in out and "create-monitor-plan --force" in out


def test_fake_catalog_refuses_an_open_ended_first_phase(sq):
    """Square: 'subscription phase requires period unless it is last phase'."""
    plan = pay.create_monitor_plan()
    assert plan["first_cents"] == 2900 and plan["cents"] == 1900
    def ph(ordinal, cents):
        return {"cadence": "MONTHLY", "ordinal": ordinal,
                "pricing": {"type": "STATIC", "price_money": {"amount": cents, "currency": "USD"}}}
    bad = {"idempotency_key": "k-bad-1", "object": {
        "type": "SUBSCRIPTION_PLAN_VARIATION", "id": "#v",
        "subscription_plan_variation_data": {"name": "M", "subscription_plan_id": plan["plan_id"],
                                             "phases": [ph(0, 2900), ph(1, 1900)]}}}   # no periods on phase 0
    assert pay._request("POST", "/v2/catalog/object", bad)[0] == 400
    with pytest.raises(pay.PaymentError):
        pay.create_monitor_plan(first_cents=50)


def _fallback_world(sq, var, *, invoice_status="PAID", amount=2900, sub_kw=None, subscription_id=None,
                    second_sub=False, invoice=True):
    """A monitor link order with NO payment of its own; the first month was paid on the
    subscription's first invoice instead."""
    t0 = time.time() - 2 * DAY
    cust = "CUSTINVOICE01"
    sq.add(oid("NOPAYLINK"), "https://inv.example/", plan="monitor", paid=None, customer_id=cust)
    sq.orders[oid("NOPAYLINK")].update(created_at=_iso(t0 - 600), updated_at=_iso(t0))
    sub = sq.subscribe("sub-inv-000001", cust, variation=var, created=t0 + 30, **(sub_kw or {}))
    if second_sub:
        sq.subscribe("sub-inv-000002", cust, variation=var, created=t0 + 90)
    if invoice:
        sq.add_invoice(sub["id"], status=invoice_status, amount=amount, created=t0 + 31,
                       subscription_id=subscription_id)
        sq.add_invoice(sub["id"], status="UNPAID", amount=1900, created=t0 + 31 * DAY)    # a later month
    return oid("NOPAYLINK"), cust


def test_first_invoice_stands_in_for_a_missing_checkout_payment(sq, var):
    o, cust = _fallback_world(sq, var)
    p = pay.get_paid_order(o)
    assert p["paid_via"] == "invoice" and p["amount"] == 2900 and p["website"] == "https://inv.example/"
    assert p["payment_id"].startswith("PAY") and p["customer_id"] == cust and not p["refunded"]
    assert p["pinned_subscription"] == "sub-inv-000001"
    assert sq.orders[o]["metadata"]["apex_subscription_id"] == "sub-inv-000001"
    assert pay.subscription_for_order(p)["id"] == "sub-inv-000001"
    row, = pay.monitored_sites()
    assert row["order_id"] == o and row["subscription_id"] == "sub-inv-000001"


@pytest.mark.parametrize("case", ["unpaid", "other_sub", "too_little", "not_current", "ambiguous", "no_invoice"])
def test_invoice_fallback_refuses_anything_doubtful(sq, var, case):
    kw = {"unpaid": {"invoice_status": "UNPAID"}, "other_sub": {"subscription_id": "sub-someone-else"},
          "too_little": {"amount": 1900}, "not_current": {"sub_kw": {"charged_through_date": _day(-1)}},
          "ambiguous": {"second_sub": True}, "no_invoice": {"invoice": False}}[case]
    o, _ = _fallback_world(sq, var, **kw)
    with pytest.raises(pay.PaymentError, match="completed payment"):
        pay.get_paid_order(o)
    assert "apex_subscription_id" not in sq.orders[o]["metadata"]
    assert pay.monitored_sites() == []


def test_mrr_uses_the_plans_real_price_not_env(sq, var, monkeypatch):
    monkeypatch.setenv("MONITOR_PRICE_CENTS", "2400")               # env says $24, Square's plan says $19
    sq.subscribe("sub-mrr-000001", "CUSTMRR000001", variation=var)
    sq.subscribe("sub-mrr-000002", "CUSTMRR000002", variation=var, price_override=1200)
    assert pay.sales_summary()["mrr_cents"] == 1900 + 1200
    sq.catalog[var]["subscription_plan_variation_data"]["phases"] = []
    with pytest.raises(pay.SquareUnavailable, match="price"):
        pay.sales_summary()


def test_sales_truncated_flag_and_refunded_only_with_money(sq, monkeypatch):
    now = time.time()
    sq.add(oid("PENDREF"), "https://pend.example/", created=now - DAY)
    sq.payments["PAY" + oid("PENDREF")[-20:]]["refund_ids"] = ["RFPENDING"]   # refund asked for, no money back yet
    s = pay.sales_summary(days=7, now=now)
    assert s["truncated"] is False and s["refunded_cents"] == 0 and s["recent"][0]["refunded"] is False
    for i in range(8):
        sq.add(oid("TR%02d" % i), f"https://t{i}.example/", created=now - (i + 2) * 3600)
    monkeypatch.setattr(pay, "MAX_PAGES", 1)                        # page size 3: more pages than allowed
    s = pay.sales_summary(days=7, now=now)
    assert s["truncated"] is True
    code, out = run_cli("sales")
    assert code == 0 and "may be low" in out


def test_api_base_override_only_counts_in_test_mode_on_loopback(sq, monkeypatch):
    assert pay._cfg()[1] == sq.base                                 # test mode + loopback: the fake
    monkeypatch.setenv("SQUARE_API_BASE", "https://connect.squareup.com")
    assert pay._cfg()[1] == pay.BASES["sandbox"]                    # can't point a sandbox run at production
    monkeypatch.delenv("SELFSERVE_TEST_ALLOW_LOOPBACK")
    assert pay._cfg()[1] == pay.BASES["sandbox"]
    monkeypatch.setenv("SQUARE_API_BASE", "https://127.0.0.1:1")
    assert pay._cfg()[1] == pay.BASES["sandbox"]                    # outside test mode it's ignored entirely
    monkeypatch.setenv("SQUARE_API_BASE", sq.base)                  # plain http outside tests: refused, no call
    with pytest.raises(RuntimeError):
        pay._cfg()
    code, out = run_cli("doctor")
    assert code == 1 and "FAIL SQUARE_API_BASE is set to a plain-http address" in out and not sq.headers_seen
    monkeypatch.setenv("SQUARE_ENV", "production")
    monkeypatch.setenv("SQUARE_API_BASE", pay.BASES["sandbox"])
    assert pay._cfg()[1] == pay.BASES["production"]
    def offline(*a, **k):
        raise pay.SquareUnavailable("offline")
    monkeypatch.setattr(pay, "_request", offline)
    rows = pay.check_setup()
    assert ("WARN", "SQUARE_API_BASE is set but ignored (it's for tests); SQUARE_ENV picks Square's address") in rows
    assert ("FAIL", "could not reach Square (offline)") in rows


def test_create_monitor_plan_guard_cant_be_bypassed_with_api_base(sq, monkeypatch):
    """SQUARE_ENV=sandbox with SQUARE_API_BASE pointed at production must still talk to
    sandbox (here: our loopback fake is refused too, so nothing is created anywhere)."""
    monkeypatch.delenv("SELFSERVE_TEST_ALLOW_LOOPBACK")
    seen = []
    monkeypatch.setattr(pay, "_request", lambda m, path, body=None: seen.append(pay._cfg()[1]) or (500, {}))
    monkeypatch.setenv("SQUARE_API_BASE", pay.BASES["production"])
    code, out = run_cli("create-monitor-plan")
    assert code == 1 and seen == [pay.BASES["sandbox"]] and sq.catalog_upserts == []


@pytest.mark.parametrize("path", ["/v2/locations", "/v2/catalog/object/"])
def test_doctor_with_square_5xx_prints_fail_and_no_traceback(sq, var, path):
    sq.fail_paths[path] = 500
    code, out = run_cli("doctor")
    assert code == 1 and "FAIL" in out and "500" in out and "Traceback" not in out
    env = {"PATH": os.environ["PATH"], "SQUARE_ACCESS_TOKEN": TOKEN, "SQUARE_ENV": "sandbox",
           "SQUARE_LOCATION_ID": LOC, "SQUARE_API_BASE": sq.base, "SELFSERVE_TEST_ALLOW_LOOPBACK": "1",
           "SQUARE_MONITOR_PLAN_VARIATION_ID": var}
    r = subprocess.run([sys.executable, "-I", os.path.join(API, "square_cli.py"), "doctor"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "Traceback" not in r.stdout + r.stderr and "FAIL" in r.stdout


def test_doctor_survives_an_unreadable_answer(sq, var, monkeypatch):
    real = pay._request
    monkeypatch.setattr(pay, "_request", lambda m, p, b=None: (200, {"locations": "not a list"})
                        if p == "/v2/locations" else real(m, p, b))
    code, out = run_cli("doctor")
    assert code == 1 and "couldn't read" in out and "Traceback" not in out
