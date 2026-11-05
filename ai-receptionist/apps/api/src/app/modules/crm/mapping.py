"""Per-tenant field mapping: our normalized records → the tenant's CRM fields.

Pure functions, no I/O, so the mapping rules are cheap to test and to reason
about when a practice complains a field landed in the wrong place.

Why this is configuration rather than code: a dental practice wants the
qualification answer "are you in pain?" on a custom `urgency` property, a law
firm wants "matter type" on `case_type`, and neither should require a deploy.
Mapping lives in `tenants.settings.crm`:

    crm:
      deal_pipeline: default
      stage_by_outcome:
        appointment_booked: appointmentscheduled
        lead_qualified: qualifiedtobuy
      contact_properties:          # qualification key -> CRM property name
        pain: urgency
        insurance: insurance_provider
      deal_title: "{service} — {name}"

Unmapped qualification answers are deliberately dropped rather than guessed
into arbitrary property names: writing to a property the tenant never created
fails the whole sync in HubSpot, and silently inventing fields in someone's
CRM is worse than omitting them.
"""

from typing import Any

from app.modules.conversation.models import Lead
from app.modules.crm.providers.base import ContactRecord, DealRecord
from app.modules.tenants.models import Tenant

DEFAULT_STAGE = "appointmentscheduled"
# HubSpot's own default pipeline id; overridden per tenant.
DEFAULT_PIPELINE = "default"


def crm_config(tenant: Tenant) -> dict[str, Any]:
    return (tenant.settings or {}).get("crm", {}) or {}


def contact_properties(tenant: Tenant, lead: Lead) -> dict[str, Any]:
    """Map qualification answers onto the tenant's configured CRM properties.

    Only explicitly mapped keys are sent — see the module docstring for why.
    """
    mapping = crm_config(tenant).get("contact_properties", {}) or {}
    answers = lead.qualification or {}
    properties: dict[str, Any] = {}
    for answer_key, crm_property in mapping.items():
        if answer_key in answers and answers[answer_key] is not None:
            properties[str(crm_property)] = _stringify(answers[answer_key])
    return properties


def _stringify(value: Any) -> str:
    """CRMs store custom properties as strings. Booleans need care: HubSpot
    reads 'True'/'False' as plain text on a text property but expects
    lowercase on a boolean one, and lowercase is right in both cases."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_contact(tenant: Tenant, lead: Lead) -> ContactRecord:
    return ContactRecord(
        phone=lead.phone,
        name=lead.name,
        email=lead.email,
        properties=contact_properties(tenant, lead),
    )


def deal_stage(tenant: Tenant, outcome: str | None) -> str:
    """Pick the pipeline stage from the call's outcome, per tenant config."""
    stages = crm_config(tenant).get("stage_by_outcome", {}) or {}
    return str(stages.get(outcome or "", stages.get("default", DEFAULT_STAGE)))


def deal_pipeline(tenant: Tenant) -> str:
    return str(crm_config(tenant).get("deal_pipeline", DEFAULT_PIPELINE))


def deal_title(tenant: Tenant, *, name: str | None, service: str) -> str:
    """Render the tenant's deal-title template.

    A template referencing a field we don't have falls back to the default
    rather than raising — a misconfigured title must not cost the practice
    the whole CRM record.
    """
    template = crm_config(tenant).get("deal_title")
    values = {"name": name or "Unknown caller", "service": service or "Enquiry"}
    if template:
        try:
            return str(template).format(**values)
        except (KeyError, IndexError):
            pass
    return f"{values['service']} — {values['name']}"


def build_deal(
    tenant: Tenant,
    *,
    contact_external_id: str,
    lead: Lead,
    outcome: str | None,
    service: str = "",
) -> DealRecord:
    return DealRecord(
        contact_external_id=contact_external_id,
        title=deal_title(tenant, name=lead.name, service=service),
        stage=deal_stage(tenant, outcome),
        properties={"pipeline": deal_pipeline(tenant)},
    )


def should_create_deal(tenant: Tenant, lead: Lead, outcome: str | None) -> bool:
    """Not every call deserves a deal. By default only a booked appointment
    or a qualified lead does — creating one per call would flood the
    practice's pipeline with wrong numbers and hang-ups.

    `deal_on_outcomes: []` in tenant config disables deals entirely for
    practices that only want contacts and timeline activity.
    """
    configured = crm_config(tenant).get("deal_on_outcomes")
    if configured is not None:
        return (outcome or "") in set(configured)
    min_score = int(crm_config(tenant).get("deal_min_score", 0))
    if outcome == "appointment_booked":
        return True
    return outcome == "lead_qualified" and (lead.score or 0) >= min_score
