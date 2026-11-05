"""Email/SMS provider factories — the single place adapter selection happens."""

from app.core.config import Settings
from app.modules.notifications.providers.base import EmailProvider, SMSProvider
from app.modules.notifications.providers.resend import ResendEmailProvider
from app.modules.notifications.providers.twilio import TwilioSMSProvider


def get_email_provider(settings: Settings) -> EmailProvider:
    return ResendEmailProvider(settings)


def get_sms_provider(settings: Settings) -> SMSProvider:
    return TwilioSMSProvider(settings)
