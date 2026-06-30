"""Explainable scoring for assignment actions."""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from .models import (
    AssignAction,
    AvailabilityWindow,
    Location,
    PlanningState,
    Recommendation,
    ServiceUser,
    Session,
    TravelEstimate,
    TravelMode,
    Worker,
)


class TravelEstimator(Protocol):
    def estimate(
        self,
        *,
        worker: Worker,
        origin: Location,
        destination: Location,
        departure_time,
    ) -> TravelEstimate:
        ...


def score_assign_action(
    state: PlanningState,
    action: AssignAction,
    travel_estimator: TravelEstimator,
) -> Recommendation:
    guardrail_failures = _action_guardrail_failures(state, action)
    if guardrail_failures:
        return Recommendation(
            action=action,
            score=0.0,
            hard_constraint_failures=guardrail_failures,
            reasons=(),
            warnings=(),
            components={},
            travel_estimate=None,
        )

    worker = state.workers[action.worker_id]
    session = state.sessions[action.session_id]
    service_user = state.service_users[session.service_user_id]
    weights = state.tenant_rules.scoring_weights

    failures = list(_hard_constraint_failures(state, worker, service_user, session))
    reasons: list[str] = []
    warnings: list[str] = []
    components: dict[str, float] = {}

    travel_origin, departure_time, origin_warning = _travel_origin(state, worker, session)
    if origin_warning:
        warnings.append(origin_warning)

    travel_estimate = travel_estimator.estimate(
        worker=worker,
        origin=travel_origin,
        destination=session.location,
        departure_time=departure_time,
    )
    warnings.extend(travel_estimate.warnings)
    next_session = _next_allocated_session_after(state, worker, session)
    failures.extend(
        _travel_failures(
            state,
            worker,
            service_user,
            session,
            travel_estimate,
            departure_time,
            travel_estimator,
            next_session,
        )
    )

    if failures:
        return Recommendation(
            action=action,
            score=0.0,
            hard_constraint_failures=tuple(failures),
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            components=components,
            travel_estimate=travel_estimate,
        )

    compatibility = state.compatibility.get((worker.id, service_user.id))
    if compatibility and compatibility.status == "appropriate":
        components["explicit_compatible"] = weights["explicit_compatible"]
        reasons.append("explicit worker/service-user compatibility")

    if worker.id in service_user.preferred_worker_ids:
        components["family_requested"] = components.get("family_requested", 0.0) + weights["family_requested"]
        reasons.append("service-user record prefers this worker")

    for request in state.family_requests:
        if request.service_user_id != service_user.id:
            continue
        if any(_overlaps(window, session.start, session.end) for window in request.unavailable_windows):
            failures.append("family/date request says service user is unavailable")
        if worker.id in request.requested_worker_ids:
            components["family_requested"] = components.get("family_requested", 0.0) + weights["family_requested"]
            reasons.append("family/date request asks for this worker")
        if worker.id in request.avoided_worker_ids:
            failures.append("family/date request says to avoid this worker")

    if failures:
        return Recommendation(
            action=action,
            score=0.0,
            hard_constraint_failures=tuple(failures),
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            components=components,
            travel_estimate=travel_estimate,
        )

    for request in state.family_requests:
        if request.service_user_id != service_user.id:
            continue
        if any(_contains(window, session.start, session.end) for window in request.preferred_time_ranges):
            components["date_request_match"] = max(
                components.get("date_request_match", 0.0),
                weights["date_request_match"],
            )
            reasons.append("session matches family/date requested window")
            break

    history = state.pair_history.get((worker.id, service_user.id))
    if history and history.completed_sessions:
        continuity = min(1.0, history.completed_sessions / 8.0)
        feedback_bonus = min(0.5, history.positive_feedback_count / 10.0)
        incident_penalty = min(0.75, history.incident_count / 4.0)
        value = weights["continuity"] * max(0.0, continuity + feedback_bonus - incident_penalty)
        components["continuity"] = value
        reasons.append("previous worker/service-user continuity")

    travel_value = _travel_fit_value(state, travel_estimate)
    components["travel_fit"] = weights["travel_fit"] * travel_value
    reasons.append(f"travel estimate {travel_estimate.minutes:.0f} minutes by {travel_estimate.mode.value}")

    if worker.home_location.area and worker.home_location.area == service_user.home_location.area:
        components["postcode_area_match"] = weights["postcode_area_match"]
        reasons.append(f"same postcode area {worker.home_location.area}")

    missing_skills = service_user.required_skills.difference(worker.skills)
    if not missing_skills and service_user.required_skills:
        components["skill_risk_fit"] = weights["skill_risk_fit"]
        reasons.append("worker has required risk/support skills")

    if service_user.home_location.area in worker.willing_areas:
        components["worker_preference"] = weights["worker_preference"]
        reasons.append("worker willing area matches service-user area")

    components["load_balance"] = weights["load_balance"] * _load_balance_value(worker)

    if worker.reliability_score is not None:
        components["reliability"] = weights["reliability"] * max(0.0, min(1.0, worker.reliability_score))
        reasons.append("reliability score present")

    if service_user.profile_is_stale:
        components["uncertainty_penalty"] = weights["uncertainty_penalty"]
        warnings.append("service-user profile is stale")

    score = sum(components.values()) * session.priority

    return Recommendation(
        action=action,
        score=round(score, 3),
        hard_constraint_failures=tuple(failures),
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        components=components,
        travel_estimate=travel_estimate,
    )


def _hard_constraint_failures(
    state: PlanningState,
    worker: Worker,
    service_user: ServiceUser,
    session: Session,
) -> tuple[str, ...]:
    failures: list[str] = []

    if len(session.assigned) >= session.required_worker_count:
        failures.append("session is already fully allocated")

    if any(allocation.worker_id == worker.id for allocation in session.assigned):
        failures.append("worker is already assigned to this session")

    if _has_schedule_conflict(state, worker, session):
        failures.append("worker already has an overlapping or too-close allocated session")

    if worker.id in service_user.avoided_worker_ids:
        failures.append("service-user record says to avoid this worker")

    compatibility = state.compatibility.get((worker.id, service_user.id))
    if compatibility and compatibility.status == "inappropriate":
        failures.append("explicitly marked inappropriate for this service user")

    if not worker.availability and not state.tenant_rules.allow_missing_worker_availability:
        failures.append("missing_worker_availability")
    elif not _is_available(worker, session):
        failures.append("worker is not available for the full session")

    if service_user.required_skills.difference(worker.skills):
        failures.append("worker is missing required risk/support skills")

    if worker.max_hours is not None:
        session_hours = (session.end - session.start).total_seconds() / 3600
        if worker.current_hours + session_hours > worker.max_hours:
            failures.append("worker would exceed configured max hours")

    return tuple(failures)


def _travel_failures(
    state: PlanningState,
    worker: Worker,
    service_user: ServiceUser,
    session: Session,
    estimate: TravelEstimate,
    departure_time,
    travel_estimator: TravelEstimator,
    next_session: Session | None,
) -> tuple[str, ...]:
    failures: list[str] = []

    if estimate.mode not in worker.travel_modes:
        failures.append(f"travel estimate mode {estimate.mode.value} is not allowed for worker")

    requires_driver = service_user.requires_driver or session.requires_driver
    if requires_driver:
        if not worker.has_car or TravelMode.DRIVE not in worker.travel_modes:
            failures.append("session requires a driver/car")
        if estimate.mode != TravelMode.DRIVE:
            failures.append("session requires a drive-capable route")

    buffer_minutes = session.travel_buffer_before_minutes
    if buffer_minutes is None:
        buffer_minutes = state.tenant_rules.min_travel_buffer_minutes

    latest_departure = session.start - timedelta(minutes=estimate.minutes + buffer_minutes)
    if latest_departure < departure_time:
        failures.append("not enough time to travel before session")

    if estimate.minutes > state.tenant_rules.max_planned_travel_minutes:
        failures.append("estimated travel exceeds tenant max planned travel")

    if next_session is not None:
        after_buffer_minutes = session.travel_buffer_after_minutes
        if after_buffer_minutes is None:
            after_buffer_minutes = state.tenant_rules.min_travel_buffer_minutes
        next_estimate = travel_estimator.estimate(
            worker=worker,
            origin=session.location,
            destination=next_session.location,
            departure_time=session.end,
        )
        if next_estimate.mode not in worker.travel_modes:
            failures.append(f"next travel estimate mode {next_estimate.mode.value} is not allowed for worker")
        earliest_arrival = session.end + timedelta(minutes=next_estimate.minutes + after_buffer_minutes)
        if earliest_arrival > next_session.start:
            failures.append("not enough time to travel to next allocated session")

    return tuple(failures)


def _action_guardrail_failures(state: PlanningState, action: AssignAction) -> tuple[str, ...]:
    failures: list[str] = []
    if action.worker_id not in state.workers:
        failures.append("unknown worker id")
    session = state.sessions.get(action.session_id)
    if session is None:
        failures.append("unknown session id")
    elif session.service_user_id not in state.service_users:
        failures.append("missing service user for session")
    return tuple(failures)


def _is_available(worker: Worker, session: Session) -> bool:
    if worker.availability:
        if not any(_contains(window, session.start, session.end) for window in worker.availability):
            return False

    if any(_overlaps(window, session.start, session.end) for window in worker.unavailability):
        return False

    return True


def _has_schedule_conflict(state: PlanningState, worker: Worker, candidate_session: Session) -> bool:
    for session_id in worker.current_allocations:
        existing = state.sessions.get(session_id)
        if existing and existing.id != candidate_session.id and _sessions_overlap(existing, candidate_session):
            return True
    for existing in state.sessions.values():
        if existing.id == candidate_session.id:
            continue
        if not any(allocation.worker_id == worker.id for allocation in existing.assigned):
            continue
        if _sessions_overlap(existing, candidate_session):
            return True
    return False


def _travel_origin(state: PlanningState, worker: Worker, candidate_session: Session):
    previous = _previous_allocated_session_before(state, worker, candidate_session)
    if previous is None or previous.end < state.decision_time:
        return worker.home_location, state.decision_time, ""
    return (
        previous.location,
        previous.end,
        f"travel origin is previous session ending {previous.end.strftime('%d/%m/%Y %H:%M')}",
    )


def _previous_allocated_session_before(
    state: PlanningState,
    worker: Worker,
    candidate_session: Session,
) -> Session | None:
    previous_sessions = [
        session
        for session in _allocated_sessions_for_worker(state, worker)
        if session.id != candidate_session.id and session.end <= candidate_session.start
    ]
    if not previous_sessions:
        return None
    return max(previous_sessions, key=lambda item: item.end)


def _next_allocated_session_after(
    state: PlanningState,
    worker: Worker,
    candidate_session: Session,
) -> Session | None:
    next_sessions = [
        session
        for session in _allocated_sessions_for_worker(state, worker)
        if session.id != candidate_session.id and session.start >= candidate_session.end
    ]
    if not next_sessions:
        return None
    return min(next_sessions, key=lambda item: item.start)


def _allocated_sessions_for_worker(state: PlanningState, worker: Worker) -> tuple[Session, ...]:
    sessions = []
    seen = set()
    for session_id in worker.current_allocations:
        session = state.sessions.get(session_id)
        if session and session.id not in seen:
            sessions.append(session)
            seen.add(session.id)
    for session in state.sessions.values():
        if session.id in seen:
            continue
        if any(allocation.worker_id == worker.id for allocation in session.assigned):
            sessions.append(session)
            seen.add(session.id)
    return tuple(sessions)


def _sessions_overlap(left: Session, right: Session) -> bool:
    return left.start < right.end and right.start < left.end


def _contains(window: AvailabilityWindow, start, end) -> bool:
    return window.start <= start and end <= window.end


def _overlaps(window: AvailabilityWindow, start, end) -> bool:
    return start < window.end and window.start < end


def _travel_fit_value(state: PlanningState, estimate: TravelEstimate) -> float:
    max_minutes = state.tenant_rules.max_planned_travel_minutes
    if max_minutes <= 0:
        return 0.0
    time_score = max(0.0, 1.0 - (estimate.minutes / max_minutes))
    confidence = max(0.0, min(1.0, estimate.confidence))
    return time_score * (0.5 + confidence / 2.0)


def _load_balance_value(worker: Worker) -> float:
    if worker.max_hours is None or worker.max_hours <= 0:
        return 0.5
    return max(0.0, 1.0 - (worker.current_hours / worker.max_hours))
