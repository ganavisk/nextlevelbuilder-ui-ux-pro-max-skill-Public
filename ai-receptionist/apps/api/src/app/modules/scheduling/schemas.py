"""Pydantic response shapes for the dashboard's appointment list/actions."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AppointmentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service: str
    starts_at: datetime
    ends_at: datetime
    status: str
    customer_name: str
    customer_phone: str
    customer_email: str | None
    timezone: str
    bay: int
    details: dict[str, Any]
    # True when the booking came from a Test Lab call (direction 'simulated').
    simulated: bool = False
