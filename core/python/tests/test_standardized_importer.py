from __future__ import annotations

import csv
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from booking_recommender import StandardizedImportError, TravelMode, load_standardized_csv


FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "standardized_csv_v1"


class StandardizedImporterTests(unittest.TestCase):
    def test_valid_fixture_loads_planning_state_with_audit_and_source_refs(self):
        result = load_standardized_csv(FIXTURE_DIR)
        state = result.state

        self.assertEqual(2, len(state.workers))
        self.assertEqual(2, len(state.service_users))
        self.assertEqual(2, len(state.sessions))
        self.assertEqual("import-synthetic-2026-07-01", state.import_audit.import_run_id)
        self.assertEqual({"workers": 2, "sessions": 2}, {
            "workers": state.import_audit.row_counts["workers"],
            "sessions": state.import_audit.row_counts["sessions"],
        })

        alpha = state.workers["worker-alpha"]
        self.assertEqual("female", alpha.gender)
        self.assertIn(TravelMode.DRIVE, alpha.travel_modes)
        self.assertEqual(1, len(alpha.availability))
        self.assertEqual(1, len(alpha.unavailability))
        self.assertEqual("synthetic-workers-row-2", alpha.source_reference.source_ref)

        bravo = state.workers["worker-bravo"]
        self.assertEqual(("session-200",), bravo.current_allocations)
        session = state.sessions["session-200"]
        self.assertEqual("su-river", session.service_user_id)
        self.assertEqual("male", state.service_users["su-river"].gender)
        self.assertEqual(("lead",), tuple(allocation.role for allocation in session.assigned))
        self.assertEqual(20, session.travel_buffer_before_minutes)

        self.assertEqual("appropriate", state.compatibility[("worker-alpha", "su-orchard")].status)
        self.assertEqual(4, state.pair_history[("worker-alpha", "su-orchard")].completed_sessions)
        self.assertEqual("request-100", state.family_requests[0].id)
        self.assertTrue(result.report.warnings)
        self.assertTrue(any(issue.code == "unknown_compatibility" for issue in result.report.warnings))

    def test_invalid_fixture_fails_with_structured_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            import_dir = Path(temp_dir) / "standardized_csv_v1"
            shutil.copytree(FIXTURE_DIR, import_dir)
            _update_csv(
                import_dir / "sessions.csv",
                lambda row: row.update(
                    {
                        "session_id": "session-100",
                        "service_user_id": "missing-su",
                        "end": "2026-07-10T13:00:00+01:00",
                    }
                )
                if row["session_id"] == "session-100"
                else None,
            )

            with self.assertRaises(StandardizedImportError) as context:
                load_standardized_csv(import_dir)

        errors = context.exception.report.errors
        self.assertTrue(any(issue.code == "unknown_service_user_id" for issue in errors))
        self.assertTrue(any(issue.code == "invalid_time_range" for issue in errors))
        self.assertTrue(all(issue.severity == "error" for issue in errors))

    def test_invalid_assigned_workers_and_compatibility_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            import_dir = Path(temp_dir) / "standardized_csv_v1"
            shutil.copytree(FIXTURE_DIR, import_dir)
            _update_csv(
                import_dir / "sessions.csv",
                lambda row: row.update({"assigned_worker_ids": "missing-worker", "assigned_roles": "lead"})
                if row["session_id"] == "session-100"
                else None,
            )
            _update_csv(
                import_dir / "compatibility.csv",
                lambda row: row.update({"status": "maybe"})
                if row["worker_id"] == "worker-alpha"
                else None,
            )
            _update_csv(
                import_dir / "workers.csv",
                lambda row: row.update({"gender": "surprise"})
                if row["worker_id"] == "worker-alpha"
                else None,
            )

            with self.assertRaises(StandardizedImportError) as context:
                load_standardized_csv(import_dir)

        codes = {issue.code for issue in context.exception.report.errors}
        self.assertIn("unknown_assigned_worker_id", codes)
        self.assertIn("invalid_compatibility_status", codes)
        self.assertIn("invalid_gender", codes)

    def test_cli_prints_deterministic_summary(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "booking_recommender.import_cli",
                str(FIXTURE_DIR),
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Standardized CSV import summary", completed.stdout)
        self.assertIn("- workers: 2", completed.stdout)
        self.assertIn("errors: 0", completed.stdout)


def _update_csv(path: Path, update_row) -> None:
    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
        fieldnames = list(rows[0].keys())
    for row in rows:
        update_row(row)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
