from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from booking_recommender.adapters.workbook import WorkbookAdapterError, convert_workbook_sources
from booking_recommender.importer import load_standardized_csv


FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "workbook_adapter_csv"


class WorkbookAdapterTests(unittest.TestCase):
    def test_adapter_writes_standardized_contract_that_importer_can_load(self):
        before_hashes = _file_hashes(FIXTURE_DIR)
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "standardized"

            report = convert_workbook_sources(
                FIXTURE_DIR,
                output_dir,
                import_run_id="import-workbook-synthetic",
            )
            result = load_standardized_csv(output_dir)

            self.assertFalse(report.errors)
            self.assertEqual(2, report.row_counts["workers"])
            self.assertEqual(2, report.row_counts["service_users"])
            self.assertEqual(2, report.row_counts["sessions"])
            self.assertEqual(2, report.row_counts["family_requests"])
            self.assertEqual("import-workbook-synthetic", result.state.import_audit.import_run_id)
            self.assertIn("worker-lane", result.state.workers)
            self.assertIn("su-cedar", result.state.service_users)
            self.assertEqual("su-cedar", result.state.sessions["booking-100"].service_user_id)
            self.assertEqual(("booking-200",), result.state.workers["worker-lane"].current_allocations)
            self.assertTrue((output_dir / "adapter_report.json").is_file())

        self.assertEqual(before_hashes, _file_hashes(FIXTURE_DIR))

    def test_adapter_cli_can_validate_generated_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "standardized"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "booking_recommender.adapters.workbook",
                    str(FIXTURE_DIR),
                    str(output_dir),
                    "--import-run-id",
                    "import-workbook-cli",
                    "--validate",
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("workbook adapter summary", completed.stdout)
            self.assertIn("Standardized importer validation passed", completed.stdout)

            manifest = json.loads((output_dir / "import_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("import-workbook-cli", manifest["import_run_id"])

    def test_missing_required_sources_fail_before_writing_standardized_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            output_dir = Path(temp_dir) / "out"
            shutil.copytree(FIXTURE_DIR, source_dir)
            (source_dir / "sessions.csv").unlink()

            with self.assertRaises(WorkbookAdapterError) as context:
                convert_workbook_sources(source_dir, output_dir)

            codes = {issue.code for issue in context.exception.report.errors}
            self.assertIn("missing_source_table", codes)
            self.assertFalse(output_dir.exists())

    def test_adapter_coerces_excel_serial_date_and_time_parts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            output_dir = Path(temp_dir) / "out"
            shutil.copytree(FIXTURE_DIR, source_dir)
            (source_dir / "sessions.csv").write_text(
                "\n".join(
                    [
                        "booking_id,child_name,date,start_time,end_time,location,postcode,required_worker_count,staff,requires_driver,status",
                        "booking-serial,Cedar Child,46213,0.5,0.625,Cedar Home,AA1 3AA,1,,false,unallocated",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            convert_workbook_sources(source_dir, output_dir, import_run_id="import-workbook-serial")
            result = load_standardized_csv(output_dir)

            session = result.state.sessions["booking-serial"]
            self.assertEqual("2026-07-10T12:00:00+01:00", session.start.isoformat())
            self.assertEqual("2026-07-10T15:00:00+01:00", session.end.isoformat())

    def test_ambiguous_worker_names_warn_and_do_not_silently_match(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            output_dir = Path(temp_dir) / "out"
            shutil.copytree(FIXTURE_DIR, source_dir)
            (source_dir / "staff.csv").write_text(
                "\n".join(
                    [
                        "staff_id,name,postcode,travel_modes,has_car,skills,willing_areas,current_hours,max_hours",
                        "worker-lane-a,Lane Worker,AA1 1AA,drive,true,road_safety,AA1,0,20",
                        "worker-lane-b,Lane Worker,AA1 2AA,bus,false,visual_support,AA1,0,20",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            report = convert_workbook_sources(source_dir, output_dir, import_run_id="import-workbook-ambiguous")
            result = load_standardized_csv(output_dir)

            self.assertIn("ambiguous_name", {issue.code for issue in report.warnings})
            self.assertEqual(0, len(result.state.sessions["booking-200"].assigned))

    def test_adapter_understands_july_import_booking_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            output_dir = Path(temp_dir) / "out"
            shutil.copytree(FIXTURE_DIR, source_dir)
            (source_dir / "sessions.csv").write_text(
                "\n".join(
                    [
                        "booking_id,child_id,booking_date,booking_start_time,booking_finish_time,lead_sw_id,2nd_sw_id,2to1_required,car,recurring",
                        "booking-july,su-brook,2026-07-10,14:00,16:00,worker-lane,worker-moss,true,true,false",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            convert_workbook_sources(source_dir, output_dir, import_run_id="import-workbook-july")
            result = load_standardized_csv(output_dir)

            session = result.state.sessions["booking-july"]
            self.assertEqual("su-brook", session.service_user_id)
            self.assertEqual("2026-07-10T14:00:00+01:00", session.start.isoformat())
            self.assertEqual("2026-07-10T16:00:00+01:00", session.end.isoformat())
            self.assertEqual(2, session.required_worker_count)
            self.assertEqual(("worker-lane", "worker-moss"), tuple(item.worker_id for item in session.assigned))
            self.assertTrue(session.requires_driver)
            self.assertEqual("allocated", session.status)

    def test_import_sheet_bookings_default_to_weekly_recurrence_within_month(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            output_dir = Path(temp_dir) / "out"
            shutil.copytree(FIXTURE_DIR, source_dir)
            (source_dir / "sessions.csv").write_text(
                "\n".join(
                    [
                        "booking_id,child_id,booking_date,booking_start_time,booking_finish_time,lead_sw_id,2nd_sw_id,2to1_required,car,recurring",
                        "booking-weekly,su-brook,10/07/2026,14:00,16:00,worker-lane,,false,false,",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            convert_workbook_sources(source_dir, output_dir, import_run_id="import-workbook-weekly")
            result = load_standardized_csv(output_dir)

            weekly_sessions = [
                session
                for session in sorted(result.state.sessions.values(), key=lambda item: item.start)
                if session.id.startswith("booking-weekly")
            ]
            self.assertEqual(4, len(weekly_sessions))
            self.assertEqual(
                [
                    "2026-07-10T14:00:00+01:00",
                    "2026-07-17T14:00:00+01:00",
                    "2026-07-24T14:00:00+01:00",
                    "2026-07-31T14:00:00+01:00",
                ],
                [session.start.isoformat() for session in weekly_sessions],
            )

    def test_ad_hoc_recurrence_expands_only_when_explicit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            output_dir = Path(temp_dir) / "out"
            shutil.copytree(FIXTURE_DIR, source_dir)
            (source_dir / "date_requests.csv").unlink()
            (source_dir / "ad_hocs.csv").write_text(
                "\n".join(
                    [
                        "request_id,child_id,requested_staff,date,start_time,end_time,recurrence,until,notes",
                        "adhoc-fortnight,su-brook,worker-lane,10/07/2026,14:00,16:00,fortnightly,31/07/2026,synthetic fortnightly ad hoc",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            convert_workbook_sources(source_dir, output_dir, import_run_id="import-workbook-adhoc-recurring")
            result = load_standardized_csv(output_dir)

            requests = [
                request
                for request in result.state.family_requests
                if request.id == "adhoc-fortnight"
            ]
            self.assertEqual(1, len(requests))
            self.assertEqual(
                [
                    "2026-07-10T14:00:00+01:00",
                    "2026-07-24T14:00:00+01:00",
                ],
                [window.start.isoformat() for window in requests[0].preferred_time_ranges],
            )
            self.assertEqual(frozenset({4}), requests[0].preferred_days)

    def test_adapter_reads_eml_date_requests_and_flags_msg_for_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            output_dir = Path(temp_dir) / "out"
            shutil.copytree(FIXTURE_DIR, source_dir)
            (source_dir / "date_requests.csv").unlink()
            (source_dir / "ad_hocs.csv").unlink()
            request_dir = source_dir / "Date requests"
            request_dir.mkdir()
            (request_dir / "Cedar Child date request.eml").write_text(
                "\n".join(
                    [
                        "Subject: Date request for Cedar Child",
                        "Content-Type: text/plain; charset=utf-8",
                        "",
                        "Child: Cedar Child",
                        "Requested staff: Lane Worker",
                        "Preferred days: Friday",
                        "Window: 2026-07-10T10:00:00+01:00/2026-07-10T12:00:00+01:00",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (request_dir / "Brook Child dates.msg").write_bytes(b"synthetic msg placeholder")

            report = convert_workbook_sources(source_dir, output_dir, import_run_id="import-workbook-requests")
            result = load_standardized_csv(output_dir)

            self.assertIn("manual_request_file_review", {issue.code for issue in report.warnings})
            cedar_requests = [
                request
                for request in result.state.family_requests
                if request.service_user_id == "su-cedar" and "worker-lane" in request.requested_worker_ids
            ]
            self.assertEqual(1, len(cedar_requests))
            self.assertEqual(frozenset({4}), cedar_requests[0].preferred_days)


def _file_hashes(folder: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(folder.iterdir()):
        if path.is_file():
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


if __name__ == "__main__":
    unittest.main()
