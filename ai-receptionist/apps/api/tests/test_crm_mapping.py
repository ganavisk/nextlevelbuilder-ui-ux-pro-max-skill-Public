"""CRM field mapping. Pure functions, no I/O — the tenant-configurable rules
that decide what lands where in someone's CRM, and whether a call earns a
deal at all.
"""

import uuid

from app.modules.conversation.models import Lead
from app.modules.crm.mapping import (
    build_contact,
    build_deal,
    contact_properties,
    deal_pipeline,
    deal_stage,
    deal_title,
    should_create_deal,
)
from app.modules.tenants.models import Tenant

CRM_SETTINGS = {
    "crm": {
        "deal_pipeline": "sales",
        "deal_title": "{service} — {name}",
        "stage_by_outcome": {
            "appointment_booked": "appointmentscheduled",
            "lead_qualified": "qualifiedtobuy",
            "default": "new",
        },
        "contact_properties": {
            "pain": "urgency",
            "insurance": "insurance_provider",
        },
        "deal_min_score": 30,
    }
}


def _tenant(settings=None) -> Tenant:
    return Tenant(
        id=uuid.uuid4(),
        slug="mapping-test",
        name="Bright Smile Dental",
        vertical="dental",
        timezone="America/New_York",
        business_hours={},
        settings=settings if settings is not None else CRM_SETTINGS,
        status="active",
    )


def _lead(**overrides) -> Lead:
    base = dict(
        tenant_id=uuid.uuid4(),
        call_id=uuid.uuid4(),
        name="Dana Lee",
        phone="+15550001111",
        email="dana@example.com",
        score=0,
        qualification={},
        status="new",
    )
    return Lead(**{**base, **overrides})


# ── contact properties ──────────────────────────────────────────────────────


def test_only_explicitly_mapped_answers_are_sent() -> None:
    """An unmapped answer must be dropped, not guessed into a property name —
    writing to a property the tenant never created fails the whole sync."""
    lead = _lead(qualification={"pain": True, "reason": "toothache", "insurance": "Delta"})
    properties = contact_properties(_tenant(), lead)
    assert properties == {"urgency": "true", "insurance_provider": "Delta"}
    assert "reason" not in properties


def test_boolean_answers_become_lowercase_strings() -> None:
    lead = _lead(qualification={"pain": False})
    assert contact_properties(_tenant(), lead) == {"urgency": "false"}


def test_none_answers_are_omitted_not_sent_as_the_string_none() -> None:
    lead = _lead(qualification={"pain": None, "insurance": "Cigna"})
    properties = contact_properties(_tenant(), lead)
    assert "urgency" not in properties
    assert properties["insurance_provider"] == "Cigna"


def test_no_crm_config_produces_no_properties() -> None:
    lead = _lead(qualification={"pain": True})
    assert contact_properties(_tenant(settings={}), lead) == {}


def test_build_contact_carries_identity_and_properties() -> None:
    lead = _lead(qualification={"pain": True})
    contact = build_contact(_tenant(), lead)
    assert contact.phone == "+15550001111"
    assert contact.name == "Dana Lee"
    assert contact.email == "dana@example.com"
    assert contact.properties == {"urgency": "true"}


# ── deal stage / pipeline / title ───────────────────────────────────────────


def test_stage_follows_the_call_outcome() -> None:
    tenant = _tenant()
    assert deal_stage(tenant, "appointment_booked") == "appointmentscheduled"
    assert deal_stage(tenant, "lead_qualified") == "qualifiedtobuy"


def test_unmapped_outcome_falls_back_to_configured_default() -> None:
    assert deal_stage(_tenant(), "voicemail") == "new"


def test_unmapped_outcome_falls_back_to_builtin_default_when_tenant_has_none() -> None:
    assert deal_stage(_tenant(settings={}), "anything") == "appointmentscheduled"


def test_pipeline_is_tenant_configured() -> None:
    assert deal_pipeline(_tenant()) == "sales"
    assert deal_pipeline(_tenant(settings={})) == "default"


def test_deal_title_renders_the_tenant_template() -> None:
    title = deal_title(_tenant(), name="Dana Lee", service="Cleaning")
    assert title == "Cleaning — Dana Lee"


def test_deal_title_falls_back_when_template_references_unknown_field() -> None:
    """A misconfigured title must not cost the practice the whole record."""
    broken = _tenant(settings={"crm": {"deal_title": "{missing_field}"}})
    title = deal_title(broken, name="Dana", service="Cleaning")
    assert title == "Cleaning — Dana"


def test_deal_title_default_uses_placeholder_name_when_caller_gave_none() -> None:
    title = deal_title(_tenant(settings={}), name=None, service="")
    assert title == "Enquiry — Unknown caller"


def test_build_deal_associates_the_given_contact_id() -> None:
    lead = _lead()
    deal = build_deal(
        _tenant(),
        contact_external_id="contact-123",
        lead=lead,
        outcome="appointment_booked",
        service="Cleaning",
    )
    assert deal.contact_external_id == "contact-123"
    assert deal.stage == "appointmentscheduled"
    assert deal.properties["pipeline"] == "sales"


# ── should_create_deal ──────────────────────────────────────────────────────


def test_booked_appointment_always_earns_a_deal() -> None:
    lead = _lead(score=0)
    assert should_create_deal(_tenant(), lead, "appointment_booked") is True


def test_qualified_lead_earns_a_deal_only_above_the_score_floor() -> None:
    tenant = _tenant()  # deal_min_score: 30
    assert should_create_deal(tenant, _lead(score=10), "lead_qualified") is False
    assert should_create_deal(tenant, _lead(score=30), "lead_qualified") is True


def test_a_plain_faq_call_does_not_earn_a_deal() -> None:
    assert should_create_deal(_tenant(), _lead(score=100), "answered_faq") is False


def test_explicit_outcome_list_overrides_the_score_heuristic() -> None:
    """A tenant that only wants deals on bookings, never on qualified leads
    regardless of score, must be able to say so."""
    tenant = _tenant(settings={"crm": {"deal_on_outcomes": ["appointment_booked"]}})
    assert should_create_deal(tenant, _lead(score=100), "lead_qualified") is False
    assert should_create_deal(tenant, _lead(score=0), "appointment_booked") is True


def test_empty_outcome_list_disables_deals_entirely() -> None:
    tenant = _tenant(settings={"crm": {"deal_on_outcomes": []}})
    assert should_create_deal(tenant, _lead(score=100), "appointment_booked") is False
