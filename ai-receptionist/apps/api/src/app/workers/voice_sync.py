"""Voice-sync jobs: push a tenant's changed settings to the voice vendor.

Queued by dashboard saves that change the agent (see
`modules/tenants/voice_sync.py`). Consumed by the CRM worker process
(`python -m app.workers.crm_sync`) on its own queue, so a deployment
needs no extra service for a job that runs a few times a month.

The handler is idempotent and coalescing: it always pushes the *current*
configuration, and skips the vendor entirely when an earlier job already
pushed the version it would.
"""

import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import AppError, PermanentIntegrationError
from app.core.logging import get_logger
from app.core.queue import DEFAULT_MAX_ATTEMPTS
from app.modules.telephony.providers.base import VoiceProvider
from app.modules.tenants.repository import get_tenant
from app.modules.tenants.voice_sync import (
    is_connected,
    load_numbers,
    mark_failed,
    mark_synced,
    push_agent,
)

logger = get_logger(__name__)


@dataclass
class Deps:
    settings: Settings
    voice: VoiceProvider


def _classify(exc: Exception) -> Exception:
    """Map a push failure to what the queue should do with it. A 4xx from the
    vendor (other than timeout/rate limit) means the payload is wrong and
    resending it won't help; our own AppError (e.g. a bad timezone) likewise."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if 400 <= status < 500 and status not in (408, 409, 429):
            return PermanentIntegrationError(f"voice vendor refused the agent (HTTP {status})")
    elif isinstance(exc, AppError) and not isinstance(exc, PermanentIntegrationError):
        return PermanentIntegrationError(str(exc))
    return exc


def _describe(exc: Exception) -> str:
    """A message safe and useful to show the business owner."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"The voice provider returned an error (HTTP {exc.response.status_code})."
    if isinstance(exc, httpx.TransportError):
        return "Couldn't reach the voice provider."
    if isinstance(exc, AppError):
        return str(exc)
    return f"Unexpected error ({type(exc).__name__})."


async def handle_voice_sync(job: dict[str, Any], *, deps: Deps) -> None:
    tenant_id = uuid.UUID(job["tenant_id"])
    attempts = int(job.get("attempts", 0))

    # The version is read before the agent is built (inside push_agent), so a
    # save landing mid-push is never recorded as live by this job.
    tenant = await get_tenant(tenant_id)
    version = tenant.voice_sync_requested
    if tenant.voice_sync_done >= version:
        logger.info("voice_sync_already_live", tenant_id=str(tenant_id), version=version)
        return
    numbers = await load_numbers(tenant_id)
    if not is_connected(numbers):
        # Provisioned later by `make provision`, which records the version.
        logger.info("voice_sync_not_connected", tenant_id=str(tenant_id))
        return

    try:
        await push_agent(tenant, numbers, deps.voice, vendor=deps.settings.voice_provider)
    except Exception as exc:
        classified = _classify(exc)
        is_last = (
            isinstance(classified, PermanentIntegrationError)
            or attempts + 1 >= DEFAULT_MAX_ATTEMPTS
        )
        if is_last:
            # The queue never tells the handler a job was dead-lettered, so
            # this attempt records the terminal state the dashboard shows.
            await mark_failed(tenant_id, version, _describe(exc))
        logger.warning(
            "voice_sync_failed",
            tenant_id=str(tenant_id),
            version=version,
            attempt=attempts + 1,
            final=is_last,
            error=str(exc)[:200],
        )
        if classified is not exc:
            raise classified from exc
        raise
    await mark_synced(tenant_id, version)
    logger.info("voice_sync_live", tenant_id=str(tenant_id), version=version)
