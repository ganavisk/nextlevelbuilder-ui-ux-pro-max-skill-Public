"""Dashboard KPI rollups (FR-11: call volume, outcomes, booking rate,
escalation rate, sentiment).

Deliberately no new event-ingestion pipeline or rollup table — every number
here is a read-side aggregate over `calls`, which the telephony/post-call
modules already write during a normal call. A rollup table becomes worth
the write-side complexity if these queries ever show up in a slow-query log;
nothing about the schema or this module's shape would need to change to add
one later, since callers only ever see `AnalyticsSummary`.

Rates are computed against *classified* calls, not all calls in the window —
a call the post-call worker hasn't reached yet has `outcome IS NULL`, and
counting it in the denominator would silently understate every rate for the
last few minutes of the window, worst right after a burst of call volume.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.db import tenant_session
from app.modules.telephony.models import Call


@dataclass(frozen=True)
class DailyVolume:
    date: str  # ISO date (UTC day boundary — see module docstring)
    count: int


@dataclass(frozen=True)
class AnalyticsSummary:
    window_days: int
    total_calls: int
    classified_calls: int
    calls_by_outcome: dict[str, int]
    calls_by_sentiment: dict[str, int]
    booking_rate: float
    escalation_rate: float
    daily_call_volume: list[DailyVolume]


async def get_summary(tenant_id: uuid.UUID, *, days: int = 30) -> AnalyticsSummary:
    window_start = datetime.now(UTC) - timedelta(days=days)

    async with tenant_session(tenant_id) as session:
        # Test-lab calls are rehearsals, not customer demand.
        base_filter = (
            Call.tenant_id == tenant_id,
            Call.started_at >= window_start,
            Call.direction != "simulated",
        )

        total_calls = (
            await session.execute(select(func.count()).select_from(Call).where(*base_filter))
        ).scalar_one()

        outcome_rows = (
            await session.execute(
                select(Call.outcome, func.count())
                .where(*base_filter, Call.outcome.is_not(None))
                .group_by(Call.outcome)
            )
        ).all()
        calls_by_outcome: dict[str, int] = {str(k): v for k, v in outcome_rows}
        classified_calls = sum(calls_by_outcome.values())

        sentiment_rows = (
            await session.execute(
                select(Call.sentiment, func.count())
                .where(*base_filter, Call.sentiment.is_not(None))
                .group_by(Call.sentiment)
            )
        ).all()
        calls_by_sentiment: dict[str, int] = {str(k): v for k, v in sentiment_rows}

        # Grouped in UTC, not the tenant's local day — a caller near
        # midnight can land on the "wrong" local day in the chart. Simple
        # and honest about it rather than silently wrong; tenant-local
        # bucketing is a documented future improvement (see README).
        day = func.date_trunc("day", Call.started_at)
        volume_rows = (
            await session.execute(
                select(day.label("day"), func.count())
                .where(*base_filter)
                .group_by(day)
                .order_by(day)
            )
        ).all()

    booked = calls_by_outcome.get("appointment_booked", 0)
    escalated = calls_by_outcome.get("callback_requested", 0)
    booking_rate = booked / classified_calls if classified_calls else 0.0
    escalation_rate = escalated / classified_calls if classified_calls else 0.0

    return AnalyticsSummary(
        window_days=days,
        total_calls=total_calls,
        classified_calls=classified_calls,
        calls_by_outcome=calls_by_outcome,
        calls_by_sentiment=calls_by_sentiment,
        booking_rate=round(booking_rate, 4),
        escalation_rate=round(escalation_rate, 4),
        daily_call_volume=[
            DailyVolume(date=row.day.date().isoformat(), count=row[1]) for row in volume_rows
        ],
    )
