"""Exogenous information events for sequential planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import AvailabilityWindow, FamilyRequest


@dataclass(frozen=True)
class ExogenousEvent:
    id: str
    observed_at: datetime
    source_ref: str | None = None


@dataclass(frozen=True)
class WorkerUnavailableEvent(ExogenousEvent):
    worker_id: str = ""
    unavailable_window: AvailabilityWindow | None = None
    reason: str = ""


@dataclass(frozen=True)
class FamilyRequestArrivedEvent(ExogenousEvent):
    request: FamilyRequest | None = None


@dataclass(frozen=True)
class ServiceUserProfileStaleEvent(ExogenousEvent):
    service_user_id: str = ""
    reason: str = ""
