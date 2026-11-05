"""Seed a demo tenant from a config/tenants YAML file.

Usage:
    python scripts/seed_demo.py ../../config/tenants/example-dental.yaml \
        --number +15550100001

Idempotent: re-running updates nothing if the slug already exists.

Knowledge-base entries in the YAML's `knowledge:` section are embedded via
Voyage and ready for RAG immediately — this step is skipped (not failed)
when VOYAGE_API_KEY isn't configured, so the tenant is still usable for
qualification/callback testing without live embedding credentials.
"""

import argparse
import asyncio
import uuid
from pathlib import Path

import yaml
from sqlalchemy import select

import app.models  # noqa: F401 — registers every ORM model on Base.metadata
from app.core.config import get_settings
from app.core.db import tenant_session
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.modules.conversation.knowledge import ingest_document
from app.modules.conversation.models import KnowledgeDoc
from app.modules.conversation.providers import get_embedding_provider
from app.modules.tenants.models import AgentConfig, PhoneNumber, Tenant, User

logger = get_logger(__name__)


async def seed(config_path: Path, number: str) -> uuid.UUID | None:
    """Onboard the tenant described by `config_path`. Returns its id, or None
    if a tenant with that slug already exists.

    Everything but knowledge embedding happens in ONE transaction: the tenant
    row, agent config, phone number, owner login and knowledge docs commit
    together or not at all. (They used to be two transactions, and a failure
    in the second left a tenant with no agent config or number that a re-run
    then skipped as "exists".) The id is generated here so the transaction can
    run tenant-scoped from the start; `tenants` itself has no RLS.
    """
    doc = yaml.safe_load(config_path.read_text())
    tenant_cfg, agent_cfg = doc["tenant"], doc["agent"]
    tenant_id = uuid.uuid4()
    knowledge_doc_ids = []
    dashboard_cfg = doc.get("dashboard", {})

    async with tenant_session(tenant_id) as session:
        existing = (
            await session.execute(select(Tenant).where(Tenant.slug == tenant_cfg["slug"]))
        ).scalar_one_or_none()
        if existing:
            logger.info("tenant_exists", slug=tenant_cfg["slug"], tenant_id=str(existing.id))
            return None
        tenant = Tenant(
            id=tenant_id,
            slug=tenant_cfg["slug"],
            name=tenant_cfg["name"],
            vertical=tenant_cfg["vertical"],
            timezone=tenant_cfg.get("timezone", "America/New_York"),
            business_hours=tenant_cfg.get("business_hours", {}),
            # `settings` is the per-tenant policy blob the modules read at
            # runtime: service durations and notice period (scheduling),
            # which confirmations to send (notifications), field mapping and
            # deal rules (crm).
            settings={
                "forbidden_topics": agent_cfg.get("forbidden_topics", []),
                "scheduling": doc.get("scheduling", {}),
                "notifications": doc.get("notifications", {}),
                "crm": doc.get("crm", {}),
            },
        )
        session.add(tenant)
        await session.flush()
        session.add(
            AgentConfig(
                tenant_id=tenant_id,
                version=1,
                is_active=True,
                system_prompt=agent_cfg["persona"],
                first_message=agent_cfg["first_message"],
                voice_id=agent_cfg.get("voice_id", ""),
                voice_provider=agent_cfg.get("voice_provider", ""),
                voice_model=agent_cfg.get("voice_model", ""),
                language=agent_cfg.get("language", "en"),
                qualification=doc.get("qualification", []),
                escalation_policy=doc.get("escalation", {"mode": "voicemail_callback"}),
            )
        )
        session.add(PhoneNumber(tenant_id=tenant_id, e164=number))

        if dashboard_cfg.get("owner_email"):
            session.add(
                User(
                    tenant_id=tenant_id,
                    email=dashboard_cfg["owner_email"],
                    display_name=dashboard_cfg.get("owner_name", ""),
                    role="owner",
                    password_hash=hash_password(dashboard_cfg["owner_password"]),
                )
            )

        for item in doc.get("knowledge", []):
            knowledge_doc = KnowledgeDoc(
                tenant_id=tenant_id, title=item["title"], content=item["content"]
            )
            session.add(knowledge_doc)
            await session.flush()
            knowledge_doc_ids.append(knowledge_doc.id)

    logger.info("tenant_seeded", slug=tenant_cfg["slug"], tenant_id=str(tenant_id), number=number)
    if dashboard_cfg.get("owner_email"):
        logger.info(
            "dashboard_login_created",
            slug=tenant_cfg["slug"],
            email=dashboard_cfg["owner_email"],
            note="demo password from the tenant YAML — never do this for a real tenant",
        )

    if not knowledge_doc_ids:
        return tenant_id
    if not get_settings().voyage_api_key:
        logger.warning(
            "knowledge_ingestion_skipped",
            reason="VOYAGE_API_KEY not configured",
            docs=len(knowledge_doc_ids),
        )
        return tenant_id

    embeddings = get_embedding_provider(get_settings())
    for knowledge_doc_id in knowledge_doc_ids:
        chunk_count = await ingest_document(tenant_id, knowledge_doc_id, embeddings)
        logger.info("knowledge_doc_ingested", doc_id=str(knowledge_doc_id), chunks=chunk_count)
    return tenant_id


if __name__ == "__main__":
    configure_logging("INFO", json_output=False)
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--number", default="+15550100001")
    args = parser.parse_args()
    asyncio.run(seed(args.config, args.number))
