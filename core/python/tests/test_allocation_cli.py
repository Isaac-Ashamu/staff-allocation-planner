from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from booking_recommender import (
    FallbackTravelEstimator,
    allocation_report_to_dict,
    build_allocation_report,
    load_standardized_csv,
)


FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "standardized_csv_v1"


class AllocationCliTests(unittest.TestCase):
    def test_build_allocation_report_allocates_and_explains_fixture_plan(self):
        import_result = load_standardized_csv(FIXTURE_DIR)
        report = build_allocation_report(
            source_folder=FIXTURE_DIR,
            state=import_result.state,
            import_report=import_result.report,
            travel_estimator=FallbackTravelEstimator(),
            top_n=5,
            include_infeasible=True,
            compare_single_session=True,
        )
        data = allocation_report_to_dict(report)

        self.assertEqual(1, data["summary"]["accepted_assignments"])
        self.assertEqual(1, data["summary"]["unfilled_sessions"])
        self.assertEqual("worker-alpha", data["recommendation_details"][0]["recommendation"]["worker_id"])
        self.assertTrue(data["recommendation_details"][0]["recommendation"]["reasons"])
        self.assertIn("session-200", {item["session_id"] for item in data["proposed_plan"]["unfilled_sessions"]})
        self.assertIn("session-100", data["single_session_rankings"])
        blocked_failures = [
            candidate["hard_constraint_failures"]
            for item in data["recommendation_details"]
            for candidate in item["candidates"]
            if candidate["hard_constraint_failures"]
        ]
        self.assertTrue(blocked_failures)

    def test_cli_writes_derived_outputs_without_mutating_source_files(self):
        before_hashes = _file_hashes(FIXTURE_DIR)
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "allocation-output"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "booking_recommender.allocation_cli",
                    str(FIXTURE_DIR),
                    "--output-dir",
                    str(output_dir),
                    "--include-infeasible",
                    "--compare-single-session",
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("Allocation plan report", completed.stdout)
            self.assertIn("Allocated sessions", completed.stdout)
            self.assertIn("Unfilled sessions", completed.stdout)
            self.assertIn("Blocked candidates", completed.stdout)

            expected = {
                "allocation_report.json",
                "planning_snapshot.json",
                "proposed_plan.json",
                "recommendation_details.json",
                "allocation_report.txt",
            }
            self.assertEqual(expected, {path.name for path in output_dir.iterdir()})

            proposed_plan = json.loads((output_dir / "proposed_plan.json").read_text(encoding="utf-8"))
            recommendation_details = json.loads(
                (output_dir / "recommendation_details.json").read_text(encoding="utf-8")
            )
            self.assertEqual("session-100", proposed_plan["allocated_sessions"][0]["session_id"])
            self.assertTrue(recommendation_details[0]["recommendation"]["reasons"])

        self.assertEqual(before_hashes, _file_hashes(FIXTURE_DIR))


def _file_hashes(folder: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(folder.iterdir()):
        if path.is_file():
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


if __name__ == "__main__":
    unittest.main()
