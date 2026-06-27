"""Booking-workbook source adapter for the standardized CSV v1 import contract.

This adapter accepts read-only CSV exports, plus an optional simple XLSX booking
workbook with the booking sheet names observed in local source data. It writes a
new standardized import folder; it never edits source files in place.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping
from xml.etree import ElementTree

from ..importer import load_standardized_csv


STANDARD_TABLE_HEADERS = {
    "workers.csv": [
        "worker_id",
        "name",
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
    "unavailability.csv": ["unavailability_id", "worker_id", "start", "end", "reason", "source_ref"],
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

BOOKING_SHEETS = {
    "Bookings": "sessions",
    "Childs": "service_users",
    "Support Workers": "workers",
    "Availability": "availability",
    "Matching": "compatibility",
}

SOURCE_FILES = {
    "workers": "staff.csv",
    "service_users": "service_users.csv",
    "sessions": "sessions.csv",
    "availability": "availability.csv",
    "compatibility": "matching.csv",
    "family_requests": "date_requests.csv",
    "ad_hocs": "ad_hocs.csv",
}


@dataclass(frozen=True)
class AdapterIssue:
    severity: str
    code: str
    message: str
    source: str | None = None
    row_number: int | None = None
    field: str | None = None


@dataclass(frozen=True)
class WorkbookAdapterReport:
    source_dir: str
    output_dir: str
    row_counts: Mapping[str, int]
    issues: tuple[AdapterIssue, ...] = ()

    @property
    def errors(self) -> tuple[AdapterIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[AdapterIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")


class WorkbookAdapterError(ValueError):
    def __init__(self, report: WorkbookAdapterReport):
        self.report = report
        messages = "; ".join(issue.message for issue in report.errors)
        super().__init__(messages or "workbook adapter failed")


def convert_workbook_sources(
    source_dir: str | Path,
    output_dir: str | Path,
    *,
    booking_workbook: str | Path | None = None,
    import_run_id: str | None = None,
    imported_at: datetime | None = None,
    source_system: str = "workbook-adapter",
) -> WorkbookAdapterReport:
    source_path = Path(source_dir)
    output_path = Path(output_dir)
    issues: list[AdapterIssue] = []

    workbook_path = Path(booking_workbook) if booking_workbook else _first_xlsx(source_path)
    source_tables = _read_source_tables(source_path, workbook_path, issues)
    worker_rows = _workers(source_tables.get("workers", ()), issues)
    _warn_duplicate_names(worker_rows, "workers", issues)
    worker_lookup = _lookup(worker_rows, "worker_id", "name")
    service_user_rows = _service_users(source_tables.get("service_users", ()), issues)
    _warn_duplicate_names(service_user_rows, "service_users", issues)
    service_user_lookup = _lookup(service_user_rows, "service_user_id", "name")

    session_rows = _sessions(
        source_tables.get("sessions", ()),
        worker_lookup,
        service_user_lookup,
        issues,
    )
    availability_rows = _availability(source_tables.get("availability", ()), worker_lookup, issues)
    compatibility_rows = _compatibility(
        source_tables.get("compatibility", ()),
        worker_lookup,
        service_user_lookup,
        issues,
    )
    family_request_rows = _family_requests(
        tuple(source_tables.get("family_requests", ())) + tuple(source_tables.get("ad_hocs", ())),
        worker_lookup,
        service_user_lookup,
        issues,
    )

    outputs = {
        "workers.csv": worker_rows,
        "service_users.csv": service_user_rows,
        "sessions.csv": session_rows,
        "availability.csv": availability_rows,
        "unavailability.csv": [],
        "compatibility.csv": compatibility_rows,
        "family_requests.csv": family_request_rows,
        "pair_history.csv": [],
    }
    row_counts = {name.removesuffix(".csv"): len(rows) for name, rows in outputs.items()}

    report = WorkbookAdapterReport(
        source_dir=str(source_path),
        output_dir=str(output_path),
        row_counts=row_counts,
        issues=tuple(sorted(issues, key=_issue_sort_key)),
    )
    if report.errors:
        raise WorkbookAdapterError(report)

    output_path.mkdir(parents=True, exist_ok=True)
    for filename, rows in outputs.items():
        _write_csv(output_path / filename, STANDARD_TABLE_HEADERS[filename], rows)

    timestamp = imported_at or datetime.now(timezone.utc)
    manifest = {
        "import_run_id": import_run_id or f"import-workbook-{timestamp.strftime('%Y%m%dT%H%M%SZ')}",
        "imported_at": timestamp.isoformat(),
        "source_system": source_system,
        "source_refs": sorted(_source_refs(source_path, workbook_path)),
        "notes": "Generated by the read-only workbook adapter into standardized CSV v1.",
    }
    (output_path / "import_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_adapter_report(output_path / "adapter_report.json", report)
    return report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert source exports into standardized CSV v1.")
    parser.add_argument("source_dir", help="Read-only folder containing source CSV exports.")
    parser.add_argument("output_dir", help="Derived output folder for standardized CSV v1 files.")
    parser.add_argument("--booking-workbook", help="Optional booking workbook with Bookings/Childs/Support Workers/Availability/Matching sheets.")
    parser.add_argument("--import-run-id", help="Stable import run ID to write into import_manifest.json.")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Load the generated standardized output with the tenant-agnostic importer.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        report = convert_workbook_sources(
            args.source_dir,
            args.output_dir,
            booking_workbook=args.booking_workbook,
            import_run_id=args.import_run_id,
        )
    except WorkbookAdapterError as error:
        print("workbook adapter failed")
        _print_issues(error.report.issues)
        return 1

    print("workbook adapter summary")
    for table, count in sorted(report.row_counts.items()):
        print(f"- {table}: {count}")
    if report.issues:
        _print_issues(report.issues)
    print(f"Saved standardized CSV v1 output to {report.output_dir}")

    if args.validate:
        load_standardized_csv(args.output_dir)
        print("Standardized importer validation passed")
    return 0


def _read_source_tables(
    source_dir: Path,
    booking_workbook: Path | None,
    issues: list[AdapterIssue],
) -> dict[str, tuple[dict[str, str], ...]]:
    tables: dict[str, tuple[dict[str, str], ...]] = {}
    for key, filename in SOURCE_FILES.items():
        path = source_dir / filename
        if path.is_file():
            tables[key] = _read_csv(path)
        else:
            tables[key] = ()

    if booking_workbook and booking_workbook.is_file():
        workbook_tables = _read_workbook_tables(booking_workbook, issues)
        for key, rows in workbook_tables.items():
            if rows:
                tables[key] = rows

    for required_key in ("workers", "service_users", "sessions"):
        if not tables.get(required_key):
            _add_issue(
                issues,
                "error",
                "missing_source_table",
                f"missing required source table: {required_key}",
                SOURCE_FILES[required_key],
            )
    if not tables.get("availability"):
        _add_issue(
            issues,
            "warning",
            "missing_availability_source",
            "missing availability source; workers may be treated as generally available",
            SOURCE_FILES["availability"],
        )
    return tables


def _workers(rows: Iterable[Mapping[str, str]], issues: list[AdapterIssue]) -> list[dict[str, str]]:
    output = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        name = _value(row, "name", "staff_name", "support_worker", "support worker", "worker")
        if not name:
            _add_issue(issues, "warning", "blank_worker_name", "blank worker row skipped", "workers", row_number, "name")
            continue
        worker_id = _value(row, "worker_id", "staff_id") or _stable_id("worker", name)
        if worker_id in seen:
            _add_issue(issues, "error", "duplicate_worker_id", f"duplicate worker id {worker_id}", "workers", row_number, "worker_id")
            continue
        seen.add(worker_id)
        modes = _modes(_value(row, "travel_modes", "transport", "travel", "drives"))
        has_car = "true" if "drive" in modes or _truthy(_value(row, "has_car", "car", "drives")) else "false"
        output.append(
            {
                "worker_id": worker_id,
                "name": name,
                "home_label": "",
                "home_postcode": _value(row, "postcode", "home_postcode", "staff_postcode"),
                "home_latitude": "",
                "home_longitude": "",
                "home_area": _area(_value(row, "area", "home_area", "postcode", "home_postcode", "staff_postcode")),
                "travel_modes": "|".join(sorted(modes or {"public_transport", "walk"})),
                "has_car": has_car,
                "skills": _pipe(_value(row, "skills", "training")),
                "languages": _pipe(_value(row, "languages")),
                "willing_areas": _pipe(_value(row, "willing_areas", "areas")),
                "current_hours": _value(row, "current_hours", "hours") or "0",
                "max_hours": _value(row, "max_hours"),
                "preference_tags": _pipe(_value(row, "preference_tags", "preferences")),
                "reliability_score": "",
                "source_ref": _source_ref("workers", row_number),
            }
        )
    return output


def _service_users(rows: Iterable[Mapping[str, str]], issues: list[AdapterIssue]) -> list[dict[str, str]]:
    output = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        name = _value(row, "name", "child_name", "service_user", "child")
        if not name:
            _add_issue(issues, "warning", "blank_service_user_name", "blank service-user row skipped", "service_users", row_number, "name")
            continue
        service_user_id = _value(row, "service_user_id", "child_id") or _stable_id("su", name)
        if service_user_id in seen:
            _add_issue(issues, "error", "duplicate_service_user_id", f"duplicate service-user id {service_user_id}", "service_users", row_number, "service_user_id")
            continue
        seen.add(service_user_id)
        output.append(
            {
                "service_user_id": service_user_id,
                "name": name,
                "home_label": "",
                "home_postcode": _value(row, "postcode", "home_postcode", "child_postcode"),
                "home_latitude": "",
                "home_longitude": "",
                "home_area": _area(_value(row, "area", "home_area", "postcode", "home_postcode", "child_postcode")),
                "support_level": _value(row, "support_level", "support") or "standard",
                "required_worker_count": _value(row, "required_worker_count", "workers_required", "staff_required") or "1",
                "requires_driver": _bool_text(_value(row, "requires_driver", "driver_required", "needs_driver")),
                "required_skills": _pipe(_value(row, "required_skills", "risks", "risk_tags")),
                "communication_needs": _pipe(_value(row, "communication_needs")),
                "interests": _pipe(_value(row, "interests")),
                "risk_tags": _pipe(_value(row, "risk_tags", "risks")),
                "preferred_worker_ids": "",
                "avoided_worker_ids": "",
                "profile_is_stale": _bool_text(_value(row, "profile_is_stale", "profile_stale")),
                "source_ref": _source_ref("service_users", row_number),
            }
        )
    return output


def _sessions(
    rows: Iterable[Mapping[str, str]],
    worker_lookup: Mapping[str, str],
    service_user_lookup: Mapping[str, str],
    issues: list[AdapterIssue],
) -> list[dict[str, str]]:
    output = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        name = _value(row, "service_user_id", "child_id", "child_name", "service_user", "child", "name")
        service_user_id = _resolve_name(name, service_user_lookup, issues, "sessions", row_number, "service_user")
        start = _datetime_text(row, "start", "session_start")
        end = _datetime_text(row, "end", "session_end")
        if not service_user_id or not start or not end:
            _add_issue(issues, "warning", "incomplete_session_row", "session row skipped because child/start/end is incomplete", "sessions", row_number)
            continue
        session_id = _value(row, "session_id", "booking_id") or _stable_id("session", f"{service_user_id}-{start}-{end}")
        if session_id in seen:
            _add_issue(issues, "error", "duplicate_session_id", f"duplicate session id {session_id}", "sessions", row_number, "session_id")
            continue
        seen.add(session_id)
        assigned_ids = _resolve_many(_value(row, "assigned_worker_ids", "staff", "worker", "support_worker"), worker_lookup, issues, "sessions", row_number, "assigned_worker_ids")
        output.append(
            {
                "session_id": session_id,
                "service_user_id": service_user_id,
                "start": start,
                "end": end,
                "location_label": _value(row, "location", "venue"),
                "location_postcode": _value(row, "postcode", "location_postcode"),
                "location_latitude": "",
                "location_longitude": "",
                "location_area": _area(_value(row, "area", "location_area", "postcode", "location_postcode")),
                "required_worker_count": _value(row, "required_worker_count", "workers_required", "staff_required") or "1",
                "assigned_worker_ids": "|".join(assigned_ids),
                "assigned_roles": "|".join(_roles(len(assigned_ids))),
                "requires_driver": _bool_text(_value(row, "requires_driver", "driver_required", "needs_driver")),
                "priority": _value(row, "priority") or "1.0",
                "status": _status(_value(row, "status"), assigned_ids),
                "travel_buffer_before_minutes": _value(row, "travel_buffer_before_minutes"),
                "travel_buffer_after_minutes": _value(row, "travel_buffer_after_minutes"),
                "source_ref": _source_ref("sessions", row_number),
            }
        )
    return output


def _availability(
    rows: Iterable[Mapping[str, str]],
    worker_lookup: Mapping[str, str],
    issues: list[AdapterIssue],
) -> list[dict[str, str]]:
    output = []
    for row_number, row in enumerate(rows, start=2):
        worker_name = _value(row, "worker_id", "staff_id", "staff_name", "support_worker", "worker", "name")
        worker_id = _resolve_name(worker_name, worker_lookup, issues, "availability", row_number, "worker")
        start = _datetime_text(row, "start", "available_from")
        end = _datetime_text(row, "end", "available_to")
        if not worker_id or not start or not end:
            _add_issue(issues, "warning", "incomplete_availability_row", "availability row skipped because worker/start/end is incomplete", "availability", row_number)
            continue
        output.append(
            {
                "availability_id": _value(row, "availability_id") or _stable_id("availability", f"{worker_id}-{start}-{end}"),
                "worker_id": worker_id,
                "start": start,
                "end": end,
                "source_ref": _source_ref("availability", row_number),
            }
        )
    return output


def _compatibility(
    rows: Iterable[Mapping[str, str]],
    worker_lookup: Mapping[str, str],
    service_user_lookup: Mapping[str, str],
    issues: list[AdapterIssue],
) -> list[dict[str, str]]:
    output = []
    for row_number, row in enumerate(rows, start=2):
        worker_id = _resolve_name(_value(row, "worker_id", "staff_id", "staff_name", "support_worker", "worker"), worker_lookup, issues, "compatibility", row_number, "worker")
        service_user_id = _resolve_name(_value(row, "service_user_id", "child_id", "child_name", "service_user", "child"), service_user_lookup, issues, "compatibility", row_number, "service_user")
        if not worker_id or not service_user_id:
            continue
        output.append(
            {
                "worker_id": worker_id,
                "service_user_id": service_user_id,
                "status": _compatibility_status(_value(row, "status", "matching", "appropriate")),
                "reason": _value(row, "reason", "notes"),
                "source_ref": _source_ref("compatibility", row_number),
            }
        )
    return output


def _family_requests(
    rows: Iterable[Mapping[str, str]],
    worker_lookup: Mapping[str, str],
    service_user_lookup: Mapping[str, str],
    issues: list[AdapterIssue],
) -> list[dict[str, str]]:
    output = []
    for row_number, row in enumerate(rows, start=2):
        service_user_id = _resolve_name(_value(row, "service_user_id", "child_id", "child_name", "service_user", "child"), service_user_lookup, issues, "family_requests", row_number, "service_user")
        if not service_user_id:
            continue
        requested = _resolve_many(_value(row, "requested_worker_ids", "requested_staff", "preferred_worker", "staff"), worker_lookup, issues, "family_requests", row_number, "requested_worker_ids")
        avoided = _resolve_many(_value(row, "avoided_worker_ids", "avoided_staff"), worker_lookup, issues, "family_requests", row_number, "avoided_worker_ids")
        output.append(
            {
                "family_request_id": _value(row, "family_request_id", "request_id") or _stable_id("request", f"{service_user_id}-{row_number}"),
                "service_user_id": service_user_id,
                "requested_worker_ids": "|".join(requested),
                "avoided_worker_ids": "|".join(avoided),
                "preferred_days": _pipe(_value(row, "preferred_days")),
                "preferred_time_ranges": _pipe(_value(row, "preferred_time_ranges", "requested_windows")),
                "unavailable_time_ranges": _pipe(_value(row, "unavailable_time_ranges")),
                "notes": _value(row, "notes", "request"),
                "source_ref": _source_ref("family_requests", row_number),
            }
        )
    return output


def _read_csv(path: Path) -> tuple[dict[str, str], ...]:
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        return tuple({_normalize_header(key): (value or "").strip() for key, value in row.items() if key} for row in reader)


def _read_workbook_tables(path: Path, issues: list[AdapterIssue]) -> dict[str, tuple[dict[str, str], ...]]:
    tables: dict[str, tuple[dict[str, str], ...]] = {}
    try:
        sheets = _xlsx_sheets(path)
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        _add_issue(issues, "error", "invalid_workbook", f"could not read workbook structure: {error}", str(path))
        return tables
    for sheet_name, target in BOOKING_SHEETS.items():
        rows = sheets.get(sheet_name)
        if rows is None:
            _add_issue(issues, "warning", "missing_workbook_sheet", f"workbook sheet is missing: {sheet_name}", str(path))
            continue
        tables[target] = rows
    return tables


def _xlsx_sheets(path: Path) -> dict[str, tuple[dict[str, str], ...]]:
    ns = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
    }
    with zipfile.ZipFile(path) as archive:
        shared_strings = _shared_strings(archive, ns)
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_targets = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rels.findall("pkg:Relationship", ns)
        }
        sheets = {}
        for sheet in workbook.findall("main:sheets/main:sheet", ns):
            name = sheet.attrib["name"]
            rel_id = sheet.attrib[f"{{{ns['rel']}}}id"]
            target = rel_targets[rel_id]
            worksheet_path = "xl/" + target.lstrip("/")
            matrix = _worksheet_matrix(archive.read(worksheet_path), shared_strings, ns)
            sheets[name] = _matrix_to_dicts(matrix)
        return sheets


def _shared_strings(archive: zipfile.ZipFile, ns: Mapping[str, str]) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    values = []
    for item in root.findall("main:si", ns):
        values.append("".join(text.text or "" for text in item.findall(".//main:t", ns)))
    return values


def _worksheet_matrix(xml_bytes: bytes, shared_strings: list[str], ns: Mapping[str, str]) -> list[list[str]]:
    root = ElementTree.fromstring(xml_bytes)
    rows = []
    for row in root.findall("main:sheetData/main:row", ns):
        values = []
        current_index = 0
        for cell in row.findall("main:c", ns):
            cell_ref = cell.attrib.get("r", "")
            index = _column_index(re.sub(r"\d+", "", cell_ref)) if cell_ref else current_index
            while current_index < index:
                values.append("")
                current_index += 1
            values.append(_cell_value(cell, shared_strings, ns))
            current_index += 1
        rows.append(values)
    return rows


def _cell_value(cell, shared_strings: list[str], ns: Mapping[str, str]) -> str:
    if cell.attrib.get("t") == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//main:t", ns)).strip()
    value = cell.find("main:v", ns)
    if value is None or value.text is None:
        return ""
    if cell.attrib.get("t") == "s":
        return shared_strings[int(value.text)].strip()
    return value.text.strip()


def _matrix_to_dicts(matrix: list[list[str]]) -> tuple[dict[str, str], ...]:
    while matrix and not any(cell.strip() for cell in matrix[0]):
        matrix.pop(0)
    if not matrix:
        return ()
    headers = [_normalize_header(value) for value in matrix[0]]
    rows = []
    for values in matrix[1:]:
        if not any(value.strip() for value in values):
            continue
        rows.append({headers[index]: values[index].strip() if index < len(values) else "" for index in range(len(headers)) if headers[index]})
    return tuple(rows)


def _write_csv(path: Path, headers: list[str], rows: Iterable[Mapping[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({header: row.get(header, "") for header in headers})


def _write_adapter_report(path: Path, report: WorkbookAdapterReport) -> None:
    payload = {
        "source_dir": report.source_dir,
        "output_dir": report.output_dir,
        "row_counts": dict(report.row_counts),
        "issues": [
            {
                "severity": issue.severity,
                "code": issue.code,
                "message": issue.message,
                "source": issue.source,
                "row_number": issue.row_number,
                "field": issue.field,
            }
            for issue in report.issues
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _lookup(rows: Iterable[Mapping[str, str]], id_field: str, name_field: str) -> dict[str, str]:
    lookup = {}
    name_counts: dict[str, int] = {}
    cached_rows = tuple(rows)
    for row in cached_rows:
        name_counts[_norm_name(row[name_field])] = name_counts.get(_norm_name(row[name_field]), 0) + 1
    for row in cached_rows:
        row_id = row[id_field]
        lookup[_norm_name(row_id)] = row_id
        name_key = _norm_name(row[name_field])
        if name_counts[name_key] == 1:
            lookup[name_key] = row_id
    return lookup


def _warn_duplicate_names(rows: Iterable[Mapping[str, str]], source: str, issues: list[AdapterIssue]) -> None:
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for row in rows:
        key = _norm_name(row.get("name", ""))
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        names[key] = row.get("name", "")
    for key, count in counts.items():
        if count > 1:
            _add_issue(
                issues,
                "warning",
                "ambiguous_name",
                f"{source} name is duplicated and must be resolved by explicit ID: {names[key]}",
                source,
                None,
                "name",
            )


def _resolve_name(
    value: str,
    lookup: Mapping[str, str],
    issues: list[AdapterIssue],
    source: str,
    row_number: int,
    field: str,
) -> str:
    if not value:
        return ""
    key = _norm_name(value)
    if key in lookup:
        return lookup[key]
    _add_issue(issues, "warning", "unresolved_name", f"could not resolve {field}: {value}", source, row_number, field)
    return ""


def _resolve_many(
    value: str,
    lookup: Mapping[str, str],
    issues: list[AdapterIssue],
    source: str,
    row_number: int,
    field: str,
) -> list[str]:
    resolved = []
    for item in _split(value):
        item_id = _resolve_name(item, lookup, issues, source, row_number, field)
        if item_id and item_id not in resolved:
            resolved.append(item_id)
    return resolved


def _value(row: Mapping[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(_normalize_header(key), "")
        if value:
            return value.strip()
    return ""


def _datetime_text(row: Mapping[str, str], *keys: str) -> str:
    value = _value(row, *keys)
    if value:
        return _coerce_datetime(value)
    date = _value(row, "date", "session_date")
    start = _value(row, "start_time", "from")
    end_key = keys[-1] if keys else ""
    if "end" in end_key:
        start = _value(row, "end_time", "to")
    if date and start:
        combined = f"{_coerce_date(date)}T{_coerce_time(start)}"
        if "+" not in combined and not combined.endswith("Z"):
            combined += "+01:00"
        return combined
    return ""


def _coerce_datetime(value: str) -> str:
    text = value.strip()
    if _is_number(text):
        return _excel_serial_datetime(float(text)).isoformat()
    if "T" in text or " " in text:
        normalized = text.replace(" ", "T", 1)
        if "+" not in normalized and not normalized.endswith("Z"):
            normalized += "+01:00"
        return normalized
    return text


def _coerce_date(value: str) -> str:
    text = value.strip()
    if _is_number(text):
        return _excel_serial_datetime(float(text)).date().isoformat()
    return text


def _coerce_time(value: str) -> str:
    text = value.strip()
    if _is_number(text):
        serial = float(text)
        fraction = serial % 1
        seconds = round(fraction * 24 * 60 * 60)
        parsed = (datetime.combine(datetime(2000, 1, 1).date(), time()) + timedelta(seconds=seconds)).time()
        return parsed.isoformat(timespec="minutes")
    return text


def _excel_serial_datetime(value: float) -> datetime:
    base = datetime(1899, 12, 30, tzinfo=timezone(timedelta(hours=1)))
    return base + timedelta(days=value)


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).strip()


def _stable_id(prefix: str, value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")[:32] or "unknown"
    digest = hashlib.sha1(f"{prefix}:{_norm_name(value)}".encode("utf-8")).hexdigest()[:8]
    checksum = str(int(digest, 16) % 100000).zfill(5)
    return f"{prefix}-{slug}-{checksum}"


def _split(value: str) -> list[str]:
    if not value:
        return []
    normalized = value.replace("\n", "|").replace(";", "|").replace(",", "|")
    return [item.strip() for item in normalized.split("|") if item.strip()]


def _pipe(value: str) -> str:
    return "|".join(dict.fromkeys(_split(value)))


def _modes(value: str) -> set[str]:
    text = value.lower()
    modes = set()
    if any(token in text for token in ("drive", "driver", "car", "yes", "true")):
        modes.add("drive")
    if "bus" in text:
        modes.add("bus")
    if "tram" in text:
        modes.add("tram")
    if "train" in text:
        modes.add("train")
    if "cycle" in text or "bike" in text:
        modes.add("cycle")
    if "walk" in text:
        modes.add("walk")
    if "taxi" in text:
        modes.add("taxi")
    if "public" in text:
        modes.add("public_transport")
    return modes


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"true", "yes", "y", "1", "drive", "driver", "car"}


def _bool_text(value: str) -> str:
    return "true" if _truthy(value) else "false"


def _area(value: str) -> str:
    value = value.strip().upper()
    if not value:
        return ""
    return value.split()[0]


def _status(value: str, assigned_ids: list[str]) -> str:
    text = value.strip().lower()
    allowed = {"unallocated", "partially_allocated", "allocated", "confirmed", "cancelled", "rejected"}
    if text in allowed:
        return text
    return "partially_allocated" if assigned_ids else "unallocated"


def _compatibility_status(value: str) -> str:
    text = value.strip().lower()
    if text in {"yes", "y", "true", "appropriate", "matched", "suitable"}:
        return "appropriate"
    if text in {"no", "n", "false", "inappropriate", "avoid", "unsuitable"}:
        return "inappropriate"
    return "unknown"


def _roles(count: int) -> list[str]:
    if count <= 0:
        return []
    return ["lead"] + [f"second_{index}" for index in range(1, count)]


def _source_ref(source: str, row_number: int) -> str:
    return f"workbook-{source}-row-{row_number}"


def _source_refs(source_dir: Path, booking_workbook: str | Path | None) -> set[str]:
    refs = {str(source_dir / filename) for filename in SOURCE_FILES.values() if (source_dir / filename).is_file()}
    if booking_workbook:
        refs.add(str(Path(booking_workbook)))
    return refs


def _first_xlsx(source_dir: Path) -> Path | None:
    matches = sorted(source_dir.glob("*.xlsx"))
    return matches[0] if matches else None


def _column_index(column: str) -> int:
    index = 0
    for char in column:
        index = index * 26 + ord(char.upper()) - ord("A") + 1
    return max(0, index - 1)


def _add_issue(
    issues: list[AdapterIssue],
    severity: str,
    code: str,
    message: str,
    source: str | None = None,
    row_number: int | None = None,
    field: str | None = None,
) -> None:
    issues.append(AdapterIssue(severity, code, message, source, row_number, field))


def _issue_sort_key(issue: AdapterIssue) -> tuple:
    return (issue.severity, issue.source or "", issue.row_number or 0, issue.code, issue.field or "")


def _print_issues(issues: Iterable[AdapterIssue]) -> None:
    for issue in issues:
        location = f" {issue.source or ''}:{issue.row_number or ''}".rstrip(":")
        print(f"- {issue.severity}:{issue.code}{location}: {issue.message}")


if __name__ == "__main__":
    raise SystemExit(main())
