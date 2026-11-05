"""Dashboard-facing view over `crm.service.failed_syncs` — the reason that
function exists (Phase 6 doc §7): the queue's own dead-letter list lives in
Redis, invisible outside the worker process, so this is what actually lets
a human see what needs attention."""

from fastapi import APIRouter, Depends, Query

from app.core.auth import AuthContext, get_current_user
from app.modules.crm.schemas import CrmSyncSummary
from app.modules.crm.service import failed_syncs

router = APIRouter()


@router.get("/crm/failed-syncs", response_model=list[CrmSyncSummary])
async def list_failed_syncs(
    auth: AuthContext = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[CrmSyncSummary]:
    rows = await failed_syncs(auth.tenant_id, limit=limit)
    return [CrmSyncSummary.model_validate(row) for row in rows]
