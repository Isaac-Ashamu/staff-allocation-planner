from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from booking_recommender import (
    AvailabilityWindow,
    DayPattern,
    FamilyRequest,
    Location,
    PlanningState,
    ServiceUser,
    Session,
    SourceReference,
    TenantRules,
    TravelMode,
    Worker,
    greedy_allocate_open_sessions,
    provision_sessions_from_family_requests,
)
from booking_recommender.travel import FallbackTravelEstimator

TZ = timezone(timedelta(hours=1))


def _worker(worker_id: str, *, available: bool = True) -> Worker:
    availability = (
        (AvailabilityWindow(datetime(2026, 7, 1, 0, 0, tzinfo=TZ), datetime(2026, 7, 31, 23, 59, tzinfo=TZ)),)
        if available
        else ()
    )
    return Worker(
        id=worker_id,
        name=worker_id,
        home_location=Location(label="home", area="M1"),
        travel_modes=frozenset({TravelMode.WALK}),
        availability=availability,
    )


def _service_user(service_user_id: str) -> ServiceUser:
    return ServiceUser(
        id=service_user_id,
        name=service_user_id,
        home_location=Location(label="home", area="M1"),
        support_level="standard",
    )


def _state(**kwargs) -> PlanningState:
    defaults = dict(
        tenant_rules=TenantRules(tenant_id="tenant-1"),
        decision_time=datetime(2026, 7, 1, 9, 0, tzinfo=TZ),
        workers={},
        service_users={},
        sessions={},
        family_requests=(),
    )
    defaults.update(kwargs)
    return PlanningState(**defaults)


class ProvisioningTests(unittest.TestCase):
    def test_feasible_request_is_created_and_then_staffed_by_greedy_allocation(self):
        worker = _worker("worker-1")
        su = _service_user("su-1")
        request = FamilyRequest(
            id="request-1",
            service_user_id="su-1",
            preferred_days=(5,),
            target_sessions_per_week=1,
            target_session_duration_minutes=180,
            source_reference=SourceReference(
                import_run_id="import-1", table="family_requests", source_ref="requests.csv", row_number=2
            ),
        )
        state = _state(workers={worker.id: worker}, service_users={su.id: su}, family_requests=(request,))

        result = provision_sessions_from_family_requests(state, FallbackTravelEstimator())

        self.assertEqual(4, len(result.created))
        self.assertTrue(all(session.start.weekday() == 5 for session in result.created))
        # The last Monday-aligned week touching July has its Saturday fall in
        # August, outside the inferred period -- correctly reported as
        # unscheduled rather than silently skipped.
        self.assertEqual(1, len(result.unscheduled))
        self.assertEqual(4, len(result.final_state.sessions))
        for session in result.final_state.sessions.values():
            self.assertEqual("family_requests", session.source_reference.table)
            self.assertEqual("requests.csv", session.source_reference.source_ref)

        plan = greedy_allocate_open_sessions(result.final_state, FallbackTravelEstimator())
        accepted = [item for item in plan.assignments if item.accepted]
        self.assertEqual(4, len(accepted))
        self.assertTrue(all(item.recommendation.action.worker_id == "worker-1" for item in accepted))

    def test_service_user_with_existing_session_is_skipped(self):
        worker = _worker("worker-1")
        su = _service_user("su-1")
        existing = Session(
            id="session-existing",
            service_user_id="su-1",
            start=datetime(2026, 7, 3, 10, 0, tzinfo=TZ),
            end=datetime(2026, 7, 3, 12, 0, tzinfo=TZ),
            location=Location(label="home"),
            required_worker_count=1,
        )
        request = FamilyRequest(
            id="request-1",
            service_user_id="su-1",
            preferred_days=(5,),
            target_sessions_per_week=1,
            target_session_duration_minutes=180,
        )
        state = _state(
            workers={worker.id: worker},
            service_users={su.id: su},
            sessions={existing.id: existing},
            family_requests=(request,),
        )

        result = provision_sessions_from_family_requests(state, FallbackTravelEstimator())

        self.assertEqual((), result.created)
        self.assertEqual((), result.unscheduled)
        self.assertEqual(1, len(result.final_state.sessions))

    def test_pick_one_of_two_days_prefers_the_feasible_day(self):
        # Worker is only available on Sundays; request allows Saturday or Sunday, 1/week.
        sunday_only = Worker(
            id="worker-1",
            name="worker-1",
            home_location=Location(label="home", area="M1"),
            travel_modes=frozenset({TravelMode.WALK}),
            availability=tuple(
                AvailabilityWindow(
                    datetime(2026, 7, day, 8, 0, tzinfo=TZ),
                    datetime(2026, 7, day, 20, 0, tzinfo=TZ),
                )
                for day in (5, 12, 19, 26)  # the Sundays in July 2026
            ),
        )
        su = _service_user("su-1")
        request = FamilyRequest(
            id="request-1",
            service_user_id="su-1",
            preferred_days=(5, 6),  # Saturday=5, Sunday=6, Saturday preferred
            target_sessions_per_week=1,
            target_session_duration_minutes=120,
        )
        state = _state(
            workers={sunday_only.id: sunday_only}, service_users={su.id: su}, family_requests=(request,)
        )

        result = provision_sessions_from_family_requests(state, FallbackTravelEstimator())

        self.assertTrue(result.created)
        self.assertTrue(all(session.start.weekday() == 6 for session in result.created))

    def test_priority_order_is_respected_when_every_candidate_day_is_feasible(self):
        # Regression test: when a worker is available on both Saturday and Sunday
        # every week, provisioning must honor the family's stated priority order
        # (Sunday listed first here) rather than always taking the numerically
        # lowest weekday -- the old `sorted(preferred_days)` behavior would have
        # picked Saturday (5 < 6) here regardless of what order was requested,
        # which is exactly the bug real weekend-preferred requests hit.
        worker = _worker("worker-1")
        su = _service_user("su-1")
        sunday_preferred = FamilyRequest(
            id="request-sunday-preferred",
            service_user_id="su-1",
            preferred_days=(6, 5),  # Sunday preferred, Saturday fallback
            target_sessions_per_week=1,
            target_session_duration_minutes=120,
        )
        state = _state(workers={worker.id: worker}, service_users={su.id: su}, family_requests=(sunday_preferred,))

        result = provision_sessions_from_family_requests(state, FallbackTravelEstimator())

        self.assertEqual(4, len(result.created))
        self.assertTrue(
            all(session.start.weekday() == 6 for session in result.created),
            f"expected every session on Sunday (weekday 6), got {[s.start.weekday() for s in result.created]}",
        )

    def test_no_feasible_worker_leaves_request_unscheduled_not_dropped(self):
        worker = _worker("worker-1", available=False)
        su = _service_user("su-1")
        request = FamilyRequest(
            id="request-1",
            service_user_id="su-1",
            preferred_days=(5,),
            target_sessions_per_week=1,
            target_session_duration_minutes=180,
        )
        state = _state(workers={worker.id: worker}, service_users={su.id: su}, family_requests=(request,))

        result = provision_sessions_from_family_requests(state, FallbackTravelEstimator())

        self.assertEqual((), result.created)
        self.assertEqual(5, len(result.unscheduled))
        self.assertTrue(all(item.family_request_id == "request-1" for item in result.unscheduled))
        self.assertEqual(0, len(result.final_state.sessions))

    def test_all_listed_days_are_used_when_sessions_per_week_matches_day_count(self):
        worker = _worker("worker-1")
        su = _service_user("su-1")
        request = FamilyRequest(
            id="request-1",
            service_user_id="su-1",
            preferred_days=(0, 3),  # Monday and Thursday
            target_sessions_per_week=2,
            target_session_duration_minutes=60,
        )
        state = _state(workers={worker.id: worker}, service_users={su.id: su}, family_requests=(request,))

        result = provision_sessions_from_family_requests(state, FallbackTravelEstimator())

        weekdays_used = {session.start.weekday() for session in result.created}
        self.assertEqual({0, 3}, weekdays_used)

    def test_midweek_plus_weekend_day_patterns_each_get_their_own_slot(self):
        # The real bug this structure fixes: a flat preferred_days=(0,1,5,6) with
        # target_sessions_per_week=2 would (with the old ascending-first-fit logic, or
        # even the priority-order fix alone) risk both weekly sessions landing in the
        # same category. Two DayPatterns guarantee one from each category every week.
        worker = _worker("worker-1")
        su = _service_user("su-1")
        request = FamilyRequest(
            id="request-1",
            service_user_id="su-1",
            target_session_duration_minutes=180,
            day_patterns=(
                DayPattern(days=(0, 1, 3, 4), sessions_per_week=1),  # midweek, not Wednesday
                DayPattern(days=(5, 6), sessions_per_week=1),  # weekend
            ),
        )
        state = _state(workers={worker.id: worker}, service_users={su.id: su}, family_requests=(request,))

        result = provision_sessions_from_family_requests(state, FallbackTravelEstimator())

        weekdays = [session.start.weekday() for session in result.created]
        # No week should ever get 2 midweek or 2 weekend sessions -- that's exactly the bug a
        # single flat list + total count risks (both weekly slots silently landing in one
        # category). The trailing partial week of the month legitimately has no in-period
        # weekend day, so weeks may fall short of 1+1, but must never exceed 1 per category.
        weeks: dict[tuple, list[int]] = {}
        for session in result.created:
            week_key = session.start.isocalendar()[:2]
            weeks.setdefault(week_key, []).append(session.start.weekday())
        full_weeks = 0
        for week_key, days in weeks.items():
            midweek_count = sum(1 for d in days if d in (0, 1, 3, 4))
            weekend_count = sum(1 for d in days if d in (5, 6))
            self.assertLessEqual(midweek_count, 1, f"week {week_key}: too many midweek sessions, got {days}")
            self.assertLessEqual(weekend_count, 1, f"week {week_key}: too many weekend sessions, got {days}")
            if midweek_count == 1 and weekend_count == 1:
                full_weeks += 1
        self.assertGreaterEqual(full_weeks, 3, "expected most weeks to place one midweek + one weekend session")
        self.assertNotIn(2, weekdays)  # never Wednesday

    def test_requests_missing_sizing_information_are_left_alone(self):
        worker = _worker("worker-1")
        su = _service_user("su-1")
        request = FamilyRequest(id="request-1", service_user_id="su-1", notes="awaiting response from mum")
        state = _state(workers={worker.id: worker}, service_users={su.id: su}, family_requests=(request,))

        result = provision_sessions_from_family_requests(state, FallbackTravelEstimator())

        self.assertEqual((), result.created)
        self.assertEqual((), result.unscheduled)


if __name__ == "__main__":
    unittest.main()
