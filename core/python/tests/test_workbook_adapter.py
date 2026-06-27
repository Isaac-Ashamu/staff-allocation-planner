from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

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

    def test_import_sheet_sw_note_text_resolves_against_staff_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            output_dir = Path(temp_dir) / "out"
            shutil.copytree(FIXTURE_DIR, source_dir)
            (source_dir / "sessions.csv").write_text(
                "\n".join(
                    [
                        "booking_id,child_id,booking_date,booking_start_time,booking_finish_time,lead_sw_id,2nd_sw_id,2to1_required,car,recurring",
                        "booking-notes,su-brook,2026-07-10,14:00,16:00,lead note: Lane Worker confirmed,cover note - Moss Worker can do it,true,true,false",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            report = convert_workbook_sources(source_dir, output_dir, import_run_id="import-workbook-sw-notes")
            result = load_standardized_csv(output_dir)

            session = result.state.sessions["booking-notes"]
            self.assertEqual(("worker-lane", "worker-moss"), tuple(item.worker_id for item in session.assigned))
            session_warnings = {
                issue.code
                for issue in report.warnings
                if issue.source == "sessions" and issue.row_number == 2
            }
            self.assertNotIn("unresolved_name", session_warnings)

    def test_import_sheet_sw_cell_comments_resolve_against_staff_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            output_dir = Path(temp_dir) / "out"
            shutil.copytree(FIXTURE_DIR, source_dir)
            (source_dir / "sessions.csv").unlink()
            _write_xlsx(
                source_dir / "booking-import.xlsx",
                {
                    "Bookings": [
                        [
                            "booking_id",
                            "child_id",
                            "booking_date",
                            "booking_start_time",
                            "booking_finish_time",
                            "lead_sw_id",
                            "2nd_sw_id",
                            "2to1_required",
                            "car",
                            "recurring",
                        ],
                        ["booking-comment", "su-brook", "2026-07-10", "14:00", "16:00", "790", "837", "true", "true", "false"],
                    ],
                },
                comments={
                    ("Bookings", "F2"): ("Planner", "Planner: Lane Worker"),
                    ("Bookings", "G2"): ("Planner", "Planner: Moss W"),
                },
            )

            report = convert_workbook_sources(source_dir, output_dir, import_run_id="import-workbook-sw-comments")
            result = load_standardized_csv(output_dir)

            session = result.state.sessions["booking-comment"]
            self.assertEqual(("worker-lane", "worker-moss"), tuple(item.worker_id for item in session.assigned))
            session_warnings = {
                issue.code
                for issue in report.warnings
                if issue.source == "sessions" and issue.row_number == 2
            }
            self.assertNotIn("unresolved_name", session_warnings)
            self.assertNotIn("ambiguous_reference_text", session_warnings)

    def test_import_sheet_sw_cell_comment_can_create_traceable_placeholder_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            output_dir = Path(temp_dir) / "out"
            shutil.copytree(FIXTURE_DIR, source_dir)
            (source_dir / "sessions.csv").unlink()
            _write_xlsx(
                source_dir / "booking-import.xlsx",
                {
                    "Bookings": [
                        [
                            "booking_id",
                            "child_id",
                            "booking_date",
                            "booking_start_time",
                            "booking_finish_time",
                            "lead_sw_id",
                            "2to1_required",
                            "car",
                            "recurring",
                        ],
                        ["booking-placeholder", "su-brook", "2026-07-10", "14:00", "16:00", "900", "false", "false", "false"],
                    ],
                },
                comments={("Bookings", "F2"): ("Planner", "Planner: Guest Worker")},
            )

            report = convert_workbook_sources(source_dir, output_dir, import_run_id="import-workbook-sw-placeholder")
            result = load_standardized_csv(output_dir)

            self.assertIn("900", result.state.workers)
            self.assertEqual("Guest Worker", result.state.workers["900"].name)
            session = result.state.sessions["booking-placeholder"]
            self.assertEqual(("900",), tuple(item.worker_id for item in session.assigned))
            self.assertIn("worker_created_from_import_sheet_note", {issue.code for issue in report.warnings})

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

    def test_adapter_merges_provider_workbooks_and_headerless_su_sheet(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            output_dir = Path(temp_dir) / "out"
            source_dir.mkdir()
            _write_xlsx(
                source_dir / "Staff and SU names and postcode.xlsx",
                {
                    "STAFF": [
                        [
                            "Support Worker",
                            "DOB",
                            "How they get to sessions",
                            "Languages Spoken",
                            "Start Date",
                            "Address",
                            "Gender",
                        ],
                        ["Nova Helper", "01/01/2000", "Car", "English", "01/01/2026", "ZZ1 1ZZ", "X"],
                    ],
                    "SU": [
                        ["River", "Young", "ZZ2 2ZZ", "2:1", "X"],
                        ["Lake", "Young", "ZZ3 3ZZ", "1:1", "X"],
                    ],
                },
            )
            _write_xlsx(
                source_dir / "booking-import.xlsx",
                {
                    "Bookings": [
                        [
                            "booking_id",
                            "child_name",
                            "booking_date",
                            "booking_start_time",
                            "booking_finish_time",
                            "lead_sw_id",
                            "2to1_required",
                            "car",
                            "recurring",
                        ],
                        ["booking-river", "River Young", "10/07/2026", "10:00", "12:00", "Nova Helper", "true", "true", "false"],
                    ],
                },
            )
            _write_xlsx(
                source_dir / "July 2026 Ad hocs.xlsx",
                {
                    "Phase 1": [
                        ["client_name", "specific_day", "specific_time", "sw_requested", "sessions_notes"],
                        ["River Young", "10/07/2026", "10:00-12:00", "Nova Helper", "synthetic workbook ad hoc"],
                    ],
                },
            )

            report = convert_workbook_sources(source_dir, output_dir, import_run_id="import-workbook-workbooks")
            result = load_standardized_csv(output_dir)

            self.assertFalse(report.errors)
            self.assertEqual(1, report.row_counts["workers"])
            self.assertEqual(2, report.row_counts["service_users"])
            self.assertEqual(1, report.row_counts["sessions"])
            worker = next(iter(result.state.workers.values()))
            service_user = next(item for item in result.state.service_users.values() if item.name == "River Young")
            session = result.state.sessions["booking-river"]
            self.assertEqual("Nova Helper", worker.name)
            self.assertEqual("ZZ1 1ZZ", worker.home_location.postcode)
            self.assertEqual("River Young", service_user.name)
            self.assertEqual(2, service_user.required_worker_count)
            self.assertEqual(service_user.id, session.service_user_id)
            self.assertEqual((worker.id,), tuple(allocation.worker_id for allocation in session.assigned))
            self.assertEqual(1, len(result.state.family_requests))
            self.assertEqual("2026-07-10T10:00:00+01:00", result.state.family_requests[0].preferred_time_ranges[0].start.isoformat())

    def test_adapter_flags_availability_report_pdfs_for_manual_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            output_dir = Path(temp_dir) / "out"
            shutil.copytree(FIXTURE_DIR, source_dir)
            report_dir = source_dir / "availability report"
            report_dir.mkdir()
            (report_dir / "July 2026.pdf").write_bytes(b"%PDF-1.4\n% synthetic placeholder\n")

            report = convert_workbook_sources(source_dir, output_dir, import_run_id="import-workbook-pdf-warning")

            self.assertIn("availability_report_pdf_requires_review", {issue.code for issue in report.warnings})


def _file_hashes(folder: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(folder.iterdir()):
        if path.is_file():
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _write_xlsx(
    path: Path,
    sheets: dict[str, list[list[str]]],
    comments: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> None:
    comments = comments or {}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        sheet_entries = []
        rel_entries = []
        for index, sheet_name in enumerate(sheets, start=1):
            sheet_entries.append(
                f'<sheet name="{escape(sheet_name)}" sheetId="{index}" r:id="rId{index}"/>'
            )
            rel_entries.append(
                f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
            )
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>"""
            + "".join(sheet_entries)
            + """</sheets>
</workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">"""
            + "".join(rel_entries)
            + """</Relationships>""",
        )
        for index, (sheet_name, rows) in enumerate(sheets.items(), start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _worksheet_xml(rows))
            sheet_comments = {
                ref: value
                for (comment_sheet, ref), value in comments.items()
                if comment_sheet == sheet_name
            }
            if sheet_comments:
                comments_name = f"comments{index}.xml"
                archive.writestr(
                    f"xl/worksheets/_rels/sheet{index}.xml.rels",
                    f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="../{comments_name}"/>
</Relationships>""",
                )
                archive.writestr(f"xl/{comments_name}", _comments_xml(sheet_comments))


def _worksheet_xml(rows: list[list[str]]) -> str:
    row_xml = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row):
            ref = f"{_column_name(column_index)}{row_index}"
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        row_xml.append(f'<row r="{row_index}">' + "".join(cells) + "</row>")
    return (
        """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>"""
        + "".join(row_xml)
        + """</sheetData></worksheet>"""
    )


def _comments_xml(comments: dict[str, tuple[str, str]]) -> str:
    authors = []
    for author, _ in comments.values():
        if author not in authors:
            authors.append(author)
    author_xml = "".join(f"<author>{escape(author)}</author>" for author in authors)
    comment_xml = []
    for ref, (author, text) in comments.items():
        author_id = authors.index(author)
        comment_xml.append(
            f'<comment ref="{escape(ref)}" authorId="{author_id}"><text><r><t>{escape(text)}</t></r></text></comment>'
        )
    return (
        """<?xml version="1.0" encoding="UTF-8"?>
<comments xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><authors>"""
        + author_xml
        + "</authors><commentList>"
        + "".join(comment_xml)
        + "</commentList></comments>"
    )


def _column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


if __name__ == "__main__":
    unittest.main()
