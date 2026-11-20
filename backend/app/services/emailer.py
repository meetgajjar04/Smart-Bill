"""Native e-mail delivery for deadline reminders and unusual-bill alerts.

Any SMTP account works (Gmail with an app password, Outlook, a company relay, ...).
The feature is entirely optional: when SMTP is not configured, sending is skipped and
the rest of the app keeps working with in-app alerts only.
"""
import logging
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage
from typing import Optional

from ..config import settings

log = logging.getLogger("smartbill.email")

BRAND = "Smart Bill AI"


def _from_address() -> str:
    return (settings.smtp_from or settings.smtp_username or "").strip()


def _money(bill, places: int = 2) -> str:
    """Legacy rows can have a NULL amount; never crash an e-mail on one."""
    if bill.amount is None:
        return "amount not recorded"
    return f"₹{float(bill.amount):,.{places}f}"


def send(to: str, subject: str, body: str) -> bool:
    """Send a plain-text e-mail. Returns False (and logs) when it cannot be delivered."""
    if not settings.email_enabled:
        log.info("e-mail skipped: EMAIL_ENABLED=false")
        return False
    if not settings.smtp_ready:
        log.info("e-mail skipped: SMTP is not configured (set SMTP_HOST and SMTP_FROM/SMTP_USERNAME)")
        return False
    sender = _from_address()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(body)

    try:
        if settings.smtp_ssl:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                  timeout=20, context=ssl.create_default_context()) as smtp:
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
                smtp.ehlo()
                if settings.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(msg)
    except Exception as e:  # SMTP failures must never break bill processing
        log.warning("e-mail to %s failed: %s", to, e)
        return False
    log.info("e-mail sent to %s: %s", to, subject)
    return True


def send_test(user) -> bool:
    return send(user.email, f"{BRAND}: test e-mail",
                f"Hi {user.name},\n\n"
                f"This is a test from {BRAND}. If you can read it, deadline reminders and "
                f"unusual-bill alerts will reach you here.\n\n"
                f"Open the app: {settings.app_url}\n")


def send_due_reminder(user, bill) -> bool:
    provider = bill.provider or bill.category.title()
    when = bill.due_date.strftime("%d %b %Y") if bill.due_date else "soon"
    days = (bill.due_date - date.today()).days if bill.due_date else None
    lead = ("tomorrow" if days == 1 else "today" if days == 0
            else f"in {days} days" if days and days > 1 else "soon")
    account = f"\nAccount: {bill.account_number}" if bill.account_number else ""
    body = (
        f"Hi {user.name},\n\n"
        f"Your {provider} bill of {_money(bill)} is due {lead} ({when}).{account}\n\n"
        f"Pay it here or mark it as paid once it is done:\n{settings.app_url}/bills\n\n"
        f"You are receiving this because deadline reminders are enabled in {BRAND}. "
        f"You can turn them off in Settings.\n"
    )
    return send(user.email, f"Reminder: {provider} bill of {_money(bill, 0)} is due {lead}", body)


def send_anomaly_alert(user, bill, result: dict) -> bool:
    provider = bill.provider or bill.category.title()
    summary: Optional[str] = (result or {}).get("summary")
    tip = (result or {}).get("top_tip")
    due = f"\nDue date: {bill.due_date.strftime('%d %b %Y')}" if bill.due_date else ""
    body = (
        f"Hi {user.name},\n\n"
        f"We spotted an unusual {bill.category} bill: {provider}, {_money(bill)}.{due}\n\n"
        f"{summary or 'The amount is noticeably higher than your usual pattern.'}\n\n"
        + (f"Suggested action: {tip}\n\n" if tip else "")
        + f"See the details and your options:\n{settings.app_url}/bills\n\n"
        f"You can turn unusual-bill e-mails off in Settings.\n"
    )
    return send(user.email, f"Unusual bill alert: {provider} {_money(bill, 0)}", body)
