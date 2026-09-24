"""Live travel wiring: postcodes.io geocoding, persistent caches, env-driven config, CLI flag.

Every test mocks urllib so nothing here touches the network. Postcodes are the
public examples from the postcodes.io documentation, not real worker or
service-user addresses.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from booking_recommender import (
    InMemoryPostcodeGeocodeCache,
    Location,
    PostcodesIoGeocoder,
    SQLitePostcodeGeocodeCache,
    TravelConfig,
    build_live_travel_estimator,
    live_travel_config_from_env,
)
from booking_recommender.allocation_cli import main as allocation_main

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "standardized_csv_v1"


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _postcodes_io_reply(request):
    """Answer a bulk lookup: SW1A 1AA and M1 1AE resolve, anything else is null."""
    body = json.loads(request.data.decode("utf-8"))
    known = {
        "SW1A1AA": {"latitude": 51.501009, "longitude": -0.141588, "outcode": "SW1A"},
        "M11AE": {"latitude": 53.477, "longitude": -2.234, "outcode": "M1"},
    }
    result = []
    for query in body["postcodes"]:
        key = query.replace(" ", "").upper()
        result.append({"query": query, "result": known.get(key)})
    return _FakeResponse(json.dumps({"status": 200, "result": result}).encode("utf-8"))


@contextmanager
def mocked_postcodes_io():
    calls: list[str] = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        if "postcodes.io" in request.full_url:
            return _postcodes_io_reply(request)
        raise AssertionError(f"unexpected network call: {request.full_url}")

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        yield calls


class PostcodesIoGeocoderTests(unittest.TestCase):
    def test_resolves_and_caches_known_postcode(self):
        cache = InMemoryPostcodeGeocodeCache()
        geocoder = PostcodesIoGeocoder(cache=cache)
        with mocked_postcodes_io() as calls:
            first = geocoder.resolve("sw1a 1aa")
            second = geocoder.resolve("SW1A1AA")
        self.assertIsNotNone(first)
        self.assertAlmostEqual(first.latitude, 51.501009)
        self.assertEqual(first.area, "SW1A")
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 1, "second lookup must come from cache")

    def test_unknown_postcode_is_cached_as_a_miss(self):
        geocoder = PostcodesIoGeocoder()
        with mocked_postcodes_io() as calls:
            self.assertIsNone(geocoder.resolve("ZZ99 9ZZ"))
            self.assertIsNone(geocoder.resolve("ZZ99 9ZZ"))
        self.assertEqual(len(calls), 1)

    def test_prefetch_batches_and_skips_cached(self):
        geocoder = PostcodesIoGeocoder()
        with mocked_postcodes_io() as calls:
            resolved = geocoder.prefetch(["SW1A 1AA", "M1 1AE", "sw1a1aa", "ZZ99 9ZZ"])
            self.assertEqual(len(calls), 1)
            geocoder.prefetch(["M1 1AE"])  # already cached: no call
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(resolved), {"SW1A1AA", "M11AE", "ZZ999ZZ"})
        self.assertIsNone(resolved["ZZ999ZZ"])

    def test_disabled_geocoder_never_calls_out(self):
        geocoder = PostcodesIoGeocoder(enabled=False)
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            self.assertIsNone(geocoder.resolve("SW1A 1AA"))

    def test_sqlite_cache_survives_a_new_geocoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "geo.sqlite"
            with mocked_postcodes_io() as calls:
                PostcodesIoGeocoder(cache=SQLitePostcodeGeocodeCache(db)).resolve("M1 1AE")
                again = PostcodesIoGeocoder(cache=SQLitePostcodeGeocodeCache(db)).resolve("M1 1AE")
            self.assertEqual(len(calls), 1)
            self.assertEqual(again.area, "M1")
            self.assertIn("M11AE", SQLitePostcodeGeocodeCache(db))


class LiveTravelConfigTests(unittest.TestCase):
    def test_no_credentials_means_no_external_routing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = live_travel_config_from_env()
        self.assertFalse(config.enable_external_travel)
        self.assertEqual(config.travel_provider_order, ("cache", "postcode_distance", "fallback"))

    def test_ors_key_puts_openrouteservice_first_after_cache(self):
        with mock.patch.dict(os.environ, {"ORS_API_KEY": "synthetic"}, clear=True):
            config = live_travel_config_from_env()
        self.assertTrue(config.enable_external_travel)
        self.assertEqual(config.travel_provider_order[:2], ("cache", "openrouteservice"))
        self.assertEqual(config.travel_provider_order[-2:], ("postcode_distance", "fallback"))

    def test_live_estimator_routes_via_ors_when_configured(self):
        ors_calls: list[str] = []

        def fake_urlopen(request, timeout=None):
            if "postcodes.io" in request.full_url:
                return _postcodes_io_reply(request)
            if "openrouteservice" in request.full_url:
                ors_calls.append(request.full_url)
                self.assertEqual(request.get_header("Authorization"), "synthetic")
                return _FakeResponse(json.dumps({"durations": [[0, 1500.0]], "distances": [[0, 12000.0]]}).encode())
            raise AssertionError(request.full_url)

        from booking_recommender import TravelMode, Worker

        worker = Worker(
            id="w1",
            name="Test Worker",
            home_location=Location(label="home", postcode="SW1A 1AA"),
            travel_modes=frozenset({TravelMode.DRIVE}),
            has_car=True,
        )
        destination = Location(label="session", postcode="M1 1AE")
        from datetime import datetime

        with mock.patch.dict(os.environ, {"ORS_API_KEY": "synthetic"}, clear=True), mock.patch(
            "urllib.request.urlopen", side_effect=fake_urlopen
        ):
            estimator = build_live_travel_estimator(tenant_id="t")
            estimate = estimator.estimate(
                worker=worker,
                origin=worker.home_location,
                destination=destination,
                departure_time=datetime(2026, 9, 20, 9, 0),
            )
            repeat = estimator.estimate(
                worker=worker,
                origin=worker.home_location,
                destination=destination,
                departure_time=datetime(2026, 9, 20, 9, 5),
            )
        self.assertEqual(estimate.provider, "openrouteservice")
        self.assertEqual(estimate.duration_minutes, 25.0)
        self.assertEqual(estimate.distance_meters, 12000.0)
        self.assertEqual(len(ors_calls), 1, "same half-hour bucket must be served from cache")
        self.assertTrue(repeat.cached)
        self.assertEqual(estimator.stats.estimates_from_cache, 1)
        self.assertEqual(estimator.stats.external_route_calls, 1)

    def test_osrm_calls_are_not_limited_by_max_external_route_calls(self):
        """OSRM is self-hosted and has no quota to protect, unlike ORS/Google -- so it
        must keep working past max_external_route_calls_per_run, and must not count
        against that budget (stats.external_route_calls should stay ORS/Google-only)."""

        osrm_calls: list[str] = []

        def fake_urlopen(request, timeout=None):
            if "postcodes.io" in request.full_url:
                return _postcodes_io_reply(request)
            if "127.0.0.1" in request.full_url or "localhost" in request.full_url:
                osrm_calls.append(request.full_url)
                return _FakeResponse(json.dumps({"routes": [{"duration": 600.0, "distance": 5000.0}]}).encode())
            raise AssertionError(request.full_url)

        from datetime import datetime

        from booking_recommender import TravelMode, Worker

        worker = Worker(
            id="w1",
            name="Test Worker",
            home_location=Location(label="home", postcode="SW1A 1AA"),
            travel_modes=frozenset({TravelMode.DRIVE}),
            has_car=True,
        )
        destination = Location(label="session", postcode="M1 1AE")

        with mock.patch.dict(
            os.environ, {"OSRM_BASE_URL": "http://127.0.0.1:5001"}, clear=True
        ), mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            estimator = build_live_travel_estimator(
                tenant_id="t", config=TravelConfig(max_external_route_calls_per_run=2)
            )
            for hour in range(9, 14):
                # Different hours land in different half-hour cache buckets, forcing 5
                # genuinely distinct OSRM lookups instead of 5 cache hits on the same one.
                estimate = estimator.estimate(
                    worker=worker,
                    origin=worker.home_location,
                    destination=destination,
                    departure_time=datetime(2026, 9, 20, hour, 0),
                )
                self.assertEqual(estimate.provider, "osrm")

        self.assertEqual(len(osrm_calls), 5, "all 5 distinct routes should reach OSRM despite the cap of 2")
        self.assertEqual(estimator.stats.estimates_from_osrm, 5)
        self.assertEqual(estimator.stats.external_route_calls, 0, "OSRM must not consume the metered-quota counter")

    def test_live_estimator_degrades_to_postcode_distance_when_ors_fails(self):
        def fake_urlopen(request, timeout=None):
            if "postcodes.io" in request.full_url:
                return _postcodes_io_reply(request)
            import urllib.error

            raise urllib.error.URLError("ors down")

        from datetime import datetime

        from booking_recommender import TravelMode, Worker

        worker = Worker(
            id="w1",
            name="Test Worker",
            home_location=Location(label="home", postcode="SW1A 1AA"),
            travel_modes=frozenset({TravelMode.DRIVE}),
            has_car=True,
        )
        with mock.patch.dict(os.environ, {"ORS_API_KEY": "synthetic"}, clear=True), mock.patch(
            "urllib.request.urlopen", side_effect=fake_urlopen
        ):
            estimator = build_live_travel_estimator(tenant_id="t", config=TravelConfig(route_retries=0))
            estimate = estimator.estimate(
                worker=worker,
                origin=worker.home_location,
                destination=Location(label="session", postcode="M1 1AE"),
                departure_time=datetime(2026, 9, 20, 9, 0),
            )
        self.assertEqual(estimate.provider, "postcode_distance")
        self.assertTrue(any("openrouteservice failed" in w for w in estimate.warnings))
        self.assertEqual(estimator.stats.provider_failures, 1)


class AllocationCliLiveTravelTests(unittest.TestCase):
    def test_flag_off_makes_no_network_calls(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("no network without --live-travel")
        ), redirect_stdout(io.StringIO()):
            code = allocation_main([str(FIXTURES), "--output-dir", tmp])
        self.assertEqual(code, 0)

    def test_flag_on_geocodes_and_reports_stats(self):
        with tempfile.TemporaryDirectory() as tmp, mocked_postcodes_io() as calls, mock.patch.dict(
            os.environ, {}, clear=True
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                code = allocation_main(
                    [str(FIXTURES), "--output-dir", tmp, "--live-travel", "--travel-cache-db", f"{tmp}/travel.sqlite"]
                )
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("Live travel enabled", text)
            self.assertIn("Travel estimates:", text)
            self.assertTrue(all("postcodes.io" in c for c in calls))
            self.assertTrue(Path(tmp, "travel.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
