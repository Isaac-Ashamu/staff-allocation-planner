"""Domain model for the sequential booking recommender.

These dataclasses are intentionally tenant-agnostic. Import adapters should map
company-specific spreadsheets, calendars, emails, and booking-system exports
into these objects before scoring or optimization runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping, Sequence


class TravelMode(str, Enum):
    DRIVE = "drive"
    BUS = "bus"
    TRAM = "tram"
    TRAIN = "train"
    PUBLIC_TRANSPORT = "public_transport"
    CYCLE = "cycle"
    WALK = "walk"
    TAXI = "taxi"


@dataclass(frozen=True)
class TenantRules:
    tenant_id: str
    min_travel_buffer_minutes: int = 15
    max_planned_travel_minutes: int = 90
    require_human_approval: bool = True
    allow_missing_worker_availability: bool = False
    scoring_weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "explicit_compatible": 40.0,
            "family_requested": 35.0,
            "continuity": 25.0,
            "travel_fit": 25.0,
            "postcode_area_match": 8.0,
            "skill_risk_fit": 20.0,
            "worker_preference": 8.0,
            "load_balance": 10.0,
            "reliability": 5.0,
            "date_request_match": 15.0,
            "uncertainty_penalty": -20.0,
        }
    )


@dataclass(frozen=True)
class Location:
    label: str
    postcode: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    area: str | None = None


@dataclass(frozen=True)
class AvailabilityWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class SourceReference:
    import_run_id: str
    table: str
    source_ref: str
    row_number: int | None = None


@dataclass(frozen=True)
class ImportAuditMetadata:
    import_run_id: str
    imported_at: datetime
    source_system: str
    source_refs: tuple[str, ...] = ()
    row_counts: Mapping[str, int] = field(default_factory=dict)
    validation_warnings: tuple[str, ...] = ()
    generated_state_snapshot_id: str | None = None


@dataclass(frozen=True)
class Worker:
    id: str
    name: str
    home_location: Location
    travel_modes: frozenset[TravelMode]
    gender: str | None = None
    has_car: bool = False
    skills: frozenset[str] = frozenset()
    languages: frozenset[str] = frozenset()
    willing_areas: frozenset[str] = frozenset()
    availability: tuple[AvailabilityWindow, ...] = ()
    unavailability: tuple[AvailabilityWindow, ...] = ()
    current_allocations: tuple[str, ...] = ()
    current_hours: float = 0.0
    max_hours: float | None = None
    preference_tags: frozenset[str] = frozenset()
    reliability_score: float | None = None
    source_reference: SourceReference | None = None


@dataclass(frozen=True)
class ServiceUser:
    id: str
    name: str
    home_location: Location
    support_level: str
    gender: str | None = None
    required_worker_count: int = 1
    requires_driver: bool = False
    required_skills: frozenset[str] = frozenset()
    communication_needs: frozenset[str] = frozenset()
    interests: frozenset[str] = frozenset()
    risk_tags: frozenset[str] = frozenset()
    preferred_worker_ids: frozenset[str] = frozenset()
    avoided_worker_ids: frozenset[str] = frozenset()
    profile_is_stale: bool = False
    source_reference: SourceReference | None = None


@dataclass(frozen=True)
class Allocation:
    worker_id: str
    role: str


@dataclass(frozen=True)
class Session:
    id: str
    service_user_id: str
    start: datetime
    end: datetime
    location: Location
    required_worker_count: int
    assigned: tuple[Allocation, ...] = ()
    requires_driver: bool = False
    priority: float = 1.0
    status: str = "unallocated"
    travel_buffer_before_minutes: int | None = None
    travel_buffer_after_minutes: int | None = None
    source_reference: SourceReference | None = None


@dataclass(frozen=True)
class Compatibility:
    worker_id: str
    service_user_id: str
    status: str
    reason: str | None = None
    source_reference: SourceReference | None = None


@dataclass(frozen=True)
class PairHistory:
    worker_id: str
    service_user_id: str
    completed_sessions: int = 0
    positive_feedback_count: int = 0
    incident_count: int = 0
    cancellation_count: int = 0
    last_session_at: datetime | None = None
    source_reference: SourceReference | None = None


@dataclass(frozen=True)
class FamilyRequest:
    id: str
    service_user_id: str
    requested_worker_ids: frozenset[str] = frozenset()
    avoided_worker_ids: frozenset[str] = frozenset()
    preferred_days: frozenset[int] = frozenset()
    preferred_time_ranges: tuple[AvailabilityWindow, ...] = ()
    unavailable_windows: tuple[AvailabilityWindow, ...] = ()
    notes: str = ""
    source_ref: str | None = None
    source_reference: SourceReference | None = None


@dataclass(frozen=True, init=False)
class TravelEstimate:
    origin: Location | None
    destination: Location | None
    mode: TravelMode
    departure_time: datetime | None
    duration_minutes: float
    distance_meters: float | None
    provider: str
    confidence: str
    warnings: tuple[str, ...]
    cached: bool
    cost: float | None

    def __init__(
        self,
        *,
        mode: TravelMode,
        origin: Location | None = None,
        destination: Location | None = None,
        departure_time: datetime | None = None,
        duration_minutes: float | None = None,
        minutes: float | None = None,
        distance_meters: float | None = None,
        distance_km: float | None = None,
        provider: str = "unknown",
        confidence: str | float = "low",
        warnings: tuple[str, ...] = (),
        cached: bool = False,
        cost: float | None = None,
    ) -> None:
        resolved_minutes = duration_minutes if duration_minutes is not None else minutes
        if resolved_minutes is None:
            raise TypeError("TravelEstimate requires duration_minutes or minutes")
        resolved_distance = distance_meters
        if resolved_distance is None and distance_km is not None:
            resolved_distance = distance_km * 1000.0

        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "departure_time", departure_time)
        object.__setattr__(self, "duration_minutes", float(resolved_minutes))
        object.__setattr__(self, "distance_meters", resolved_distance)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "confidence", _travel_confidence_label(confidence))
        object.__setattr__(self, "warnings", tuple(warnings))
        object.__setattr__(self, "cached", bool(cached))
        object.__setattr__(self, "cost", cost)

    @property
    def minutes(self) -> float:
        return self.duration_minutes

    @property
    def distance_km(self) -> float | None:
        if self.distance_meters is None:
            return None
        return self.distance_meters / 1000.0


def _travel_confidence_label(value: str | float) -> str:
    if isinstance(value, (int, float)):
        if value >= 0.75:
            return "high"
        if value >= 0.45:
            return "medium"
        return "low"
    normalized = value.strip().lower()
    if normalized in {"high", "medium", "low"}:
        return normalized
    return "low"


@dataclass(frozen=True)
class AssignAction:
    worker_id: str
    session_id: str
    role: str


@dataclass(frozen=True)
class Recommendation:
    action: AssignAction
    score: float
    hard_constraint_failures: tuple[str, ...]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    components: Mapping[str, float]
    travel_estimate: TravelEstimate | None = None


@dataclass(frozen=True)
class PlanningState:
    tenant_rules: TenantRules
    decision_time: datetime
    workers: Mapping[str, Worker]
    service_users: Mapping[str, ServiceUser]
    sessions: Mapping[str, Session]
    compatibility: Mapping[tuple[str, str], Compatibility] = field(default_factory=dict)
    pair_history: Mapping[tuple[str, str], PairHistory] = field(default_factory=dict)
    family_requests: Sequence[FamilyRequest] = ()
    data_quality_warnings: tuple[str, ...] = ()
    import_audit: ImportAuditMetadata | None = None
