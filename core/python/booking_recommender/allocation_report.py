"""End-to-end allocation reporting helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .batch import GreedyPlan, greedy_allocate_open_sessions
from .importer import ImportIssue, ImportReport
from .models import (
    Allocation,
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
    TravelEstimate,
    TravelMode,
    Worker,
)
from .planner import rank_session_candidates
from .scoring import TravelEstimator


@dataclass(frozen=True)
class AllocationReport:
    source_folder: str
    generated_at: datetime
    import_report: ImportReport
    initial_state: PlanningState
    plan: GreedyPlan
    single_session_rankings: Mapping[str, tuple[Recommendation, ...]]


class FallbackTravelEstimator:
    """Small deterministic estimator for local planning before map providers exist."""

    def estimate(self, *, worker: Worker, origin: Location, destination: Location, departure_time) -> TravelEstimate:
        mode = _preferred_mode(worker)
        minutes = 20.0
        warnings: list[str] = []

        if origin.area and destination.area:
            minutes = 12.0 if origin.area == destination.area else 35.0
        elif origin.latitude is not None and destination.latitude is not None:
            minutes = 25.0
        else:
            minutes = 30.0
            warnings.append("fallback travel estimate used because location data is incomplete")

        if mode == TravelMode.WALK:
            minutes *= 1.5
        elif mode in {TravelMode.BUS, TravelMode.PUBLIC_TRANSPORT, TravelMode.TRAM, TravelMode.TRAIN}:
            minutes *= 1.25

        return TravelEstimate(
            mode=mode,
            minutes=round(minutes, 1),
            confidence=0.35,
            provider="fallback",
            warnings=tuple(warnings),
        )


def build_allocation_report(
    *,
    source_folder: str | Path,
    state: PlanningState,
    import_report: ImportReport,
    travel_estimator: TravelEstimator | None = None,
    top_n: int = 5,
    include_infeasible: bool = False,
    compare_single_session: bool = False,
) -> AllocationReport:
    estimator = travel_estimator or FallbackTravelEstimator()
    plan = greedy_allocate_open_sessions(
        state,
        estimator,
        top_n=top_n,
        include_infeasible=include_infeasible,
    )
    rankings: dict[str, tuple[Recommendation, ...]] = {}
    if compare_single_session:
        for session in sorted(state.sessions.values(), key=lambda item: (item.start, item.id)):
            if not _is_open_session(session):
                continue
            role = _next_role(session)
            rankings[session.id] = tuple(
                rank_session_candidates(
                    state,
                    session.id,
                    role,
                    estimator,
                    top_n=top_n,
                    include_infeasible=include_infeasible,
                )
            )

    return AllocationReport(
        source_folder=str(Path(source_folder)),
        generated_at=datetime.now(timezone.utc),
        import_report=import_report,
        initial_state=state,
        plan=plan,
        single_session_rankings=rankings,
    )


def allocation_report_to_dict(report: AllocationReport) -> dict:
    final_state = report.plan.final_state
    accepted = tuple(item for item in report.plan.assignments if item.accepted)
    unfilled_sessions = tuple(
        session
        for session in sorted(final_state.sessions.values(), key=lambda item: (item.start, item.id))
        if _is_open_session(session) and len(session.assigned) < session.required_worker_count
    )
    blocked_assignments = tuple(item for item in report.plan.assignments if not item.accepted)

    return {
        "run": {
            "source_folder": report.source_folder,
            "generated_at": report.generated_at.isoformat(),
            "import_run_id": _import_run_id(final_state),
        },
        "summary": {
            "sessions": len(final_state.sessions),
            "accepted_assignments": len(accepted),
            "unfilled_sessions": len(unfilled_sessions),
            "blocked_decisions": len(blocked_assignments),
            "import_errors": len(report.import_report.errors),
            "import_warnings": len(report.import_report.warnings),
        },
        "import_report": import_report_to_dict(report.import_report),
        "planning_snapshot": planning_state_to_dict(final_state),
        "proposed_plan": {
            "allocated_sessions": [
                session_plan_to_dict(final_state, session)
                for session in sorted(final_state.sessions.values(), key=lambda item: (item.start, item.id))
                if session.assigned and len(session.assigned) >= session.required_worker_count
            ],
            "unfilled_sessions": [session_plan_to_dict(final_state, session) for session in unfilled_sessions],
            "blocked_decisions": [planned_assignment_to_dict(item) for item in blocked_assignments],
        },
        "recommendation_details": [planned_assignment_to_dict(item) for item in report.plan.assignments],
        "single_session_rankings": {
            session_id: [recommendation_to_dict(item) for item in recommendations]
            for session_id, recommendations in report.single_session_rankings.items()
        },
    }


def render_allocation_report(report: AllocationReport) -> str:
    data = allocation_report_to_dict(report)
    lines = [
        "Allocation plan report",
        f"import_run_id: {data['run']['import_run_id'] or 'unknown'}",
        f"source_folder: {data['run']['source_folder']}",
        (
            "summary: "
            f"{data['summary']['accepted_assignments']} proposed assignments, "
            f"{data['summary']['unfilled_sessions']} unfilled sessions, "
            f"{data['summary']['blocked_decisions']} blocked decisions, "
            f"{data['summary']['import_warnings']} import warnings"
        ),
        "",
        "Allocated sessions",
    ]

    allocated = data["proposed_plan"]["allocated_sessions"]
    if not allocated:
        lines.append("- none")
    for session in allocated:
        workers = ", ".join(
            f"{item['worker_name']} ({item['role']})" for item in session["assigned_workers"]
        )
        lines.append(f"- {session['session_id']} {session['start']} -> {session['end']}: {workers}")
        for detail in _accepted_details_for_session(data["recommendation_details"], session["session_id"]):
            lines.append(
                f"  score {detail['recommendation']['score']}: "
                + _join_or_default(detail["recommendation"]["reasons"], "no positive reasons recorded")
            )
            warnings = detail["recommendation"]["warnings"]
            if warnings:
                lines.append(f"  warnings: {_join_or_default(warnings, 'none')}")

    lines.append("")
    lines.append("Unfilled sessions")
    unfilled = data["proposed_plan"]["unfilled_sessions"]
    if not unfilled:
        lines.append("- none")
    for session in unfilled:
        assigned = ""
        if session["assigned_workers"]:
            assigned = "; assigned: " + ", ".join(
                f"{item['worker_name']} ({item['role']})" for item in session["assigned_workers"]
            )
        lines.append(
            f"- {session['session_id']} needs "
            f"{session['remaining_worker_count']} more worker(s){assigned}"
        )

    lines.append("")
    lines.append("Blocked candidates")
    blocked_candidates = _blocked_candidates(data["recommendation_details"])
    if not blocked_candidates:
        lines.append("- none")
    for item in blocked_candidates:
        recommendation = item["recommendation"]
        lines.append(
            f"- {recommendation['session_id']} / {recommendation['worker_id'] or 'no worker'}: "
            + _join_or_default(recommendation["hard_constraint_failures"], "no feasible candidates")
        )

    if data["single_session_rankings"]:
        lines.append("")
        lines.append("One-session ranking comparison")
        for session_id, recommendations in data["single_session_rankings"].items():
            if not recommendations:
                lines.append(f"- {session_id}: no candidates")
                continue
            top = recommendations[0]
            lines.append(
                f"- {session_id}: top one-session candidate {top['worker_id']} "
                f"score {top['score']}"
            )

    if report.import_report.warnings:
        lines.append("")
        lines.append("Import warnings")
        for issue in report.import_report.warnings:
            lines.append(f"- {issue.code}: {issue.message}")

    return "\n".join(lines) + "\n"


def import_report_to_dict(report: ImportReport) -> dict:
    return {
        "row_counts": dict(sorted(report.row_counts.items())),
        "issues": [import_issue_to_dict(issue) for issue in report.issues],
    }


def import_issue_to_dict(issue: ImportIssue) -> dict:
    return {
        "severity": issue.severity,
        "code": issue.code,
        "message": issue.message,
        "table": issue.table,
        "row_number": issue.row_number,
        "field": issue.field,
        "source_ref": issue.source_ref,
    }


def planning_state_to_dict(state: PlanningState) -> dict:
    return {
        "tenant_rules": {
            "tenant_id": state.tenant_rules.tenant_id,
            "min_travel_buffer_minutes": state.tenant_rules.min_travel_buffer_minutes,
            "max_planned_travel_minutes": state.tenant_rules.max_planned_travel_minutes,
            "require_human_approval": state.tenant_rules.require_human_approval,
            "scoring_weights": dict(state.tenant_rules.scoring_weights),
        },
        "decision_time": state.decision_time.isoformat(),
        "workers": {worker_id: worker_to_dict(worker) for worker_id, worker in sorted(state.workers.items())},
        "service_users": {
            service_user_id: service_user_to_dict(service_user)
            for service_user_id, service_user in sorted(state.service_users.items())
        },
        "sessions": {
            session_id: session_to_dict(session)
            for session_id, session in sorted(state.sessions.items())
        },
        "compatibility": [
            compatibility_to_dict(item)
            for _, item in sorted(state.compatibility.items())
        ],
        "pair_history": [
            pair_history_to_dict(item)
            for _, item in sorted(state.pair_history.items())
        ],
        "family_requests": [family_request_to_dict(item) for item in state.family_requests],
        "data_quality_warnings": list(state.data_quality_warnings),
        "import_audit": import_audit_to_dict(state.import_audit) if state.import_audit else None,
    }


def session_plan_to_dict(state: PlanningState, session: Session) -> dict:
    return {
        **session_to_dict(session),
        "assigned_workers": [
            {
                "worker_id": allocation.worker_id,
                "worker_name": state.workers.get(allocation.worker_id).name
                if allocation.worker_id in state.workers
                else allocation.worker_id,
                "role": allocation.role,
            }
            for allocation in session.assigned
        ],
        "remaining_worker_count": max(0, session.required_worker_count - len(session.assigned)),
    }


def planned_assignment_to_dict(item) -> dict:
    return {
        "accepted": item.accepted,
        "reason": item.reason,
        "recommendation": recommendation_to_dict(item.recommendation),
        "candidates": [recommendation_to_dict(candidate) for candidate in item.candidates],
    }


def recommendation_to_dict(recommendation: Recommendation) -> dict:
    return {
        "worker_id": recommendation.action.worker_id,
        "session_id": recommendation.action.session_id,
        "role": recommendation.action.role,
        "score": recommendation.score,
        "hard_constraint_failures": list(recommendation.hard_constraint_failures),
        "reasons": list(recommendation.reasons),
        "warnings": list(recommendation.warnings),
        "components": dict(recommendation.components),
        "travel_estimate": travel_estimate_to_dict(recommendation.travel_estimate)
        if recommendation.travel_estimate
        else None,
    }


def worker_to_dict(worker: Worker) -> dict:
    return {
        "id": worker.id,
        "name": worker.name,
        "gender": worker.gender,
        "home_location": location_to_dict(worker.home_location),
        "travel_modes": sorted(mode.value for mode in worker.travel_modes),
        "has_car": worker.has_car,
        "skills": sorted(worker.skills),
        "languages": sorted(worker.languages),
        "willing_areas": sorted(worker.willing_areas),
        "availability": [window_to_dict(window) for window in worker.availability],
        "unavailability": [window_to_dict(window) for window in worker.unavailability],
        "current_allocations": list(worker.current_allocations),
        "current_hours": worker.current_hours,
        "max_hours": worker.max_hours,
        "preference_tags": sorted(worker.preference_tags),
        "reliability_score": worker.reliability_score,
        "source_reference": source_reference_to_dict(worker.source_reference),
    }


def service_user_to_dict(service_user: ServiceUser) -> dict:
    return {
        "id": service_user.id,
        "name": service_user.name,
        "gender": service_user.gender,
        "home_location": location_to_dict(service_user.home_location),
        "support_level": service_user.support_level,
        "required_worker_count": service_user.required_worker_count,
        "requires_driver": service_user.requires_driver,
        "required_skills": sorted(service_user.required_skills),
        "communication_needs": sorted(service_user.communication_needs),
        "interests": sorted(service_user.interests),
        "risk_tags": sorted(service_user.risk_tags),
        "preferred_worker_ids": sorted(service_user.preferred_worker_ids),
        "avoided_worker_ids": sorted(service_user.avoided_worker_ids),
        "profile_is_stale": service_user.profile_is_stale,
        "source_reference": source_reference_to_dict(service_user.source_reference),
    }


def session_to_dict(session: Session) -> dict:
    return {
        "id": session.id,
        "session_id": session.id,
        "service_user_id": session.service_user_id,
        "start": session.start.isoformat(),
        "end": session.end.isoformat(),
        "location": location_to_dict(session.location),
        "required_worker_count": session.required_worker_count,
        "assigned": [allocation_to_dict(item) for item in session.assigned],
        "requires_driver": session.requires_driver,
        "priority": session.priority,
        "status": session.status,
        "travel_buffer_before_minutes": session.travel_buffer_before_minutes,
        "travel_buffer_after_minutes": session.travel_buffer_after_minutes,
        "source_reference": source_reference_to_dict(session.source_reference),
    }


def compatibility_to_dict(compatibility: Compatibility) -> dict:
    return {
        "worker_id": compatibility.worker_id,
        "service_user_id": compatibility.service_user_id,
        "status": compatibility.status,
        "reason": compatibility.reason,
        "source_reference": source_reference_to_dict(compatibility.source_reference),
    }


def pair_history_to_dict(pair_history: PairHistory) -> dict:
    return {
        "worker_id": pair_history.worker_id,
        "service_user_id": pair_history.service_user_id,
        "completed_sessions": pair_history.completed_sessions,
        "positive_feedback_count": pair_history.positive_feedback_count,
        "incident_count": pair_history.incident_count,
        "cancellation_count": pair_history.cancellation_count,
        "last_session_at": pair_history.last_session_at.isoformat()
        if pair_history.last_session_at
        else None,
        "source_reference": source_reference_to_dict(pair_history.source_reference),
    }


def family_request_to_dict(request: FamilyRequest) -> dict:
    return {
        "id": request.id,
        "service_user_id": request.service_user_id,
        "requested_worker_ids": sorted(request.requested_worker_ids),
        "avoided_worker_ids": sorted(request.avoided_worker_ids),
        "preferred_days": sorted(request.preferred_days),
        "preferred_time_ranges": [window_to_dict(window) for window in request.preferred_time_ranges],
        "unavailable_windows": [window_to_dict(window) for window in request.unavailable_windows],
        "notes": request.notes,
        "source_ref": request.source_ref,
        "source_reference": source_reference_to_dict(request.source_reference),
    }


def import_audit_to_dict(audit: ImportAuditMetadata) -> dict:
    return {
        "import_run_id": audit.import_run_id,
        "imported_at": audit.imported_at.isoformat(),
        "source_system": audit.source_system,
        "source_refs": list(audit.source_refs),
        "row_counts": dict(audit.row_counts),
        "validation_warnings": list(audit.validation_warnings),
        "generated_state_snapshot_id": audit.generated_state_snapshot_id,
    }


def allocation_to_dict(allocation: Allocation) -> dict:
    return {"worker_id": allocation.worker_id, "role": allocation.role}


def window_to_dict(window: AvailabilityWindow) -> dict:
    return {"start": window.start.isoformat(), "end": window.end.isoformat()}


def source_reference_to_dict(source_reference: SourceReference | None) -> dict | None:
    if source_reference is None:
        return None
    return {
        "import_run_id": source_reference.import_run_id,
        "table": source_reference.table,
        "source_ref": source_reference.source_ref,
        "row_number": source_reference.row_number,
    }


def location_to_dict(location: Location) -> dict:
    return {
        "label": location.label,
        "postcode": location.postcode,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "area": location.area,
    }


def travel_estimate_to_dict(estimate: TravelEstimate) -> dict:
    return {
        "mode": estimate.mode.value,
        "minutes": estimate.minutes,
        "distance_km": estimate.distance_km,
        "cost": estimate.cost,
        "confidence": estimate.confidence,
        "provider": estimate.provider,
        "warnings": list(estimate.warnings),
    }


def _accepted_details_for_session(recommendation_details, session_id: str):
    return [
        item
        for item in recommendation_details
        if item["accepted"] and item["recommendation"]["session_id"] == session_id
    ]


def _blocked_candidates(recommendation_details):
    blocked = []
    for detail in recommendation_details:
        if not detail["accepted"]:
            blocked.append(detail)
        for candidate in detail["candidates"]:
            if candidate["hard_constraint_failures"]:
                blocked.append(
                    {
                        "accepted": False,
                        "reason": "candidate failed hard constraints",
                        "recommendation": candidate,
                        "candidates": [],
                    }
                )
    return blocked


def _join_or_default(values, default: str) -> str:
    return "; ".join(values) if values else default


def _preferred_mode(worker: Worker) -> TravelMode:
    if worker.has_car and TravelMode.DRIVE in worker.travel_modes:
        return TravelMode.DRIVE
    for mode in (
        TravelMode.PUBLIC_TRANSPORT,
        TravelMode.BUS,
        TravelMode.TRAM,
        TravelMode.TRAIN,
        TravelMode.CYCLE,
        TravelMode.WALK,
        TravelMode.TAXI,
        TravelMode.DRIVE,
    ):
        if mode in worker.travel_modes:
            return mode
    return TravelMode.WALK


def _is_open_session(session: Session) -> bool:
    return session.status not in {"cancelled", "rejected", "confirmed"} and len(session.assigned) < session.required_worker_count


def _next_role(session: Session) -> str:
    roles = {allocation.role for allocation in session.assigned}
    if "lead" not in roles:
        return "lead"
    return "second"


def _import_run_id(state: PlanningState) -> str | None:
    if state.import_audit is None:
        return None
    return state.import_audit.import_run_id
