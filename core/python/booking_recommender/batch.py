"""Sequential batch planning helpers."""

from __future__ import annotations

from dataclasses import dataclass

from .models import AssignAction, PlanningState, Recommendation
from .planner import rank_session_candidates
from .scoring import TravelEstimator
from .transitions import apply_assign_action


@dataclass(frozen=True)
class PlannedAssignment:
    recommendation: Recommendation
    accepted: bool
    reason: str
    candidates: tuple[Recommendation, ...] = ()


@dataclass(frozen=True)
class GreedyPlan:
    final_state: PlanningState
    assignments: tuple[PlannedAssignment, ...]


def greedy_allocate_open_sessions(
    state: PlanningState,
    travel_estimator: TravelEstimator,
    top_n: int = 10,
    include_infeasible: bool = False,
) -> GreedyPlan:
    """Build a simple sequential allocation plan.

    This is the baseline policy, not the final optimizer. It processes sessions
    in chronological order, chooses the top feasible worker for each open role,
    applies that provisional assignment, then moves to the next decision.
    """

    current_state = state
    assignments: list[PlannedAssignment] = []

    for session in sorted(current_state.sessions.values(), key=lambda item: (item.start, item.id)):
        if session.status in {"cancelled", "rejected", "confirmed"}:
            continue
        while True:
            current_session = current_state.sessions[session.id]
            if len(current_session.assigned) >= current_session.required_worker_count:
                break
            role = _next_role(current_session)
            recommendations = rank_session_candidates(
                state=current_state,
                session_id=current_session.id,
                role=role,
                travel_estimator=travel_estimator,
                top_n=top_n,
                include_infeasible=include_infeasible,
            )
            feasible_recommendations = tuple(
                recommendation
                for recommendation in recommendations
                if not recommendation.hard_constraint_failures
            )
            if not feasible_recommendations:
                assignments.append(
                    PlannedAssignment(
                        recommendation=Recommendation(
                            action=AssignAction(worker_id="", session_id=current_session.id, role=role),
                            score=0,
                            hard_constraint_failures=("no feasible candidates",),
                            reasons=(),
                            warnings=(),
                            components={},
                        ),
                        accepted=False,
                        reason="no feasible candidates",
                        candidates=tuple(recommendations),
                    )
                )
                break
            selected = feasible_recommendations[0]
            current_state = apply_assign_action(current_state, selected.action)
            assignments.append(
                PlannedAssignment(
                    recommendation=selected,
                    accepted=True,
                    reason="selected top feasible candidate",
                    candidates=tuple(recommendations),
                )
            )

    return GreedyPlan(final_state=current_state, assignments=tuple(assignments))


def _next_role(session) -> str:
    roles = {allocation.role for allocation in session.assigned}
    if "lead" not in roles:
        return "lead"
    return "second"
