"""State transitions for accepted decisions and exogenous information."""

from __future__ import annotations

from dataclasses import replace

from .events import (
    ExogenousEvent,
    FamilyRequestArrivedEvent,
    ServiceUserProfileStaleEvent,
    WorkerUnavailableEvent,
)
from .models import Allocation, AssignAction, PlanningState, Session


def apply_assign_action(state: PlanningState, action: AssignAction) -> PlanningState:
    """Return a new state after a human-approved assignment decision."""

    session = state.sessions[action.session_id]
    updated_session = _assign_worker_to_session(session, action)
    updated_sessions = dict(state.sessions)
    updated_sessions[action.session_id] = updated_session

    worker = state.workers[action.worker_id]
    updated_worker = replace(
        worker,
        current_allocations=worker.current_allocations + (action.session_id,),
        current_hours=worker.current_hours
        + ((session.end - session.start).total_seconds() / 3600.0),
    )
    updated_workers = dict(state.workers)
    updated_workers[action.worker_id] = updated_worker

    return replace(state, workers=updated_workers, sessions=updated_sessions)


def apply_exogenous_event(state: PlanningState, event: ExogenousEvent) -> PlanningState:
    """Return a new state after information outside our control arrives."""

    if isinstance(event, WorkerUnavailableEvent):
        return _apply_worker_unavailable(state, event)
    if isinstance(event, FamilyRequestArrivedEvent):
        return _apply_family_request_arrived(state, event)
    if isinstance(event, ServiceUserProfileStaleEvent):
        return _apply_profile_stale(state, event)
    return state


def _assign_worker_to_session(session: Session, action: AssignAction) -> Session:
    assigned = session.assigned + (Allocation(worker_id=action.worker_id, role=action.role),)
    status = "allocated" if len(assigned) >= session.required_worker_count else "partially_allocated"
    return replace(session, assigned=assigned, status=status)


def _apply_worker_unavailable(state: PlanningState, event: WorkerUnavailableEvent) -> PlanningState:
    if not event.worker_id or event.worker_id not in state.workers or event.unavailable_window is None:
        return state

    worker = state.workers[event.worker_id]
    updated_worker = replace(worker, unavailability=worker.unavailability + (event.unavailable_window,))
    updated_workers = dict(state.workers)
    updated_workers[event.worker_id] = updated_worker

    updated_sessions = {}
    affected_session_ids = set()
    for session_id, session in state.sessions.items():
        if any(allocation.worker_id == event.worker_id for allocation in session.assigned):
            if _window_overlaps_session(event.unavailable_window, session):
                remaining = tuple(
                    allocation
                    for allocation in session.assigned
                    if allocation.worker_id != event.worker_id
                )
                affected_session_ids.add(session_id)
                status = "unallocated" if not remaining else "partially_allocated"
                updated_sessions[session_id] = replace(session, assigned=remaining, status=status)
                continue
        updated_sessions[session_id] = session

    warnings = state.data_quality_warnings
    if affected_session_ids:
        warnings = warnings + (
            f"Worker {event.worker_id} unavailable; sessions need repair: "
            + ", ".join(sorted(affected_session_ids)),
        )

    return replace(
        state,
        workers=updated_workers,
        sessions=updated_sessions,
        data_quality_warnings=warnings,
    )


def _apply_family_request_arrived(
    state: PlanningState,
    event: FamilyRequestArrivedEvent,
) -> PlanningState:
    if event.request is None:
        return state
    return replace(state, family_requests=tuple(state.family_requests) + (event.request,))


def _apply_profile_stale(
    state: PlanningState,
    event: ServiceUserProfileStaleEvent,
) -> PlanningState:
    if not event.service_user_id or event.service_user_id not in state.service_users:
        return state

    service_user = state.service_users[event.service_user_id]
    updated_service_users = dict(state.service_users)
    updated_service_users[event.service_user_id] = replace(service_user, profile_is_stale=True)
    warning = f"Service-user profile stale: {event.service_user_id}"
    if event.reason:
        warning = f"{warning} ({event.reason})"
    return replace(
        state,
        service_users=updated_service_users,
        data_quality_warnings=state.data_quality_warnings + (warning,),
    )


def _window_overlaps_session(window, session: Session) -> bool:
    return session.start < window.end and window.start < session.end

