"""Confirmation message bodies.

Pure rendering — no I/O — so the wording is cheap to test and review. Kept
deliberately plain: these are transactional messages, and a confirmation that
reads like marketing invites spam filtering and, for SMS, TCPA complaints.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str


def format_when(starts_at: datetime, timezone: str) -> str:
    local = starts_at.astimezone(ZoneInfo(timezone))
    minutes = f":{local.minute:02d}" if local.minute else ""
    hour = local.hour % 12 or 12
    meridiem = "am" if local.hour < 12 else "pm"
    return f"{local.strftime('%A, %B')} {local.day} at {hour}{minutes} {meridiem}"


def appointment_confirmation_email(
    *, business_name: str, customer_name: str, service: str, starts_at: datetime, timezone: str
) -> RenderedEmail:
    when = format_when(starts_at, timezone)
    greeting = f"Hi {customer_name}," if customer_name else "Hi,"
    service_line = f"<p><strong>{service}</strong></p>" if service else ""
    return RenderedEmail(
        subject=f"Your appointment with {business_name} — {when}",
        html=(
            f"<p>{greeting}</p>"
            f"<p>Your appointment with {business_name} is confirmed for "
            f"<strong>{when}</strong>.</p>"
            f"{service_line}"
            f"<p>If you need to change or cancel it, just reply to this email "
            f"or give us a call.</p>"
            f"<p>— {business_name}</p>"
        ),
    )


def appointment_confirmation_sms(
    *, business_name: str, service: str, starts_at: datetime, timezone: str
) -> str:
    when = format_when(starts_at, timezone)
    detail = f" ({service})" if service else ""
    # Kept under ~160 chars where possible to avoid multi-part billing.
    return (
        f"{business_name}: your appointment{detail} is confirmed for {when}. Reply STOP to opt out."
    )
