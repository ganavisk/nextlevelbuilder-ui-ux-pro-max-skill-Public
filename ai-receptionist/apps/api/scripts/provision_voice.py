"""Connect a tenant to the voice vendor: push its agent config and point its
phone number at it.

This is the link between the database and the phone network. Until it runs,
a tenant exists in our schema but no phone call can reach it.

Usage:
    python scripts/provision_voice.py --slug bright-smile-dental

Idempotent. After this first run, dashboard settings changes are pushed
automatically (see app/modules/tenants/voice_sync.py); re-run it by hand only
for changes made outside the dashboard (a YAML re-seed, a code change to the
prompt or tools) or to repair numbers on the vendor side. The vendor-side ids
are stored on `phone_numbers`, so re-running updates the existing agent rather
than creating a duplicate.
"""

import argparse
import asyncio

from sqlalchemy import select

import app.models  # noqa: F401 — registers every ORM model on Base.metadata
from app.core.config import get_settings
from app.core.db import admin_session
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.modules.telephony.providers import get_voice_provider
from app.modules.tenants.models import Tenant
from app.modules.tenants.voice_sync import load_numbers, mark_synced, push_agent

logger = get_logger(__name__)


async def provision(slug: str) -> None:
    settings = get_settings()
    if not settings.public_webhook_base_url:
        raise AppError(
            "PUBLIC_WEBHOOK_BASE_URL is not set — the vendor needs a reachable "
            "URL to send call and tool-call webhooks to"
        )

    async with admin_session() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()
        if tenant is None:
            raise AppError(f"no tenant with slug {slug!r} — run `make seed` first")
        # Detach so the instance stays usable after the session closes.
        session.expunge(tenant)

    numbers = await load_numbers(tenant.id)
    if not numbers:
        raise AppError(f"tenant {slug!r} has no phone number configured")

    # Read before pushing, like the voice-sync job: a dashboard save that
    # lands mid-push stays "updating" and its own job pushes it.
    version = tenant.voice_sync_requested
    await push_agent(
        tenant,
        numbers,
        get_voice_provider(settings),
        vendor=settings.voice_provider,
        attach_all=True,
    )
    await mark_synced(tenant.id, version)

    logger.info(
        "provisioned",
        slug=slug,
        webhook_url=f"{settings.public_webhook_base_url.rstrip('/')}/webhooks/voice/vapi",
        numbers=[n.e164 for n in numbers],
    )


if __name__ == "__main__":
    configure_logging("INFO", json_output=False)
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True, help="tenant slug, e.g. bright-smile-dental")
    args = parser.parse_args()
    asyncio.run(provision(args.slug))
