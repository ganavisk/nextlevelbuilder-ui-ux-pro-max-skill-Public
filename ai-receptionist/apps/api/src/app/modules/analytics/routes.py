"""Dashboard KPI endpoint (FR-11)."""

from fastapi import APIRouter, Depends, Query

from app.core.auth import AuthContext, get_current_user
from app.modules.analytics.schemas import AnalyticsSummaryOut, DailyVolumeOut
from app.modules.analytics.service import get_summary

router = APIRouter()


@router.get("/analytics/summary", response_model=AnalyticsSummaryOut)
async def analytics_summary(
    auth: AuthContext = Depends(get_current_user),
    days: int = Query(default=30, ge=1, le=365),
) -> AnalyticsSummaryOut:
    summary = await get_summary(auth.tenant_id, days=days)
    return AnalyticsSummaryOut(
        window_days=summary.window_days,
        total_calls=summary.total_calls,
        classified_calls=summary.classified_calls,
        calls_by_outcome=summary.calls_by_outcome,
        calls_by_sentiment=summary.calls_by_sentiment,
        booking_rate=summary.booking_rate,
        escalation_rate=summary.escalation_rate,
        daily_call_volume=[
            DailyVolumeOut(date=d.date, count=d.count) for d in summary.daily_call_volume
        ],
    )
