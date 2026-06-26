"""Booking recommender domain and scoring core."""

from .models import (
    Allocation,
    AssignAction,
    AvailabilityWindow,
    Compatibility,
    FamilyRequest,
    ImportAuditMetadata,
    Location,
    PairHistory,
    PlanningState,
    Recommendation,
    ServiceUser,
    Session,
    SourceReference,
    TenantRules,
    TravelEstimate,
    TravelMode,
    Worker,
)
from .events import (
    ExogenousEvent,
    FamilyRequestArrivedEvent,
    ServiceUserProfileStaleEvent,
    WorkerUnavailableEvent,
)
from .planner import rank_session_candidates
from .batch import GreedyPlan, PlannedAssignment, greedy_allocate_open_sessions
from .allocation_report import (
    AllocationReport,
    FallbackTravelEstimator,
    allocation_report_to_dict,
    build_allocation_report,
    render_allocation_report,
)
from .scoring import TravelEstimator, score_assign_action
from .transitions import apply_assign_action, apply_exogenous_event
from .importer import (
    ImportIssue,
    ImportReport,
    StandardizedImportError,
    StandardizedImportResult,
    load_standardized_csv,
)

__all__ = [
    "Allocation",
    "AllocationReport",
    "AssignAction",
    "AvailabilityWindow",
    "Compatibility",
    "ExogenousEvent",
    "FamilyRequest",
    "FamilyRequestArrivedEvent",
    "FallbackTravelEstimator",
    "GreedyPlan",
    "ImportAuditMetadata",
    "ImportIssue",
    "ImportReport",
    "Location",
    "PairHistory",
    "PlannedAssignment",
    "PlanningState",
    "Recommendation",
    "ServiceUser",
    "ServiceUserProfileStaleEvent",
    "Session",
    "SourceReference",
    "StandardizedImportError",
    "StandardizedImportResult",
    "TenantRules",
    "TravelEstimate",
    "TravelEstimator",
    "TravelMode",
    "Worker",
    "WorkerUnavailableEvent",
    "allocation_report_to_dict",
    "apply_assign_action",
    "apply_exogenous_event",
    "build_allocation_report",
    "greedy_allocate_open_sessions",
    "load_standardized_csv",
    "rank_session_candidates",
    "render_allocation_report",
    "score_assign_action",
]
