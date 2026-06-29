from __future__ import annotations

import csv
import json
import unittest
from datetime import datetime
from pathlib import Path


FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "standardized_csv_v1"

EXPECTED_HEADERS = {
    "workers.csv": [
        "worker_id",
        "name",
        "gender",
        "home_label",
        "home_postcode",
        "home_latitude",
        "home_longitude",
        "home_area",
        "travel_modes",
        "has_car",
        "skills",
        "languages",
        "willing_areas",
        "current_hours",
        "max_hours",
        "preference_tags",
        "reliability_score",
        "source_ref",
    ],
    "service_users.csv": [
        "service_user_id",
        "name",
        "gender",
        "home_label",
        "home_postcode",
        "home_latitude",
        "home_longitude",
        "home_area",
        "support_level",
        "required_worker_count",
        "requires_driver",
        "required_skills",
        "communication_needs",
        "interests",
        "risk_tags",
        "preferred_worker_ids",
        "avoided_worker_ids",
        "profile_is_stale",
        "source_ref",
    ],
    "sessions.csv": [
        "session_id",
        "service_user_id",
        "start",
        "end",
        "location_label",
        "location_postcode",
        "location_latitude",
        "location_longitude",
        "location_area",
        "required_worker_count",
        "assigned_worker_ids",
        "assigned_roles",
        "requires_driver",
        "priority",
        "status",
        "travel_buffer_before_minutes",
        "travel_buffer_after_minutes",
        "source_ref",
    ],
    "availability.csv": ["availability_id", "worker_id", "start", "end", "source_ref"],
    "unavailability.csv": [
        "unavailability_id",
        "worker_id",
        "start",
        "end",
        "reason",
        "source_ref",
    ],
    "compatibility.csv": ["worker_id", "service_user_id", "status", "reason", "source_ref"],
    "family_requests.csv": [
        "family_request_id",
        "service_user_id",
        "requested_worker_ids",
        "avoided_worker_ids",
        "preferred_days",
        "preferred_time_ranges",
        "unavailable_time_ranges",
        "notes",
        "source_ref",
    ],
    "pair_history.csv": [
        "worker_id",
        "service_user_id",
        "completed_sessions",
        "positive_feedback_count",
        "incident_count",
        "cancellation_count",
        "last_session_at",
        "source_ref",
    ],
}

TRAVEL_MODES = {
    "drive",
    "bus",
    "tram",
    "train",
    "public_transport",
    "cycle",
    "walk",
    "taxi",
}
COMPATIBILITY_STATUSES = {"appropriate", "inappropriate", "unknown"}
SESSION_STATUSES = {
    "unallocated",
    "partially_allocated",
    "allocated",
    "confirmed",
    "cancelled",
    "rejected",
}
GENDER_VALUES = {"", "female", "male", "non_binary", "other", "prefer_not_to_say", "unknown"}


class StandardizedCsvV1FixtureTests(unittest.TestCase):
    def test_fixture_files_exist_and_have_exact_headers(self):
        self.assertTrue((FIXTURE_DIR / "import_manifest.json").is_file())
        for filename, expected_header in EXPECTED_HEADERS.items():
            rows = _read_csv(filename)
            self.assertEqual(expected_header, list(rows[0].keys()), filename)
            self.assertGreaterEqual(len(rows), 1, filename)

    def test_manifest_has_import_audit_fields_and_source_refs(self):
        manifest = json.loads((FIXTURE_DIR / "import_manifest.json").read_text(encoding="utf-8"))

        self.assertEqual("import-synthetic-2026-07-01", manifest["import_run_id"])
        self.assertEqual("standardized-csv", manifest["source_system"])
        _assert_offset_datetime(self, manifest["imported_at"])
        for filename in EXPECTED_HEADERS:
            self.assertIn(f"fixtures/standardized_csv_v1/{filename}", manifest["source_refs"])

    def test_fixture_rows_use_required_values_enums_and_offset_datetimes(self):
        workers = _read_csv("workers.csv")
        service_users = _read_csv("service_users.csv")
        sessions = _read_csv("sessions.csv")

        for row in workers:
            self.assertTrue(row["worker_id"])
            self.assertTrue(row["name"])
            self.assertIn(row["gender"], GENDER_VALUES)
            self.assertIn(row["has_car"], {"true", "false"})
            self.assertTrue(set(_split_list(row["travel_modes"])).issubset(TRAVEL_MODES))
            self.assertTrue(row["travel_modes"])

        for row in service_users:
            self.assertTrue(row["service_user_id"])
            self.assertTrue(row["name"])
            self.assertIn(row["gender"], GENDER_VALUES)
            self.assertGreaterEqual(int(row["required_worker_count"]), 1)
            self.assertIn(row["requires_driver"], {"true", "false"})
            self.assertIn(row["profile_is_stale"], {"true", "false"})

        for row in sessions:
            self.assertTrue(row["session_id"])
            start = _assert_offset_datetime(self, row["start"])
            end = _assert_offset_datetime(self, row["end"])
            self.assertLess(start, end)
            self.assertGreaterEqual(int(row["required_worker_count"]), 1)
            self.assertIn(row["requires_driver"], {"true", "false"})
            self.assertIn(row["status"], SESSION_STATUSES)

    def test_fixture_references_are_consistent(self):
        workers = {row["worker_id"] for row in _read_csv("workers.csv")}
        service_users = {row["service_user_id"] for row in _read_csv("service_users.csv")}

        for row in _read_csv("service_users.csv"):
            self.assertTrue(set(_split_list(row["preferred_worker_ids"])).issubset(workers))
            self.assertTrue(set(_split_list(row["avoided_worker_ids"])).issubset(workers))

        for row in _read_csv("sessions.csv"):
            self.assertIn(row["service_user_id"], service_users)
            assigned_worker_ids = _split_list(row["assigned_worker_ids"])
            assigned_roles = _split_list(row["assigned_roles"])
            self.assertTrue(set(assigned_worker_ids).issubset(workers))
            self.assertEqual(len(assigned_worker_ids), len(assigned_roles))

        for table in ("availability.csv", "unavailability.csv"):
            for row in _read_csv(table):
                self.assertIn(row["worker_id"], workers)
                start = _assert_offset_datetime(self, row["start"])
                end = _assert_offset_datetime(self, row["end"])
                self.assertLess(start, end)

        for row in _read_csv("compatibility.csv"):
            self.assertIn(row["worker_id"], workers)
            self.assertIn(row["service_user_id"], service_users)
            self.assertIn(row["status"], COMPATIBILITY_STATUSES)

        for row in _read_csv("family_requests.csv"):
            self.assertIn(row["service_user_id"], service_users)
            self.assertTrue(set(_split_list(row["requested_worker_ids"])).issubset(workers))
            self.assertTrue(set(_split_list(row["avoided_worker_ids"])).issubset(workers))
            for day in _split_list(row["preferred_days"]):
                self.assertIn(int(day), range(7))
            for interval in _split_list(row["preferred_time_ranges"]):
                _assert_interval(self, interval)
            for interval in _split_list(row["unavailable_time_ranges"]):
                _assert_interval(self, interval)

        for row in _read_csv("pair_history.csv"):
            self.assertIn(row["worker_id"], workers)
            self.assertIn(row["service_user_id"], service_users)
            for column in (
                "completed_sessions",
                "positive_feedback_count",
                "incident_count",
                "cancellation_count",
            ):
                self.assertGreaterEqual(int(row[column]), 0)
            if row["last_session_at"]:
                _assert_offset_datetime(self, row["last_session_at"])


def _read_csv(filename: str) -> list[dict[str, str]]:
    with (FIXTURE_DIR / filename).open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def _split_list(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split("|") if item.strip()]


def _assert_offset_datetime(testcase: unittest.TestCase, value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    testcase.assertIsNotNone(parsed.tzinfo, value)
    testcase.assertIsNotNone(parsed.utcoffset(), value)
    return parsed


def _assert_interval(testcase: unittest.TestCase, value: str) -> None:
    start_value, end_value = value.split("/", maxsplit=1)
    start = _assert_offset_datetime(testcase, start_value)
    end = _assert_offset_datetime(testcase, end_value)
    testcase.assertLess(start, end)


if __name__ == "__main__":
    unittest.main()
