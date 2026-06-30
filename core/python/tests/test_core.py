from __future__ import annotations

import unittest
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from booking_recommender import (
    Allocation,
    AssignAction,
    AvailabilityWindow,
    Compatibility,
    FamilyRequest,
    FamilyRequestArrivedEvent,
    ImportAuditMetadata,
    Location,
    PairHistory,
    PlanningState,
    ServiceUser,
    Session,
    SourceReference,
    TenantRules,
    TravelEstimate,
    TravelMode,
    Worker,
    WorkerUnavailableEvent,
    apply_assign_action,
    apply_exogenous_event,
    greedy_allocate_open_sessions,
    rank_session_candidates,
    score_assign_action,
)


class FixedTravelEstimator:
    def __init__(self, minutes: float = 10, mode: TravelMode = TravelMode.DRIVE):
        self.minutes = minutes
        self.mode = mode

    def estimate(self, *, worker, origin, destination, departure_time):
        return TravelEstimate(mode=self.mode, minutes=self.minutes, confidence=1.0)


class BookingRecommenderCoreTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 7, 10, 14, 0)
        self.end = datetime(2026, 7, 10, 17, 0)
        self.home = Location(label="Worker home", area="M1")
        self.session_location = Location(label="Session location", area="M1")
        self.worker = Worker(
            id="worker-1",
            name="Worker One",
            home_location=self.home,
            travel_modes=frozenset({TravelMode.DRIVE, TravelMode.WALK}),
            has_car=True,
            skills=frozenset({"personal-care"}),
            willing_areas=frozenset({"M1"}),
            availability=(AvailabilityWindow(self.start, self.end),),
            max_hours=20,
            reliability_score=0.8,
        )
        self.second_worker = replace(
            self.worker,
            id="worker-2",
            name="Worker Two",
            current_allocations=(),
            current_hours=0,
        )
        self.service_user = ServiceUser(
            id="su-1",
            name="Service User One",
            home_location=Location(label="Service user home", area="M1"),
            support_level="standard",
            required_skills=frozenset({"personal-care"}),
            preferred_worker_ids=frozenset({"worker-1"}),
        )
        self.session = Session(
            id="session-1",
            service_user_id="su-1",
            start=self.start,
            end=self.end,
            location=self.session_location,
            required_worker_count=1,
        )
        self.state = PlanningState(
            tenant_rules=TenantRules(tenant_id="tenant-1"),
            decision_time=self.start - timedelta(days=1),
            workers={self.worker.id: self.worker, self.second_worker.id: self.second_worker},
            service_users={self.service_user.id: self.service_user},
            sessions={self.session.id: self.session},
            compatibility={
                ("worker-1", "su-1"): Compatibility("worker-1", "su-1", "appropriate")
            },
            pair_history={
                ("worker-1", "su-1"): PairHistory(
                    "worker-1",
                    "su-1",
                    completed_sessions=4,
                    positive_feedback_count=2,
                )
            },
        )

    def test_score_exposes_reasons_components_and_boundary_availability(self):
        recommendation = score_assign_action(
            self.state,
            AssignAction("worker-1", "session-1", "lead"),
            FixedTravelEstimator(minutes=12, mode=TravelMode.DRIVE),
        )

        self.assertEqual((), recommendation.hard_constraint_failures)
        self.assertGreater(recommendation.score, 0)
        self.assertIn("explicit_compatible", recommendation.components)
        self.assertIn("travel_fit", recommendation.components)
        self.assertTrue(any("travel estimate" in reason for reason in recommendation.reasons))

    def test_unknown_ids_missing_service_user_and_full_sessions_are_inspectable(self):
        missing_worker = score_assign_action(
            self.state,
            AssignAction("missing-worker", "session-1", "lead"),
            FixedTravelEstimator(),
        )
        self.assertEqual(("unknown worker id",), missing_worker.hard_constraint_failures)

        missing_session = score_assign_action(
            self.state,
            AssignAction("worker-1", "missing-session", "lead"),
            FixedTravelEstimator(),
        )
        self.assertEqual(("unknown session id",), missing_session.hard_constraint_failures)

        broken_state = replace(
            self.state,
            sessions={
                "session-1": replace(self.session, service_user_id="missing-service-user")
            },
        )
        broken = score_assign_action(
            broken_state,
            AssignAction("worker-1", "session-1", "lead"),
            FixedTravelEstimator(),
        )
        self.assertEqual(("missing service user for session",), broken.hard_constraint_failures)

        full_state = apply_assign_action(self.state, AssignAction("worker-1", "session-1", "lead"))
        full = score_assign_action(
            full_state,
            AssignAction("worker-2", "session-1", "second"),
            FixedTravelEstimator(),
        )
        self.assertIn("session is already fully allocated", full.hard_constraint_failures)

    def test_travel_mode_and_driver_route_are_hard_constraints(self):
        bus_only_worker = replace(
            self.worker,
            travel_modes=frozenset({TravelMode.BUS}),
            has_car=False,
        )
        bus_state = replace(self.state, workers={"worker-1": bus_only_worker})
        drive_recommendation = score_assign_action(
            bus_state,
            AssignAction("worker-1", "session-1", "lead"),
            FixedTravelEstimator(mode=TravelMode.DRIVE),
        )
        self.assertIn(
            "travel estimate mode drive is not allowed for worker",
            drive_recommendation.hard_constraint_failures,
        )

        driver_session = replace(self.session, requires_driver=True)
        driver_state = replace(self.state, sessions={"session-1": driver_session})
        walk_recommendation = score_assign_action(
            driver_state,
            AssignAction("worker-1", "session-1", "lead"),
            FixedTravelEstimator(mode=TravelMode.WALK),
        )
        self.assertIn(
            "session requires a drive-capable route",
            walk_recommendation.hard_constraint_failures,
        )

    def test_missing_worker_availability_blocks_by_default(self):
        worker_without_availability = replace(self.worker, availability=())
        state = replace(self.state, workers={"worker-1": worker_without_availability})

        recommendation = score_assign_action(
            state,
            AssignAction("worker-1", "session-1", "lead"),
            FixedTravelEstimator(mode=TravelMode.DRIVE),
        )

        self.assertIn("missing_worker_availability", recommendation.hard_constraint_failures)

        opt_in_state = replace(
            state,
            tenant_rules=replace(state.tenant_rules, allow_missing_worker_availability=True),
        )
        opt_in = score_assign_action(
            opt_in_state,
            AssignAction("worker-1", "session-1", "lead"),
            FixedTravelEstimator(mode=TravelMode.DRIVE),
        )
        self.assertNotIn("missing_worker_availability", opt_in.hard_constraint_failures)

    def test_assignment_transition_ignores_duplicate_unknown_and_full_actions(self):
        action = AssignAction("worker-1", "session-1", "lead")
        assigned_state = apply_assign_action(self.state, action)
        duplicate_state = apply_assign_action(assigned_state, action)

        self.assertEqual(assigned_state, duplicate_state)
        self.assertEqual(("session-1",), duplicate_state.workers["worker-1"].current_allocations)
        self.assertEqual(3.0, duplicate_state.workers["worker-1"].current_hours)
        self.assertEqual(1, len(duplicate_state.sessions["session-1"].assigned))

        self.assertEqual(
            duplicate_state,
            apply_assign_action(duplicate_state, AssignAction("missing", "session-1", "lead")),
        )
        self.assertEqual(
            duplicate_state,
            apply_assign_action(duplicate_state, AssignAction("worker-2", "session-1", "second")),
        )

    def test_greedy_allocate_open_sessions_handles_multi_worker_session(self):
        multi_session = replace(self.session, required_worker_count=2)
        state = replace(self.state, sessions={"session-1": multi_session})

        plan = greedy_allocate_open_sessions(state, FixedTravelEstimator(mode=TravelMode.DRIVE))

        self.assertEqual(2, len([item for item in plan.assignments if item.accepted]))
        final_session = plan.final_state.sessions["session-1"]
        self.assertEqual("allocated", final_session.status)
        self.assertEqual({"lead", "second"}, {allocation.role for allocation in final_session.assigned})
        self.assertEqual(2, len({allocation.worker_id for allocation in final_session.assigned}))

    def test_unavailability_repair_removes_assignment_and_worker_state(self):
        assigned_state = apply_assign_action(
            self.state,
            AssignAction("worker-1", "session-1", "lead"),
        )
        event = WorkerUnavailableEvent(
            id="event-1",
            observed_at=self.start - timedelta(hours=1),
            worker_id="worker-1",
            unavailable_window=AvailabilityWindow(self.start, self.end),
        )

        repaired = apply_exogenous_event(assigned_state, event)

        self.assertEqual((), repaired.sessions["session-1"].assigned)
        self.assertEqual("unallocated", repaired.sessions["session-1"].status)
        self.assertEqual((), repaired.workers["worker-1"].current_allocations)
        self.assertEqual(0.0, repaired.workers["worker-1"].current_hours)
        self.assertTrue(repaired.data_quality_warnings)

    def test_family_request_changes_feasibility_after_arrival(self):
        request = FamilyRequest(
            id="request-1",
            service_user_id="su-1",
            avoided_worker_ids=frozenset({"worker-1"}),
        )
        event = FamilyRequestArrivedEvent(
            id="event-1",
            observed_at=self.start - timedelta(hours=1),
            request=request,
        )
        updated = apply_exogenous_event(self.state, event)

        recommendation = score_assign_action(
            updated,
            AssignAction("worker-1", "session-1", "lead"),
            FixedTravelEstimator(),
        )

        self.assertIn(
            "family/date request says to avoid this worker",
            recommendation.hard_constraint_failures,
        )

    def test_rank_session_candidates_can_include_infeasible_results(self):
        unavailable_worker = replace(
            self.worker,
            availability=(AvailabilityWindow(self.start - timedelta(hours=4), self.start),),
        )
        state = replace(self.state, workers={"worker-1": unavailable_worker})

        feasible_only = rank_session_candidates(state, "session-1", "lead", FixedTravelEstimator())
        debug_results = rank_session_candidates(
            state,
            "session-1",
            "lead",
            FixedTravelEstimator(),
            include_infeasible=True,
        )

        self.assertEqual([], feasible_only)
        self.assertEqual(1, len(debug_results))
        self.assertIn(
            "worker is not available for the full session",
            debug_results[0].hard_constraint_failures,
        )

    def test_import_audit_metadata_preserves_source_traceability(self):
        source_reference = SourceReference(
            import_run_id="import-run-1",
            table="workers",
            source_ref="synthetic/workers.csv",
            row_number=2,
        )
        audit = ImportAuditMetadata(
            import_run_id="import-run-1",
            imported_at=self.start - timedelta(days=2),
            source_system="standardized-csv",
            source_refs=("synthetic/workers.csv", "synthetic/sessions.csv"),
            row_counts={"workers": 2, "sessions": 1},
            validation_warnings=("synthetic warning for test coverage",),
            generated_state_snapshot_id="snapshot-1",
        )
        traced_worker = replace(self.worker, source_reference=source_reference)
        traced_state = replace(
            self.state,
            workers={traced_worker.id: traced_worker},
            import_audit=audit,
        )

        self.assertEqual(audit, traced_state.import_audit)
        self.assertEqual(source_reference, traced_state.workers["worker-1"].source_reference)
        self.assertEqual("synthetic/workers.csv", traced_state.import_audit.source_refs[0])


if __name__ == "__main__":
    unittest.main()
