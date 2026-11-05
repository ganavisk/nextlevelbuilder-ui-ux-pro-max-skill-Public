"""Pydantic response shape for the dashboard's "CRM sync needs attention"
view — the reason `crm.service.failed_syncs()` exists (Phase 6)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CrmSyncSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    call_id: UUID | None
    provider: str
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
