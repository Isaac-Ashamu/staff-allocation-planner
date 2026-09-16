"""GtfsTransitProvider against a tiny, controlled synthetic feed.

Three stops (S1 -> S2 -> S3, two trips, one weekday service) so every number
in these tests can be checked by hand rather than trusted blindly:

    T1: S1 09:10 -> S2 09:20        T2: S2 09:25 -> S3 09:35

A journey from S1 to S3 has no direct trip, so it must transfer at S2.
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from booking_recommender.gtfs_transit import GtfsTransitProvider
from booking_recommender.models import Location, TravelMode
from booking_recommender.travel import TravelProviderError

_FILES: dict[str, list[dict[str, str]]] = {
    "stops.txt": [
        {"stop_id": "S1", "stop_name": "Origin Stop", "stop_lat": "53.4808", "stop_lon": "-2.2426"},
        {"stop_id": "S2", "stop_name": "Transfer Stop", "stop_lat": "53.4700", "stop_lon": "-2.2300"},
        {"stop_id": "S3", "stop_name": "Destination Stop", "stop_lat": "53.4600", "stop_lon": "-2.2200"},
    ],
    "routes.txt": [
        {"route_id": "R1", "route_short_name": "1", "route_type": "3"},
        {"route_id": "R2", "route_short_name": "2", "route_type": "3"},
    ],
    "trips.txt": [
        {"route_id": "R1", "service_id": "WD", "trip_id": "T1"},
        {"route_id": "R2", "service_id": "WD", "trip_id": "T2"},
    ],
    "calendar.txt": [
        {
            "service_id": "WD", "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1",
            "friday": "1", "saturday": "0", "sunday": "0", "start_date": "20260101", "end_date": "20261231",
        }
    ],
    "calendar_dates.txt": [],
    "stop_times.txt": [
        {"trip_id": "T1", "stop_id": "S1", "arrival_time": "09:10:00", "departure_time": "09:10:00", "stop_sequence": "1"},
        {"trip_id": "T1", "stop_id": "S2", "arrival_time": "09:20:00", "departure_time": "09:20:00", "stop_sequence": "2"},
        {"trip_id": "T2", "stop_id": "S2", "arrival_time": "09:25:00", "departure_time": "09:25:00", "stop_sequence": "1"},
        {"trip_id": "T2", "stop_id": "S3", "arrival_time": "09:35:00", "departure_time": "09:35:00", "stop_sequence": "2"},
    ],
}

_ALL_FIELDS = {
    "stops.txt": ["stop_id", "stop_name", "stop_lat", "stop_lon"],
    "routes.txt": ["route_id", "route_short_name", "route_type"],
    "trips.txt": ["route_id", "service_id", "trip_id"],
    "calendar.txt": ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "start_date", "end_date"],
    "calendar_dates.txt": ["service_id", "date", "exception_type"],
    "stop_times.txt": ["trip_id", "stop_id", "arrival_time", "departure_time", "stop_sequence"],
}


def _build_mini_feed(root: Path) -> Path:
    for name, fields in _ALL_FIELDS.items():
        with (root / name).open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(_FILES[name])
    return root


class GtfsTransitProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.feed_dir = _build_mini_feed(Path(self._tmp.name))
        self.provider = GtfsTransitProvider(gtfs_path=self.feed_dir)
        self.origin = Location(label="near S1", latitude=53.4808, longitude=-2.2426)
        self.destination = Location(label="near S3", latitude=53.4600, longitude=-2.2200)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_one_transfer_journey_totals_every_component(self) -> None:
        # 0 walk + 10 wait-for-first-bus + 10 (S1->S2) + 5 transfer wait + 10 (S2->S3) + 0 walk = 35
        estimate = self.provider.estimate(
            origin=self.origin, destination=self.destination, mode=TravelMode.BUS,
            departure_time=datetime(2026, 9, 21, 9, 0),
        )
        self.assertEqual(estimate.provider, "gtfs_transit")
        self.assertEqual(estimate.confidence, "medium")
        self.assertEqual(estimate.duration_minutes, 35.0)
        self.assertIn("transfer at Transfer Stop", " ".join(estimate.warnings))

    def test_departing_exactly_at_boarding_time_has_no_wait(self) -> None:
        estimate = self.provider.estimate(
            origin=self.origin, destination=self.destination, mode=TravelMode.TRAM,
            departure_time=datetime(2026, 9, 21, 9, 10),
        )
        self.assertEqual(estimate.duration_minutes, 25.0)

    def test_refuses_non_transit_modes(self) -> None:
        with self.assertRaisesRegex(TravelProviderError, "does not serve mode"):
            self.provider.estimate(
                origin=self.origin, destination=self.destination, mode=TravelMode.DRIVE,
                departure_time=datetime(2026, 9, 21, 9, 0),
            )

    def test_refuses_when_no_feed_configured(self) -> None:
        empty = GtfsTransitProvider(gtfs_path=None)
        with self.assertRaisesRegex(TravelProviderError, "not configured"):
            empty.estimate(
                origin=self.origin, destination=self.destination, mode=TravelMode.BUS,
                departure_time=datetime(2026, 9, 21, 9, 0),
            )

    def test_refuses_without_coordinates(self) -> None:
        no_coords = Location(label="no coords", postcode="M1 1AA")
        with self.assertRaisesRegex(TravelProviderError, "requires coordinates"):
            self.provider.estimate(
                origin=no_coords, destination=self.destination, mode=TravelMode.BUS,
                departure_time=datetime(2026, 9, 21, 9, 0),
            )

    def test_refuses_when_nothing_within_walking_distance(self) -> None:
        far = Location(label="middle of nowhere", latitude=54.5, longitude=-3.5)
        with self.assertRaisesRegex(TravelProviderError, "no direct or one-transfer"):
            self.provider.estimate(
                origin=far, destination=self.destination, mode=TravelMode.BUS,
                departure_time=datetime(2026, 9, 21, 9, 0),
            )

    def test_refuses_outside_service_calendar(self) -> None:
        saturday = datetime(2026, 9, 19, 9, 0)  # service WD does not run weekends
        with self.assertRaisesRegex(TravelProviderError, "no direct or one-transfer"):
            self.provider.estimate(
                origin=self.origin, destination=self.destination, mode=TravelMode.BUS, departure_time=saturday,
            )

    def test_calendar_dates_exception_can_add_a_normally_inactive_service(self) -> None:
        # Add a Saturday-only exception on top of the weekday calendar, then confirm it activates.
        with (self.feed_dir / "calendar_dates.txt").open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["service_id", "date", "exception_type"])
            writer.writeheader()
            writer.writerow({"service_id": "WD", "date": "20260919", "exception_type": "1"})
        provider = GtfsTransitProvider(gtfs_path=self.feed_dir)  # fresh instance: caches not yet loaded
        estimate = provider.estimate(
            origin=self.origin, destination=self.destination, mode=TravelMode.BUS,
            departure_time=datetime(2026, 9, 19, 9, 0),
        )
        self.assertEqual(estimate.duration_minutes, 35.0)


if __name__ == "__main__":
    unittest.main()
