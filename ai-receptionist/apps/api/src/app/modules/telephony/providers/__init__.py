"""Voice provider factory — the single place adapter selection happens."""

from app.core.config import Settings
from app.core.errors import AppError
from app.modules.telephony.providers.base import VoiceProvider
from app.modules.telephony.providers.vapi import VapiProvider


def get_voice_provider(settings: Settings) -> VoiceProvider:
    if settings.voice_provider == "vapi":
        return VapiProvider(settings)
    # "retell" adapter is the planned fallback (Phase 1, Risk R2).
    raise AppError(f"voice provider {settings.voice_provider!r} has no adapter yet")
