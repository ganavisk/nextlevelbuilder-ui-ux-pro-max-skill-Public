"""Pydantic response shape for the dashboard's KPI summary."""

from pydantic import BaseModel


class DailyVolumeOut(BaseModel):
    date: str
    count: int


class AnalyticsSummaryOut(BaseModel):
    window_days: int
    total_calls: int
    classified_calls: int
    calls_by_outcome: dict[str, int]
    calls_by_sentiment: dict[str, int]
    booking_rate: float
    escalation_rate: float
    daily_call_volume: list[DailyVolumeOut]
