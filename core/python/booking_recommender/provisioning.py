"""Planner-driven session provisioning from family requests with no matching session.

Family/ad hoc requests describe a family's ongoing scheduling intent, but
nothing turns that intent into a bookable ``Session`` unless someone keys the
booking into the source system by hand. This module closes that gap for
requests with enough structure to act on: it tries candidate (day, start time)
slots against the existing hard-constraint/scoring machinery, and only
creates a session where at least one worker actually looks feasible. Worker
*selection* is left entirely to the normal planner (``batch.py``); this module
only decides whether and when a session should exist.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from .models import DayPattern, FamilyRequest, PlanningState, ServiceUser, Session
from .planner import rank_session_candidates
from .scoring import TravelEstimator

_DEFAULT_WINDOW_START = time(8, 0)
_DEFAULT_WINDOW_END = time(20, 0)


@dataclass(frozen=True)
class ProvisionedSession:
    session_id: str
    family_request_id: str
    service_user_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class UnscheduledRequest:
    family_request_id: str
    service_user_id: str
    reason: str


@dataclass(frozen=True)
class ProvisioningResult:
    final_state: PlanningState
    created: tuple[ProvisionedSession, ...]
    unscheduled: tuple[UnscheduledRequest, ...]


def provision_sessions_from_family_requests(
    state: PlanningState,
    travel_estimator: TravelEstimator,
) -> ProvisioningResult:
    """Create sessions for family requests with no matching session in the period.

    Only requests with enough structure to act on -- one or more day patterns
    (``FamilyRequest.day_patterns``, or the simpler ``preferred_days`` +
    ``target_sessions_per_week`` pair as a single implicit pattern) plus
    ``target_session_duration_minutes`` -- are considered, and only when the
    service user has no session at all yet in the inferred period. A request
    with more than one day pattern (e.g. "1 midweek + 1 weekend session/week")
    fills each pattern's quota from its own day pool independently, so one
    category can never silently absorb another's slots. Candidate slots are
    tested with the same ``rank_session_candidates`` hard-constraint/scoring
    path used everywhere else, so a created session is never a guess a worker
    couldn't actually fill. A pattern that can't be filled in a given week is
    recorded in ``unscheduled``, never silently dropped.
    """

    period_start, period_end = _infer_period(state)
    current_state = state
    created: list[ProvisionedSession] = []
    unscheduled: list[UnscheduledRequest] = []

    for request in state.family_requests:
        service_user = state.service_users.get(request.service_user_id)
        patterns = _effective_day_patterns(request)
        if service_user is None or not patterns or not request.target_session_duration_minutes:
            continue
        if _service_user_has_session(state, request.service_user_id):
            continue

        total_target = sum(pattern.sessions_per_week for pattern in patterns)
        for week_start in _weeks_in_period(period_start, period_end):
            placed_this_week = 0
            for pattern in patterns:
                placed_this_pattern = 0
                for day_offset in pattern.days:
                    if placed_this_pattern >= pattern.sessions_per_week:
                        break
                    candidate_date = week_start + timedelta(days=day_offset)
                    if candidate_date < period_start or candidate_date > period_end:
                        continue
                    session, current_state = _try_create_session(
                        current_state,
                        request=request,
                        service_user=service_user,
                        candidate_date=candidate_date,
                        travel_estimator=travel_estimator,
                    )
                    if session is not None:
                        created.append(
                            ProvisionedSession(
                                session_id=session.id,
                                family_request_id=request.id,
                                service_user_id=request.service_user_id,
                                start=session.start,
                                end=session.end,
                            )
                        )
                        placed_this_pattern += 1
                        placed_this_week += 1

            if placed_this_week < total_target:
                unscheduled.append(
                    UnscheduledRequest(
                        family_request_id=request.id,
                        service_user_id=request.service_user_id,
                        reason=(
                            f"no feasible worker found for week of {week_start.isoformat()} "
                            f"({placed_this_week}/{total_target} sessions placed)"
                        ),
                    )
                )

    return ProvisioningResult(final_state=current_state, created=tuple(created), unscheduled=tuple(unscheduled))


def _effective_day_patterns(request: FamilyRequest) -> tuple[DayPattern, ...]:
    if request.day_patterns:
        return request.day_patterns
    if request.preferred_days and request.target_sessions_per_week:
        return (DayPattern(days=request.preferred_days, sessions_per_week=request.target_sessions_per_week),)
    return ()


def _try_create_session(
    state: PlanningState,
    *,
    request: FamilyRequest,
    service_user: ServiceUser,
    candidate_date: date,
    travel_estimator: TravelEstimator,
) -> tuple[Session | None, PlanningState]:
    duration = timedelta(minutes=request.target_session_duration_minutes or 0)
    window_start = request.preferred_time_window_start or _DEFAULT_WINDOW_START
    window_end = request.preferred_time_window_end or _DEFAULT_WINDOW_END
    tzinfo = state.decision_time.tzinfo

    session_id = f"session-request-{request.id}-{candidate_date.isoformat()}"
    if session_id in state.sessions:
        return None, state

    for start_time in _candidate_start_times(window_start, window_end, duration):
        start = datetime.combine(candidate_date, start_time, tzinfo=tzinfo)
        end = start + duration
        candidate_session = Session(
            id=session_id,
            service_user_id=service_user.id,
            start=start,
            end=end,
            location=service_user.home_location,
            required_worker_count=service_user.required_worker_count,
            requires_driver=service_user.requires_driver,
            status="unallocated",
            source_reference=request.source_reference,
        )
        trial_sessions = dict(state.sessions)
        trial_sessions[session_id] = candidate_session
        trial_state = replace(state, sessions=trial_sessions)
        recommendations = rank_session_candidates(
            trial_state,
            session_id,
            "lead",
            travel_estimator,
            top_n=1,
            include_infeasible=False,
        )
        if recommendations:
            return candidate_session, trial_state

    return None, state


def _candidate_start_times(window_start: time, window_end: time, duration: timedelta) -> list[time]:
    start_minutes = window_start.hour * 60 + window_start.minute
    end_minutes = window_end.hour * 60 + window_end.minute
    duration_minutes = int(duration.total_seconds() // 60)
    latest_start_minutes = max(start_minutes, min(end_minutes - duration_minutes, 23 * 60 + 59))
    candidate_minutes = sorted({start_minutes, latest_start_minutes})
    return [time(minute_value // 60, minute_value % 60) for minute_value in candidate_minutes if 0 <= minute_value < 24 * 60]


def _service_user_has_session(state: PlanningState, service_user_id: str) -> bool:
    return any(
        session.service_user_id == service_user_id and session.status not in {"cancelled", "rejected"}
        for session in state.sessions.values()
    )


def _infer_period(state: PlanningState) -> tuple[date, date]:
    if state.sessions:
        starts = [session.start.date() for session in state.sessions.values()]
        ends = [session.end.date() for session in state.sessions.values()]
        return min(starts), max(ends)
    month_start = state.decision_time.date().replace(day=1)
    next_month_probe = month_start.replace(day=28) + timedelta(days=4)
    month_end = next_month_probe.replace(day=1) - timedelta(days=1)
    return month_start, month_end


def _weeks_in_period(period_start: date, period_end: date) -> list[date]:
    first_monday = period_start - timedelta(days=period_start.weekday())
    weeks = []
    current = first_monday
    while current <= period_end:
        weeks.append(current)
        current += timedelta(days=7)
    return weeks
