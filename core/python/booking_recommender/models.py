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


@dataclass(frozen=True)
class TravelEstimate:
    mode: TravelMode
    minutes: float
    distance_km: float | None = None
    cost: float | None = None
    confidence: float = 0.5
    provider: str = "unknown"
    warnings: tuple[str, ...] = ()


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
