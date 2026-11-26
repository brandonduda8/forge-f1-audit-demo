"""mailer.py - the two transactional emails a buyer can get, as plain text (stdlib only).

Only two emails exist, and each goes only to the buyer's own email address as
Square recorded it at checkout:
  * report_ready    after a payment: your report is ready (report link + dashboard link)
  * monitor_report  after a monthly monitoring re-check: what changed (report link +
                    dashboard link, where monitoring can be cancelled)
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
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import quote

ADDR_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                     r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}$")
KINDS = ("report_ready", "monitor_report")

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


def compose(kind, to, *, order_id, site, plan="checkup", grade=None, score=None, changes=None):
    """(subject, body) for one of the two emails. Pure: no network."""
    if kind not in KINDS:
        raise MailError("unknown email kind")
    site = _site(site)
    report, dash = links(order_id)
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
            lines += ["",
                      "Monthly Monitoring renews every month at the price shown at checkout until you cancel.",
                      "Cancel online from your dashboard; it takes effect at the end of the paid month."]
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
    lines += ["", "Apex Forge Website Checkup",
              "You get this email because you bought a checkup with this address. We send only",
              "emails about your own purchase."]
    return _safe_line(subject, 150), "\n".join(lines) + "\n"


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
