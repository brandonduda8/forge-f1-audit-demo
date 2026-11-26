"""mailer.py - the three transactional emails a buyer can get, as plain text (stdlib only).

Only these exist, and each goes only to the buyer's own email address as Square
recorded it at checkout:
  * report_ready    after a payment: your report is ready (report link + dashboard link;
                    for Monthly Monitoring also the renewal terms, so the buyer keeps a copy)
  * monitor_report  after a monthly monitoring re-check: what changed (report link +
                    dashboard link, where monitoring can be cancelled)
  * find_links      the buyer asked "find my report": the links for their purchases
                    (sent to the address that paid, never shown on the page)
No marketing, no lists, no other recipients.

Environment:
  MAIL_BACKEND   "off" (default: nothing is sent and nothing is logged) or "smtp"
  SMTP_HOST      the provider's SMTP server
  SMTP_PORT      default 587. The connection must offer STARTTLS; if it doesn't,
                 nothing is sent (no plain-text login, no plain-text mail).
  SMTP_USER, SMTP_PASSWORD   login (optional if the server needs none)
  MAIL_FROM      the sender address, e.g. checkup@yourdomain.example
  MAIL_REPLY_TO  optional reply-to address
  SITE_ORIGIN    the static site, used for the links in the email (required to send)

send(kind, to, **ctx) never raises: it returns True when the server accepted the
email and False otherwise. Failures are logged by error type only, never with
an address, an order id or a password.
"""
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import quote

ADDR_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                     r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}$")
KINDS = ("report_ready", "monitor_report", "find_links")
FIND_MAX = 5

CAPTURED = []          # MAIL_BACKEND=capture (tests only, needs SELFSERVE_TEST_ALLOW_LOOPBACK=1)


class MailError(Exception):
    pass


def backend():
    b = os.environ.get("MAIL_BACKEND", "off").strip().lower()
    if b == "capture" and os.environ.get("SELFSERVE_TEST_ALLOW_LOOPBACK") == "1":
        return "capture"
    return "smtp" if b == "smtp" else "off"


def origin():
    o = os.environ.get("SITE_ORIGIN", "").strip().rstrip("/")
    return o if re.match(r"^https?://[A-Za-z0-9.:-]+$", o) else ""


def enabled():
    """True only when an email could really go out (backend on, sender and links configured)."""
    b = backend()
    if b == "off" or not origin() or not valid_address(os.environ.get("MAIL_FROM", "")):
        return False
    return b == "capture" or bool(os.environ.get("SMTP_HOST", "").strip())


def valid_address(a):
    return isinstance(a, str) and len(a) <= 254 and bool(ADDR_RE.match(a))


def _safe_line(s, limit=200):
    """One line of text: control characters (CR, LF, tabs...) become spaces."""
    return re.sub(r"[\x00-\x1f\x7f  \u0085]+", " ", str(s or "")).strip()[:limit]


def _site(s):
    s = str(s or "").strip().lower()
    return s if HOST_RE.match(s) else "your website"


def links(order_id):
    o, q = origin(), quote(order_id, safe="")
    return o + "/report.html?order_id=" + q, o + "/dashboard.html?order_id=" + q


def money(cents):
    try:
        c = int(cents)
    except (TypeError, ValueError):
        return ""
    return "$%d" % (c // 100) if c % 100 == 0 else "$%d.%02d" % (c // 100, c % 100)


def renewal_terms(terms):
    """The Monthly Monitoring terms, in plain words (also what the buyer keeps in the email)."""
    terms = terms or {}
    first, monthly = money(terms.get("first_cents")), money(terms.get("cents"))
    if first and monthly:
        price = f"{first} for the first month (it includes this full report), then {monthly} a month."
    else:
        price = "the price shown at checkout."
    return ["Your Monthly Monitoring plan: " + price,
            "It renews every month until you cancel, and we re-check your website each month.",
            "Cancel online any time from your dashboard (link above). Cancelling takes effect",
            "at the end of the month you have already paid for; there is no charge after that."]


def compose(kind, to, *, order_id=None, site=None, plan="checkup", grade=None, score=None, changes=None,
            terms=None, items=None):
    """(subject, body) for one of the emails. Pure: no network."""
    if kind not in KINDS:
        raise MailError("unknown email kind")
    if kind == "find_links":
        return _compose_find(items)
    site = _site(site)
    report, dash = links(order_id or "")
    if kind == "report_ready":
        subject = f"Your website checkup for {site} is ready"
        lines = [
            "Hello,",
            "",
            f"Thank you for your purchase. Your website checkup for {site} is ready:",
            report,
            "",
            "Your dashboard shows your plan, the check history and, for monitoring, lets you",
            "manage or cancel the plan at any time:",
            dash,
            "",
            "These links work only for this purchase, so keep this email.",
        ]
        if plan == "monitor":
            lines += [""] + renewal_terms(terms)
    else:
        subject = f"Monthly check of {site}: " + _headline(changes, grade, score)
        lines = ["Hello,", "", f"We re-checked {site} as part of your Monthly Monitoring plan."]
        if grade is not None and score is not None:
            lines.append(f"Grade now: {_safe_line(grade, 3)} (score {int(score)} out of 100).")
        lines.append("")
        lines += _change_lines(changes)
        lines += ["",
                  "The full, updated report:",
                  report,
                  "",
                  "Manage or cancel your monitoring plan (cancelling takes effect at the end of the paid month):",
                  dash]
    lines += _footer()
    return _safe_line(subject, 150), "\n".join(lines) + "\n"


def _footer():
    return ["", "Apex Forge Website Checkup",
            "You get this email because you bought a checkup with this address. We send only",
            "emails about your own purchase."]


def _compose_find(items):
    items = [i for i in (items or []) if isinstance(i, dict) and i.get("order_id")][:FIND_MAX]
    if not items:
        raise MailError("nothing to send")
    lines = ["Hello,", "", "Someone (we hope you) asked us to resend the links for your website checkup."
             " Here they are:", ""]
    for i in items:
        report, dash = links(i["order_id"])
        when = ""
        if isinstance(i.get("created"), (int, float)):
            when = " (bought " + datetime.fromtimestamp(i["created"], timezone.utc).strftime("%Y-%m-%d") + ")"
        lines += [_site(i.get("site")) + when, "  Report: " + report, "  Dashboard (plan, history, cancel): " + dash, ""]
    lines += ["If you didn't ask for this, you can ignore this email: the links were sent only to you."]
    lines += _footer()
    return "Your Apex Forge checkup links", "\n".join(lines) + "\n"


def _headline(changes, grade, score):
    if not changes:
        return "your updated report"
    new, fixed = len(changes.get("new") or []), len(changes.get("fixed") or [])
    parts = []
    if new:
        parts.append(f"{new} new problem" + ("s" if new != 1 else ""))
    if fixed:
        parts.append(f"{fixed} fixed")
    return ", ".join(parts) if parts else "no changes"


def _change_lines(changes):
    if not changes:
        return ["This is the first monitoring check we have on file for this plan, so there is",
                "nothing to compare with yet. Next month's email will show what changed."]
    out = []
    b, a = changes.get("score_before"), changes.get("score_after")
    if b is not None and a is not None:
        d = int(a) - int(b)
        out.append(f"Score: {int(b)} -> {int(a)} ({'+' if d > 0 else ''}{d}).")
    new, fixed = changes.get("new") or [], changes.get("fixed") or []
    if new:
        out.append(f"New problems ({len(new)}):")
        out += ["  - " + _safe_line(p.get("title") or p.get("id"), 160) for p in new[:10]]
        if len(new) > 10:
            out.append(f"  ...and {len(new) - 10} more in the report.")
    if fixed:
        out.append(f"Fixed since last time ({len(fixed)}):")
        out += ["  - " + _safe_line(p.get("title") or p.get("id"), 160) for p in fixed[:10]]
        if len(fixed) > 10:
            out.append(f"  ...and {len(fixed) - 10} more.")
    if not new and not fixed:
        out.append("No new problems and nothing newly fixed since the last check.")
    return out


def build_message(kind, to, **ctx):
    sender = os.environ.get("MAIL_FROM", "").strip()
    reply_to = os.environ.get("MAIL_REPLY_TO", "").strip()
    for a in (to, sender) + ((reply_to,) if reply_to else ()):
        if not valid_address(a):
            raise MailError("invalid address")
    subject, body = compose(kind, to, **ctx)
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[1])
    msg["Auto-Submitted"] = "auto-generated"
    # plain 7-bit text when possible, so links stay readable and unbroken in every client
    msg.set_content(body, cte="7bit" if body.isascii() else "quoted-printable")
    return msg


def _tls_context():
    return ssl.create_default_context()


def _smtp_send(msg):
    host = os.environ.get("SMTP_HOST", "").strip()
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    user, password = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    with smtplib.SMTP(host, port, timeout=20) as s:
        s.ehlo()
        if not s.has_extn("starttls"):
            raise MailError("the mail server did not offer STARTTLS")
        s.starttls(context=_tls_context())
        s.ehlo()
        if user:
            s.login(user, password)
        s.send_message(msg)


def send(kind, to, **ctx):
    """True when the email was accepted. Never raises."""
    if not enabled():
        return False
    try:
        msg = build_message(kind, to, **ctx)
        if backend() == "capture":
            CAPTURED.append(msg)
        else:
            _smtp_send(msg)
        return True
    except Exception as e:       # an email must never break a request or a run
        sys.stderr.write(f"mail {kind} not sent: {type(e).__name__}\n")
        return False
