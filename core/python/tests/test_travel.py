from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from booking_recommender import (
    AssignAction,
    AvailabilityWindow,
    CachedTravelEstimator,
    FallbackTravelEstimator,
    FallbackTravelProvider,
    GoogleRoutesProvider,
    InMemoryTravelEstimateCache,
    ImportReport,
    Location,
    ModeSpecificTravelProvider,
    OSRMProvider,
    OpenRouteServiceProvider,
    PlanningState,
    PostcodeDistanceProvider,
    PublicTransportProvider,
    SQLiteTravelEstimateCache,
    ServiceUser,
    Session,
    StaticPostcodeGeocoder,
    TenantRules,
    TravelConfig,
    TravelEstimate,
    TravelMode,
    Worker,
    allocation_report_to_dict,
    build_allocation_report,
    location_cache_key,
    normalize_postcode,
    postcode_area,
    resolve_location_for_travel,
    score_assign_action,
    time_bucket,
    travel_cache_key,
    travel_location_issues,
)


class RecordingProvider:
    name = "recording"

    def __init__(self, estimate: TravelEstimate | None = None, error: Exception | None = None):
        self.estimate_value = estimate or TravelEstimate(mode=TravelMode.DRIVE, minutes=18, confidence=0.9, provider=self.name)
        self.error = error
        self.calls = []

    def estimate(self, *, origin, destination, mode, departure_time):
        self.calls.append((origin, destination, mode, departure_time))
        if self.error:
            raise self.error
        return replace(self.estimate_value, mode=mode)


class WrongModeProvider:
    name = "wrong-mode"

    def estimate(self, *, origin, destination, mode, departure_time):
        return TravelEstimate(mode=TravelMode.DRIVE, minutes=12, confidence=0.9, provider=self.name)


class CountingDriveProvider:
    name = "counting_drive"

    def __init__(self):
        self.calls = 0

    def estimate(self, *, origin, destination, mode, departure_time):
        self.calls += 1
        return TravelEstimate(
            origin=origin,
            destination=destination,
            mode=TravelMode.DRIVE,
            departure_time=departure_time,
            duration_minutes=10,
            provider=self.name,
            confidence="high",
        )


class TravelSupportTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 7, 10, 14, 0)
        self.end = datetime(2026, 7, 10, 17, 0)
        self.worker = Worker(
            id="worker-1",
            name="Worker One",
            home_location=Location(label="Worker home", postcode="m1 1aa", area="M1"),
            travel_modes=frozenset({TravelMode.DRIVE, TravelMode.WALK}),
            has_car=True,
            availability=(AvailabilityWindow(self.start, self.end),),
        )
        self.service_user = ServiceUser(
            id="su-1",
            name="Service User",
            home_location=Location(label="Service user home", postcode="M1 2BB", area="M1"),
            support_level="standard",
        )
        self.session = Session(
            id="session-1",
            service_user_id="su-1",
            start=self.start,
            end=self.end,
            location=Location(label="Session location", postcode="M1 2BB", area="M1"),
            required_worker_count=1,
        )
        self.state = PlanningState(
            tenant_rules=TenantRules(tenant_id="tenant-1"),
            decision_time=self.start - timedelta(days=1),
            workers={self.worker.id: self.worker},
            service_users={self.service_user.id: self.service_user},
            sessions={self.session.id: self.session},
        )

    def test_cache_key_normalizes_location_mode_tenant_and_time_bucket(self):
        origin = Location(label="Home", postcode="m1  1aa")
        destination = Location(label="Visit", postcode="M1 2BB")

        self.assertEqual("M11AA", normalize_postcode("m1  1aa"))
        self.assertEqual("postcode:M11AA", location_cache_key(origin))
        self.assertEqual("2026-07-10T14:30:00", time_bucket(datetime(2026, 7, 10, 14, 44), bucket_minutes=30))
        self.assertEqual(
            (
                "tenant-1",
                "postcode:M11AA",
                "postcode:M12BB",
                "drive",
                "weekday",
                "2026-07-10T14:30:00",
            ),
            travel_cache_key(
                tenant_id="tenant-1",
                origin=origin,
                destination=destination,
                mode=TravelMode.DRIVE,
                departure_time=datetime(2026, 7, 10, 14, 44),
                bucket_minutes=30,
            ),
        )

    def test_provider_interface_contract_with_fallback_provider(self):
        provider = FallbackTravelProvider()

        estimate = provider.estimate(
            origin=Location(label="A", area="M1"),
            destination=Location(label="B", area="M2"),
            mode=TravelMode.DRIVE,
            departure_time=self.start,
        )

        self.assertEqual("fallback", estimate.provider)
        self.assertIn(estimate.confidence, {"low", "medium"})
        self.assertTrue(estimate.warnings)
        self.assertEqual(TravelMode.DRIVE, estimate.mode)

    def test_postcode_distance_provider_is_approximate_and_offline(self):
        provider = PostcodeDistanceProvider()

        estimate = provider.estimate(
            origin=Location(label="A", latitude=53.48, longitude=-2.24),
            destination=Location(label="B", latitude=53.50, longitude=-2.20),
            mode=TravelMode.CYCLE,
            departure_time=self.start,
        )

        self.assertEqual("postcode_distance", estimate.provider)
        self.assertEqual(TravelMode.CYCLE, estimate.mode)
        self.assertGreater(estimate.distance_meters, 0)
        self.assertTrue(any("approximate" in warning for warning in estimate.warnings))

    def test_provider_estimate_is_cached_by_mode_and_bucket(self):
        provider = RecordingProvider()
        cache = InMemoryTravelEstimateCache()
        estimator = CachedTravelEstimator(
            tenant_id="tenant-1",
            provider=provider,
            cache=cache,
            time_bucket_minutes=30,
        )

        first = estimator.estimate(
            worker=self.worker,
            origin=self.worker.home_location,
            destination=self.session.location,
            departure_time=self.start,
        )
        second = estimator.estimate(
            worker=self.worker,
            origin=self.worker.home_location,
            destination=self.session.location,
            departure_time=self.start + timedelta(minutes=20),
        )

        self.assertEqual(1, len(provider.calls))
        self.assertEqual(1, len(cache))
        self.assertEqual("recording", first.provider)
        self.assertIn("travel estimate served from cache", second.warnings)

    def test_sqlite_cache_persists_estimates_across_instances(self):
        key = travel_cache_key(
            tenant_id="tenant-1",
            origin=self.worker.home_location,
            destination=self.session.location,
            mode=TravelMode.DRIVE,
            departure_time=self.start,
        )
        estimate = TravelEstimate(
            mode=TravelMode.DRIVE,
            minutes=17.5,
            distance_km=4.2,
            confidence=0.82,
            provider="test-provider",
            warnings=("synthetic cache warning",),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "travel-cache.sqlite"
            SQLiteTravelEstimateCache(path).set(key, estimate)
            cached = SQLiteTravelEstimateCache(path).get(key)

        self.assertEqual(estimate, cached)

    def test_mode_specific_provider_dispatches_and_fails_by_mode(self):
        drive_provider = RecordingProvider(
            estimate=TravelEstimate(mode=TravelMode.DRIVE, minutes=16, confidence=0.9, provider="drive-provider")
        )
        provider = ModeSpecificTravelProvider({TravelMode.DRIVE: drive_provider})

        estimate = provider.estimate(
            origin=self.worker.home_location,
            destination=self.session.location,
            mode=TravelMode.DRIVE,
            departure_time=self.start,
        )

        self.assertEqual("drive-provider", estimate.provider)
        self.assertEqual(1, len(drive_provider.calls))
        with self.assertRaisesRegex(RuntimeError, "no travel provider configured for mode bus"):
            provider.estimate(
                origin=self.worker.home_location,
                destination=self.session.location,
                mode=TravelMode.BUS,
                departure_time=self.start,
            )

    def test_cache_hit_avoids_provider_call_for_same_worker_session_pair(self):
        provider = CountingDriveProvider()
        estimator = CachedTravelEstimator(
            tenant_id="tenant-1",
            provider=provider,
            time_bucket_minutes=30,
        )

        first = score_assign_action(self.state, AssignAction("worker-1", "session-1", "lead"), estimator)
        second = score_assign_action(self.state, AssignAction("worker-1", "session-1", "lead"), estimator)

        self.assertEqual(1, provider.calls)
        self.assertFalse(first.travel_estimate.cached)
        self.assertTrue(second.travel_estimate.cached)

    def test_external_providers_are_disabled_by_default(self):
        config = TravelConfig()

        with self.assertRaisesRegex(RuntimeError, "OpenRouteService is not configured"):
            OpenRouteServiceProvider(api_key="synthetic", config=config).estimate(
                origin=Location(label="A", latitude=53.48, longitude=-2.24),
                destination=Location(label="B", latitude=53.50, longitude=-2.20),
                mode=TravelMode.DRIVE,
                departure_time=self.start,
            )
        with self.assertRaisesRegex(RuntimeError, "OSRM is not configured"):
            OSRMProvider(base_url="http://localhost:5000", config=config).estimate(
                origin=Location(label="A", latitude=53.48, longitude=-2.24),
                destination=Location(label="B", latitude=53.50, longitude=-2.20),
                mode=TravelMode.DRIVE,
                departure_time=self.start,
            )

    def test_google_provider_cannot_run_unless_explicitly_enabled(self):
        disabled = GoogleRoutesProvider(api_key="synthetic", config=TravelConfig(enable_external_travel=True))

        with self.assertRaisesRegex(RuntimeError, "Google Routes is disabled"):
            disabled.estimate(
                origin=self.worker.home_location,
                destination=self.session.location,
                mode=TravelMode.DRIVE,
                departure_time=self.start,
            )

        enabled = GoogleRoutesProvider(
            api_key="synthetic",
            config=TravelConfig(enable_external_travel=True, enable_google_routes=True),
        )
        with self.assertRaisesRegex(RuntimeError, "later-stage integration"):
            enabled.estimate(
                origin=self.worker.home_location,
                destination=self.session.location,
                mode=TravelMode.DRIVE,
                departure_time=self.start,
            )

    def test_public_transport_mode_does_not_use_driving_estimate_silently(self):
        transit_worker = replace(
            self.worker,
            travel_modes=frozenset({TravelMode.PUBLIC_TRANSPORT}),
            has_car=False,
        )
        state = replace(self.state, workers={"worker-1": transit_worker})
        drive_provider = CountingDriveProvider()
        estimator = CachedTravelEstimator(
            tenant_id="tenant-1",
            providers={
                "public_transport": PublicTransportProvider(configured=False),
                "drive": drive_provider,
            },
            config=TravelConfig(travel_provider_order=("cache", "public_transport", "fallback")),
        )

        recommendation = score_assign_action(state, AssignAction("worker-1", "session-1", "lead"), estimator)

        self.assertEqual(0, drive_provider.calls)
        self.assertEqual(TravelMode.PUBLIC_TRANSPORT, recommendation.travel_estimate.mode)
        self.assertTrue(any("GTFS/public transport provider is not configured" in warning for warning in recommendation.warnings))

    def test_location_quality_and_static_geocoder_do_not_mutate_source_location(self):
        source = Location(label="Source", postcode="m1 2bb")
        geocoder = StaticPostcodeGeocoder(
            {
                "M12BB": Location(
                    label="Resolved",
                    postcode="M1 2BB",
                    latitude=53.48,
                    longitude=-2.24,
                    area="M1",
                )
            }
        )

        resolved, issues = resolve_location_for_travel(source, geocoder=geocoder, context="session location")

        self.assertEqual("m1 2bb", source.postcode)
        self.assertEqual("M12BB", resolved.postcode)
        self.assertEqual(53.48, resolved.latitude)
        self.assertEqual((), issues)
        self.assertEqual("M1", postcode_area("M1 2BB"))
        self.assertEqual("missing_travel_location", travel_location_issues(Location(label="Unknown"))[0].code)

    def test_provider_failure_falls_back_with_visible_warning(self):
        estimator = CachedTravelEstimator(
            tenant_id="tenant-1",
            provider=RecordingProvider(error=RuntimeError("maps unavailable")),
            config=TravelConfig(travel_provider_order=("cache", "recording", "fallback")),
        )

        recommendation = score_assign_action(
            self.state,
            AssignAction("worker-1", "session-1", "lead"),
            estimator,
        )

        self.assertEqual((), recommendation.hard_constraint_failures)
        self.assertEqual("fallback", recommendation.travel_estimate.provider)
        self.assertTrue(any("travel provider recording failed" in warning for warning in recommendation.warnings))

    def test_fallback_travel_warning_appears_in_report(self):
        report = build_allocation_report(
            source_folder="synthetic",
            state=self.state,
            import_report=ImportReport(row_counts={}),
            travel_estimator=FallbackTravelEstimator(),
            top_n=1,
            include_infeasible=True,
        )
        data = allocation_report_to_dict(report)

        self.assertEqual(1, data["travel_summary"]["estimates_from_fallback"])
        self.assertTrue(data["travel_summary"]["all_accepted_assignments_used_fallback"])
        warnings = data["recommendation_details"][0]["recommendation"]["warnings"]
        self.assertTrue(any("fallback travel estimate used" in warning for warning in warnings))

    def test_provider_mode_mismatch_does_not_make_drive_and_bus_interchangeable(self):
        bus_worker = replace(
            self.worker,
            travel_modes=frozenset({TravelMode.BUS}),
            has_car=False,
        )
        state = replace(self.state, workers={"worker-1": bus_worker})
        estimator = CachedTravelEstimator(
            tenant_id="tenant-1",
            provider=WrongModeProvider(),
            config=TravelConfig(travel_provider_order=("cache", "wrong-mode", "fallback")),
        )

        recommendation = score_assign_action(
            state,
            AssignAction("worker-1", "session-1", "lead"),
            estimator,
        )

        self.assertEqual(TravelMode.BUS, recommendation.travel_estimate.mode)
        self.assertNotIn("travel estimate mode drive is not allowed for worker", recommendation.hard_constraint_failures)
        self.assertTrue(any("returned drive for requested bus" in warning for warning in recommendation.warnings))

    def test_slow_travel_remains_a_hard_failure(self):
        estimator = CachedTravelEstimator(
            tenant_id="tenant-1",
            provider=RecordingProvider(
                estimate=TravelEstimate(mode=TravelMode.DRIVE, minutes=200, confidence=0.9, provider="recording")
            ),
        )

        recommendation = score_assign_action(
            self.state,
            AssignAction("worker-1", "session-1", "lead"),
            estimator,
        )

        self.assertIn("estimated travel exceeds tenant max planned travel", recommendation.hard_constraint_failures)


if __name__ == "__main__":
    unittest.main()
