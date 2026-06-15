from datetime import datetime
import unittest

from booking_recommender import (
    Allocation,
    AssignAction,
    AvailabilityWindow,
    GeocodedHeuristicTravelEstimator,
    HeuristicTravelEstimator,
    Location,
    PlanningState,
    ServiceUser,
    Session,
    TenantRules,
    TravelMode,
    Worker,
    WorkerUnavailableEvent,
    apply_assign_action,
    apply_exogenous_event,
    rank_session_candidates,
)


class ScoringTests(unittest.TestCase):
    def test_driver_requirement_blocks_non_driver(self):
        decision_time = datetime(2026, 7, 1, 8, 0)
        worker = Worker(
            id="w1",
            name="No Car",
            home_location=Location("home", area="M"),
            travel_modes=frozenset({TravelMode.BUS}),
            has_car=False,
            availability=(AvailabilityWindow(decision_time, datetime(2026, 7, 1, 18, 0)),),
        )
        service_user = ServiceUser(
            id="su1",
            name="Service User",
            home_location=Location("home", area="M"),
            support_level="1:1",
            requires_driver=True,
        )
        session = Session(
            id="s1",
            service_user_id="su1",
            start=datetime(2026, 7, 1, 12, 0),
            end=datetime(2026, 7, 1, 14, 0),
            location=service_user.home_location,
            required_worker_count=1,
        )
        state = PlanningState(
            tenant_rules=TenantRules("test"),
            decision_time=decision_time,
            workers={"w1": worker},
            service_users={"su1": service_user},
            sessions={"s1": session},
        )

        recommendations = rank_session_candidates(
            state,
            "s1",
            "lead",
            HeuristicTravelEstimator(),
            include_infeasible=True,
        )

        self.assertIn("session requires a driver/car", recommendations[0].hard_constraint_failures)

    def test_assign_action_updates_session_and_worker_state(self):
        state = self._basic_state()
        next_state = apply_assign_action(state, AssignAction("w1", "s1", "lead"))

        self.assertEqual(next_state.sessions["s1"].status, "allocated")
        self.assertEqual(next_state.sessions["s1"].assigned[0].worker_id, "w1")
        self.assertIn("s1", next_state.workers["w1"].current_allocations)

    def test_worker_unavailable_event_repairs_affected_session(self):
        state = self._basic_state()
        assigned_state = apply_assign_action(state, AssignAction("w1", "s1", "lead"))
        event = WorkerUnavailableEvent(
            id="e1",
            observed_at=datetime(2026, 7, 1, 9, 0),
            worker_id="w1",
            unavailable_window=AvailabilityWindow(
                datetime(2026, 7, 1, 11, 0),
                datetime(2026, 7, 1, 15, 0),
            ),
        )

        repaired_state = apply_exogenous_event(assigned_state, event)

        self.assertEqual(repaired_state.sessions["s1"].status, "unallocated")
        self.assertEqual(repaired_state.sessions["s1"].assigned, ())

    def test_minimum_buffer_blocks_too_close_sessions(self):
        state = self._basic_state()
        assigned_state = apply_assign_action( state, AssignAction("w1", "s1", "lead"))
        close_session = Session(
            id="s2",
            service_user_id="su1",
            start=datetime(2026, 7, 1, 14, 5),
            end=datetime(2026, 7, 1, 15, 0),
            location=state.service_users["su1"].home_location,
            required_worker_count=1,
        )
        sessions = dict(assigned_state.sessions)
        sessions["s2"] = close_session
        assigned_state = PlanningState(
            tenant_rules=assigned_state.tenant_rules,
            decision_time=assigned_state.decision_time,
            workers=assigned_state.workers,
            service_users=assigned_state.service_users,
            sessions=sessions,
        )

        recommendations = rank_session_candidates(
            assigned_state,
            "s2",
            "lead",
            HeuristicTravelEstimator(),
            include_infeasible=True,
        )

        self.assertIn(
            "worker already has an overlapping or too-close allocated session",
            recommendations[0].hard_constraint_failures,
        )

    def test_scoring_uses_previous_session_as_travel_origin(self):
        decision_time = datetime(2026, 7, 1, 8, 0)
        worker = Worker(
            id="w1",
            name="Worker",
            home_location=Location("far home", latitude=53.35, longitude=-2.35, area="WA"),
            travel_modes=frozenset({TravelMode.DRIVE}),
            has_car=True,
            availability=(AvailabilityWindow(decision_time, datetime(2026, 7, 1, 18, 0)),),
            current_allocations=("s0",),
        )
        previous_location = Location("previous session", latitude=53.473, longitude=-2.244, area="M")
        service_user = ServiceUser(
            id="su1",
            name="Service User",
            home_location=Location("candidate", latitude=53.474, longitude=-2.245, area="M"),
            support_level="1:1",
        )
        previous = Session(
            id="s0",
            service_user_id="su1",
            start=datetime(2026, 7, 1, 9, 0),
            end=datetime(2026, 7, 1, 10, 0),
            location=previous_location,
            required_worker_count=1,
            assigned=(Allocation("w1", "lead"),),
            status="allocated",
        )
        candidate = Session(
            id="s1",
            service_user_id="su1",
            start=datetime(2026, 7, 1, 12, 0),
            end=datetime(2026, 7, 1, 13, 0),
            location=service_user.home_location,
            required_worker_count=1,
        )
        state = PlanningState(
            tenant_rules=TenantRules("test"),
            decision_time=decision_time,
            workers={"w1": worker},
            service_users={"su1": service_user},
            sessions={"s0": previous, "s1": candidate},
        )

        recommendations = rank_session_candidates(state, "s1", "lead", HeuristicTravelEstimator())

        self.assertIn("travel origin is previous session", recommendations[0].warnings[0])
        self.assertLess(recommendations[0].travel_estimate.minutes, 10)

    def test_geocoded_heuristic_uses_postcode_distance_when_coordinates_available(self):
        worker = Worker(
            id="w1",
            name="Worker",
            home_location=Location("home", latitude=53.4808, longitude=-2.2426, area="M"),
            travel_modes=frozenset({TravelMode.PUBLIC_TRANSPORT}),
        )
        estimator = GeocodedHeuristicTravelEstimator()

        near = estimator.estimate(
            worker,
            worker.home_location,
            Location("near", latitude=53.483, longitude=-2.244, area="M"),
            datetime(2026, 7, 1, 12, 0),
        )
        far = estimator.estimate(
            worker,
            worker.home_location,
            Location("far", latitude=53.4084, longitude=-2.9916, area="L"),
            datetime(2026, 7, 1, 12, 0),
        )

        self.assertEqual(near.provider, "geocoded_heuristic")
        self.assertLess(near.minutes, far.minutes)

    def _basic_state(self):
        decision_time = datetime(2026, 7, 1, 8, 0)
        worker = Worker(
            id="w1",
            name="Worker",
            home_location=Location("home", area="M"),
            travel_modes=frozenset({TravelMode.BUS}),
            availability=(AvailabilityWindow(decision_time, datetime(2026, 7, 1, 18, 0)),),
        )
        service_user = ServiceUser(
            id="su1",
            name="Service User",
            home_location=Location("home", area="M"),
            support_level="1:1",
        )
        session = Session(
            id="s1",
            service_user_id="su1",
            start=datetime(2026, 7, 1, 12, 0),
            end=datetime(2026, 7, 1, 14, 0),
            location=service_user.home_location,
            required_worker_count=1,
        )
        return PlanningState(
            tenant_rules=TenantRules("test"),
            decision_time=decision_time,
            workers={"w1": worker},
            service_users={"su1": service_user},
            sessions={"s1": session},
        )


if __name__ == "__main__":
    unittest.main()
