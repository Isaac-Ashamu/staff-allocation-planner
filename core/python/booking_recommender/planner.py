"""Planning helpers for ranking feasible assignment actions."""

from __future__ import annotations

from .models import AssignAction, PlanningState, Recommendation
from .scoring import TravelEstimator, score_assign_action


def rank_session_candidates(
    state: PlanningState,
    session_id: str,
    role: str,
    travel_estimator: TravelEstimator,
    top_n: int = 10,
    include_infeasible: bool = False,
) -> list[Recommendation]:
    recommendations: list[Recommendation] = []

    for worker_id in state.workers:
        recommendation = score_assign_action(
            state=state,
            action=AssignAction(worker_id=worker_id, session_id=session_id, role=role),
            travel_estimator=travel_estimator,
        )
        if include_infeasible or not recommendation.hard_constraint_failures:
            recommendations.append(recommendation)

    recommendations.sort(key=lambda item: item.score, reverse=True)
    return recommendations[:top_n]
