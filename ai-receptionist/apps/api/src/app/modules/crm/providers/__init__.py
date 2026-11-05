"""CRM provider factory — the single place adapter selection happens."""

from app.core.config import Settings
from app.modules.crm.providers.base import CRMProvider
from app.modules.crm.providers.hubspot import HubSpotCRMProvider


def get_crm_provider(settings: Settings) -> CRMProvider:
    # Pipedrive/Zoho are the planned follow-on adapters (Phase 1 §5.1); they
    # slot in behind the same interface with no change above this line.
    return HubSpotCRMProvider(settings)
