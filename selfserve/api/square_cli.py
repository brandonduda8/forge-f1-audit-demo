"""square_cli.py - the owner's command line for the Square side of Apex Forge.

Run it on your own computer with the server's settings exported in the terminal
(see selfserve/SQUARE-KIT.md):

  python selfserve/api/square_cli.py doctor                 check the settings and the Square account
  python selfserve/api/square_cli.py plans                  the plans and prices the site offers
  python selfserve/api/square_cli.py sales [--days N]       sales from Square for the last N days (default 30)
  python selfserve/api/square_cli.py create-monitor-plan    make the monitoring plan in Square's catalog
                                                            (first month MONITOR_FIRST_CENTS, then
                                                            MONITOR_PRICE_CENTS every month)
        [--production]                                      (needed, plus typing yes, when SQUARE_ENV=production)
        [--force]                                           (make another even if one is already set)

It reads settings from environment variables only and never prints a token or key.
Exit code: 0 fine, 1 a check failed, 2 refused or bad usage.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import square_api as pay  # noqa: E402


def _money(c):
    return f"${c / 100:,.2f}"


def _monitor_price(first, monthly):
    return f"{_money(first)} for the first month, then {_money(monthly)} a month"


def cmd_doctor(_args, out):
    try:
        rows = pay.check_setup()
    except Exception as e:                     # check_setup never raises; this is a last guard
        rows = [("FAIL", f"the check stopped unexpectedly ({type(e).__name__})")]
    for status, msg in rows:
        print(f"{status:<4} {msg}", file=out)
    failed = any(s == "FAIL" for s, _ in rows)
    print("\nAll good." if not failed else "\nFix the FAIL lines above, then run doctor again.", file=out)
    return 1 if failed else 0


def cmd_plans(_args, out):
    for p in pay.public_plans():
        price = (_monitor_price(p["first_cents"], p["cents"]) if p["kind"] == "subscription"
                 else _money(p["cents"]))
        state = "" if p["available"] else "   (hidden: SQUARE_MONITOR_PLAN_VARIATION_ID is not set)"
        print(f"{p['id']:<8} {price:<16} {p['label']}{state}", file=out)
    return 0


def cmd_sales(args, out):
    try:
        s = pay.sales_summary(days=args.days)
    except pay.SquareUnavailable as e:
        print(f"FAIL could not read sales from Square ({e})", file=out)
        return 1
    print(f"Last {s['days']} days (read from Square)", file=out)
    if s.get("truncated"):
        print("  NOTE Square had more records than this command reads, so these numbers may be low", file=out)
    print(f"  orders           {s['orders']}", file=out)
    print(f"  gross            {_money(s['gross_cents'])}", file=out)
    print(f"  refunded         {_money(s['refunded_cents'])}", file=out)
    print(f"  net              {_money(s['net_cents'])}", file=out)
    print(f"  active monitors  {s['active_subscriptions']}", file=out)
    print(f"  cancelling       {s.get('cancelling', 0)}  (paid up, ending at the end of their month)", file=out)
    print(f"  monthly recurring {_money(s['mrr_cents'])}", file=out)
    for plan, v in s["by_plan"].items():
        print(f"  {plan:<8} {v['count']:>4} sold  {_money(v['gross_cents'])}", file=out)
    if s["recent"]:
        print("Recent:", file=out)
        for r in s["recent"]:
            mark = "  refunded" if r["refunded"] else ""
            print(f"  {r['created'][:10]}  {r['plan']:<8} {_money(r['amount_cents']):>9}  {r['site']}{mark}", file=out)
    return 0


def cmd_create_monitor_plan(args, out, stdin):
    env = pay.env_name()
    if env is None:
        print("REFUSED SQUARE_ENV must be sandbox or production", file=out)
        return 2
    if env == "production":
        if not args.production:
            print("REFUSED SQUARE_ENV is production. Try it in sandbox first; to really create the plan in "
                  "production run again with --production.", file=out)
            return 2
        m = pay.plan_info("monitor")
        print(f"This creates a real plan ({_monitor_price(m['first_cents'], m['cents'])}) in your "
              "PRODUCTION Square account.", file=out)
        print("Type yes to go ahead: ", end="", file=out)
        out.flush()
        answer = (stdin.readline() or "").strip()
        if answer != "yes":
            print("\nREFUSED nothing was created.", file=out)
            return 2
    if os.environ.get("SQUARE_MONITOR_PLAN_VARIATION_ID", "").strip() and not args.force:
        print("REFUSED SQUARE_MONITOR_PLAN_VARIATION_ID is already set. Run doctor to check it, "
              "or add --force to make another plan.", file=out)
        return 2
    try:
        made = pay.create_monitor_plan()
    except (pay.SquareUnavailable, pay.PaymentError) as e:
        print(f"FAIL {e}", file=out)
        return 1
    print(f"Created the Monthly Monitoring plan in Square {env}: "
          f"{_monitor_price(made['first_cents'], made['cents'])}.", file=out)
    print("Paste this into Render as SQUARE_MONITOR_PLAN_VARIATION_ID:", file=out)
    print(made["variation_id"], file=out)
    return 0


def main(argv=None, out=None, stdin=None):
    out = out or sys.stdout
    stdin = stdin or sys.stdin
    ap = argparse.ArgumentParser(prog="square_cli.py", description="Square tools for Apex Forge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check settings and the Square account")
    sub.add_parser("plans", help="list plans and prices")
    s = sub.add_parser("sales", help="sales summary from Square")
    s.add_argument("--days", type=int, default=30)
    c = sub.add_parser("create-monitor-plan", help="create the monthly plan in Square's catalog")
    c.add_argument("--production", action="store_true")
    c.add_argument("--force", action="store_true")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if args.cmd == "doctor":
        return cmd_doctor(args, out)
    if args.cmd == "plans":
        return cmd_plans(args, out)
    if args.cmd == "sales":
        if not 1 <= args.days <= 366:
            print("REFUSED --days must be 1 to 366", file=out)
            return 2
        return cmd_sales(args, out)
    return cmd_create_monitor_plan(args, out, stdin)


if __name__ == "__main__":
    sys.exit(main())
