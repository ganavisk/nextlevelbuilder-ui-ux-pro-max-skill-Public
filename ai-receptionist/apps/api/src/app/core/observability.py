"""Error reporting (Sentry), configured for a product whose data is phone
calls: transcripts, caller numbers, customer emails, login tokens.

Layered, because any single layer will eventually miss something:
  1. Don't collect it — no request bodies, headers, cookies or query strings
     (the OAuth callback's query carries an authorization code), no per-frame
     local variables (the SDK's default, and the quickest way a transcript or
     password ends up in a stack trace), no user object.
  2. Scrub what is collected — phone numbers, emails, bearer tokens and JWTs
     are masked in every string of the event, since exception messages and
     log breadcrumbs are free text.
  3. Don't report expected failures — a 401 or 404 is the API working.

A no-op unless SENTRY_DSN is set, so development and tests never phone home.
"""

import logging
import os
import re
from typing import Any, cast

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.types import Event, Hint

from app.core.config import Settings
from app.core.errors import AppError

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}"), "[jwt]"),
    (re.compile(r"(?i)bearer\s+[\w.~+/-]+=*"), "Bearer [token]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\+\d[\d\s().-]{6,17}\d"), "[phone]"),
    (re.compile(r"\(?\b\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b"), "[phone]"),
)

_DROP_REQUEST_KEYS = ("data", "cookies", "headers", "query_string", "env")


def scrub_text(value: str) -> str:
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _scrub(node: Any) -> Any:
    if isinstance(node, str):
        return scrub_text(node)
    if isinstance(node, dict):
        return {k: _scrub(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub(v) for v in node]
    if isinstance(node, tuple):
        return tuple(_scrub(v) for v in node)
    return node


def before_send(event: Event, hint: Hint) -> Event | None:
    exc_info = hint.get("exc_info")
    if exc_info:
        exc = exc_info[1]
        if isinstance(exc, AppError) and exc.status_code < 500:
            return None  # an expected, handled outcome (bad login, not found, throttled)

    request = event.get("request")
    if isinstance(request, dict):
        for key in _DROP_REQUEST_KEYS:
            request.pop(key, None)
    event.pop("user", None)
    return cast(Event, _scrub(event))


def init_sentry(settings: Settings, *, service: str, transport: Any = None) -> bool:
    """Start error reporting for this process. Returns whether it is active."""
    if not settings.sentry_dsn:
        return False
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        release=settings.app_release
        or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or os.environ.get("GIT_SHA")
        or None,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        before_send=before_send,
        # structlog writes through stdlib logging; ERROR-level records become
        # events, lower levels are not kept as breadcrumbs (their text is the
        # most likely place for a stray number or name).
        integrations=[LoggingIntegration(level=None, event_level=logging.ERROR)],
        transport=transport,
    )
    sentry_sdk.set_tag("service", service)
    return True
