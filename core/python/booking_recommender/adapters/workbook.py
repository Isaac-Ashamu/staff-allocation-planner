"""Booking-workbook source adapter for the standardized CSV v1 import contract.

This adapter accepts read-only CSV exports, plus an optional simple XLSX booking
workbook with the booking sheet names observed in local source data. It writes a
new standardized import folder; it never edits source files in place.
"""

from __future__ import annotations

import argparse
import csv
import email
import hashlib
import json
import posixpath
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email import policy
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from xml.etree import ElementTree

from ..importer import load_standardized_csv
from .llm_ad_hoc_parser import (
    AnthropicAdHocResolver,
    GapRowResult,
    LlmAdHocParsingConfig,
    LlmCallLimitExceeded,
    LlmCallStats,
    llm_config_from_env,
    meets_confidence,
)


STANDARD_TABLE_HEADERS = {
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
        "target_sessions_per_week",
        "target_session_duration_minutes",
        "preferred_time_window_start",
        "preferred_time_window_end",
        "preferred_day_patterns",
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

REQUEST_FILE_EXTENSIONS = {".eml", ".docx", ".txt", ".msg"}
AVAILABILITY_REPORT_ALL_DAY_START = "08:00:00"
AVAILABILITY_REPORT_ALL_DAY_END = "23:00:00"


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
    alias_config_path: str | Path | None = None,
    import_run_id: str | None = None,
    imported_at: datetime | None = None,
    source_system: str = "workbook-adapter",
    llm_ad_hoc_parsing: bool = False,
    llm_resolver: AnthropicAdHocResolver | None = None,
) -> WorkbookAdapterReport:
    source_path = Path(source_dir)
    output_path = Path(output_dir)
    issues: list[AdapterIssue] = []

    workbook_path = Path(booking_workbook) if booking_workbook else None
    source_tables = _read_source_tables(source_path, workbook_path, issues)
    alias_config = _read_alias_config(_alias_config_path(source_path, alias_config_path), issues)
    worker_rows = _workers(source_tables.get("workers", ()), issues)
    worker_rows = _add_local_workers(worker_rows, alias_config.get("workers"), issues)
    worker_rows = _augment_workers_from_session_notes(worker_rows, source_tables.get("sessions", ()), issues)
    _warn_duplicate_names(worker_rows, "workers", issues)
    worker_lookup = _lookup(worker_rows, "worker_id", "name")
    _apply_aliases(worker_lookup, alias_config.get("worker_aliases"), "workers", issues)
    _add_session_worker_aliases(worker_lookup, source_tables.get("sessions", ()), issues)
    service_user_rows = _service_users(source_tables.get("service_users", ()), issues)
    service_user_rows = _add_local_service_users(service_user_rows, alias_config.get("service_users"), issues)
    _warn_duplicate_names(service_user_rows, "service_users", issues)
    service_user_lookup = _lookup(service_user_rows, "service_user_id", "name")
    _apply_aliases(service_user_lookup, alias_config.get("service_user_aliases"), "service_users", issues)
    service_users_by_id = {row["service_user_id"]: row for row in service_user_rows}

    session_rows = _sessions(
        source_tables.get("sessions", ()),
        worker_lookup,
        service_user_lookup,
        service_users_by_id,
        issues,
    )
    session_rows = _deduplicate_sessions(session_rows, issues)
    target_month = _target_month_from_sessions(session_rows)
    availability_report_rows = _read_availability_report_pdfs(source_path, issues, target_month=target_month)
    combined_availability_rows = tuple(source_tables.get("availability", ())) + tuple(availability_report_rows)
    if not combined_availability_rows:
        _add_issue(
            issues,
            "warning",
            "missing_availability_source",
            "missing availability source; workers may be treated as generally available",
            SOURCE_FILES["availability"],
        )
    availability_rows = _availability(combined_availability_rows, worker_lookup, issues)
    compatibility_rows = _compatibility(
        source_tables.get("compatibility", ()),
        worker_lookup,
        service_user_lookup,
        issues,
    )
    resolved_llm_resolver = llm_resolver
    if resolved_llm_resolver is None and llm_ad_hoc_parsing:
        llm_config = llm_config_from_env()
        if llm_config.enabled:
            resolved_llm_resolver = AnthropicAdHocResolver(config=llm_config)
        else:
            _add_issue(
                issues, "warning", "llm_ad_hoc_parsing_unavailable",
                f"--enable-llm-ad-hoc-parsing was set but {llm_config.anthropic_api_key_env} is not set; "
                "continuing with deterministic parsing only",
                "family_requests",
            )
    family_request_rows = _family_requests(
        tuple(source_tables.get("family_requests", ())) + tuple(source_tables.get("ad_hocs", ())),
        worker_lookup,
        service_user_lookup,
        issues,
        worker_rows=worker_rows,
        service_user_rows=service_user_rows,
        llm_resolver=resolved_llm_resolver,
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
    parser.add_argument("--alias-config", help="Optional local alias/config JSON file for tenant-specific name corrections.")
    parser.add_argument("--import-run-id", help="Stable import run ID to write into import_manifest.json.")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Load the generated standardized output with the tenant-agnostic importer.",
    )
    parser.add_argument(
        "--enable-llm-ad-hoc-parsing",
        action="store_true",
        help=(
            "Use Claude to fill in ad hoc workbook rows (Phase 1/Phase 2 sheets) the "
            "deterministic parser could not -- free-text day/time patterns and either/or "
            "scheduling. Off by default so this adapter never calls external services unless "
            "asked. Requires ANTHROPIC_API_KEY; only high-confidence name matches are applied "
            "automatically, everything else is surfaced as a warning for review."
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        report = convert_workbook_sources(
            args.source_dir,
            args.output_dir,
            booking_workbook=args.booking_workbook,
            alias_config_path=args.alias_config,
            import_run_id=args.import_run_id,
            llm_ad_hoc_parsing=args.enable_llm_ad_hoc_parsing,
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

    workbook_paths = (booking_workbook,) if booking_workbook else tuple(sorted(source_dir.glob("*.xlsx")))
    for workbook_path in workbook_paths:
        if workbook_path and workbook_path.is_file():
            workbook_tables = _read_workbook_tables(workbook_path, issues)
            for key, rows in workbook_tables.items():
                if not rows:
                    continue
                if key == "family_requests":
                    # Every row reaching "family_requests" via a workbook sheet comes from an
                    # ad-hoc-shaped sheet per _workbook_sheet_target (Phase 1/Phase 2/ad
                    # hocs/date requests) -- tag it so LLM-assisted gap-filling can be scoped to
                    # exactly this source, not the separate raw .eml/.docx/.msg pathway.
                    rows = tuple({**row, "_source_kind": "ad_hoc_workbook"} for row in rows)
                tables[key] = tuple(tables.get(key, ())) + tuple(rows)

    request_rows = _read_request_files(source_dir, issues)
    if request_rows:
        tables["family_requests"] = tuple(tables.get("family_requests", ())) + tuple(request_rows)

    for required_key in ("workers", "service_users", "sessions"):
        if not tables.get(required_key):
            _add_issue(
                issues,
                "error",
                "missing_source_table",
                f"missing required source table: {required_key}",
                SOURCE_FILES[required_key],
            )
    # Availability report PDFs are read later, in convert_workbook_sources, once the target
    # import month is known from parsed session dates -- see _read_availability_report_pdfs.
    return tables


def _alias_config_path(source_dir: Path, explicit_path: str | Path | None) -> Path:
    if explicit_path:
        return Path(explicit_path)
    return source_dir.parent / "config" / "aliases.local.json"


def _read_alias_config(path: Path, issues: list[AdapterIssue]) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _add_issue(issues, "error", "invalid_alias_config", f"could not read alias config: {error}", str(path))
        return {}
    if not isinstance(value, dict):
        _add_issue(issues, "error", "invalid_alias_config", "alias config must be a JSON object", str(path))
        return {}
    return value


def _workers(rows: Iterable[Mapping[str, str]], issues: list[AdapterIssue]) -> list[dict[str, str]]:
    output = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        name = _value(
            row,
            "name",
            "staff_name",
            "support_worker",
            "support worker",
            "worker",
            "worker_name",
            "sw_name",
            "support_worker_name",
        )
        if not name:
            _add_issue(issues, "warning", "blank_worker_name", "blank worker row skipped", "workers", row_number, "name")
            continue
        worker_id = _value(row, "worker_id", "staff_id", "sw_id", "support_worker_id") or _stable_id("worker", name)
        if worker_id in seen:
            _add_issue(issues, "error", "duplicate_worker_id", f"duplicate worker id {worker_id}", "workers", row_number, "worker_id")
            continue
        seen.add(worker_id)
        postcode = _postcode(_value(row, "postcode", "home_postcode", "staff_postcode", "sw_postcode", "support_worker_postcode", "address"))
        modes = _modes(_value(row, "travel_modes", "transport", "travel", "drives", "how_they_get_to_sessions"))
        has_car = "true" if "drive" in modes or _truthy(_value(row, "has_car", "car", "drives")) else "false"
        output.append(
            {
                "worker_id": worker_id,
                "name": name,
                "gender": _gender_text(_value(row, "gender", "sex")),
                "home_label": "",
                "home_postcode": postcode,
                "home_latitude": "",
                "home_longitude": "",
                "home_area": _area(_value(row, "area", "home_area") or postcode),
                "travel_modes": "|".join(sorted(modes or {"public_transport", "walk"})),
                "has_car": has_car,
                "skills": _pipe(_value(row, "skills", "training")),
                "languages": _pipe(_value(row, "languages", "languages_spoken")),
                "willing_areas": _pipe(_value(row, "willing_areas", "areas")),
                "current_hours": _value(row, "current_hours", "hours") or "0",
                "max_hours": _value(row, "max_hours"),
                "preference_tags": _pipe(_value(row, "preference_tags", "preferences")),
                "reliability_score": "",
                "source_ref": _source_ref("workers", row_number),
            }
        )
    return output


def _apply_aliases(
    lookup: dict[str, str],
    aliases_by_canonical: object,
    source: str,
    issues: list[AdapterIssue],
) -> None:
    if aliases_by_canonical is None:
        return
    if not isinstance(aliases_by_canonical, dict):
        _add_issue(issues, "warning", "invalid_aliases", f"{source} aliases must be an object", source)
        return
    for canonical, aliases in aliases_by_canonical.items():
        canonical_key = _norm_name(str(canonical))
        target_id = lookup.get(canonical_key)
        if not target_id:
            _add_issue(
                issues,
                "warning",
                "unknown_alias_target",
                f"alias target is not in {source}: {canonical}",
                source,
                None,
                "aliases",
            )
            continue
        if isinstance(aliases, str):
            alias_values = [aliases]
        elif isinstance(aliases, list):
            alias_values = [str(item) for item in aliases]
        else:
            _add_issue(issues, "warning", "invalid_aliases", f"aliases for {canonical} must be a string or list", source)
            continue
        for alias in alias_values:
            alias_key = _norm_name(alias)
            if not alias_key:
                continue
            existing = lookup.get(alias_key)
            if existing and existing != target_id:
                _add_issue(
                    issues,
                    "warning",
                    "conflicting_alias",
                    f"alias {alias} maps to more than one {source} record",
                    source,
                    None,
                    "aliases",
                )
                continue
            lookup[alias_key] = target_id


def _add_local_workers(
    worker_rows: list[dict[str, str]],
    local_records: object,
    issues: list[AdapterIssue],
) -> list[dict[str, str]]:
    if local_records is None:
        return worker_rows
    if not isinstance(local_records, list):
        _add_issue(issues, "warning", "invalid_local_workers", "workers local records must be a list", "workers")
        return worker_rows
    output = list(worker_rows)
    seen_ids = {row["worker_id"] for row in output}
    seen_names = {_norm_name(row["name"]) for row in output}
    for index, record in enumerate(local_records, start=1):
        if not isinstance(record, dict):
            _add_issue(issues, "warning", "invalid_local_worker", "local worker record must be an object", "workers", index)
            continue
        name = str(record.get("name") or "").strip()
        if not name:
            _add_issue(issues, "warning", "invalid_local_worker", "local worker record requires name", "workers", index, "name")
            continue
        worker_id = str(record.get("worker_id") or _stable_id("worker", name)).strip()
        if worker_id in seen_ids or _norm_name(name) in seen_names:
            continue
        seen_ids.add(worker_id)
        seen_names.add(_norm_name(name))
        postcode = _postcode(str(record.get("postcode") or record.get("home_postcode") or ""))
        modes = _modes(str(record.get("travel_modes") or record.get("transport") or ""))
        has_car = "true" if "drive" in modes or _truthy(str(record.get("has_car") or "")) else "false"
        output.append(
            {
                "worker_id": worker_id,
                "name": name,
                "gender": _gender_text(str(record.get("gender") or record.get("sex") or "")),
                "home_label": "",
                "home_postcode": postcode,
                "home_latitude": "",
                "home_longitude": "",
                "home_area": _area(postcode),
                "travel_modes": "|".join(sorted(modes or {"public_transport", "walk"})),
                "has_car": has_car,
                "skills": _pipe(str(record.get("skills") or "")),
                "languages": _pipe(str(record.get("languages") or "")),
                "willing_areas": _pipe(str(record.get("willing_areas") or "")),
                "current_hours": str(record.get("current_hours") or "0"),
                "max_hours": str(record.get("max_hours") or ""),
                "preference_tags": _pipe(str(record.get("preference_tags") or "")),
                "reliability_score": "",
                "source_ref": "local-alias-config",
            }
        )
        _add_issue(
            issues,
            "warning",
            "worker_added_from_alias_config",
            f"added local worker record from alias config: {worker_id}",
            "workers",
            index,
            "worker_id",
        )
    return output


def _augment_workers_from_session_notes(
    worker_rows: list[dict[str, str]],
    session_rows: Iterable[Mapping[str, str]],
    issues: list[AdapterIssue],
) -> list[dict[str, str]]:
    lookup = _lookup(worker_rows, "worker_id", "name")
    for row_number, row in enumerate(session_rows, start=2):
        for value, note in _session_worker_value_notes(row):
            if not note:
                continue
            if _resolve_reference_text(note, lookup, issues, "sessions", row_number, "assigned_worker_ids", warn=False):
                continue
            _add_issue(
                issues,
                "warning",
                "unresolved_import_sheet_worker_note",
                f"import-sheet worker note could not be resolved to a staff record: {note}",
                "sessions",
                row_number,
                "assigned_worker_ids",
            )
    return worker_rows


def _add_session_worker_aliases(
    worker_lookup: dict[str, str],
    session_rows: Iterable[Mapping[str, str]],
    issues: list[AdapterIssue],
) -> None:
    aliases: dict[str, str] = {}
    for row_number, row in enumerate(session_rows, start=2):
        for value, note in _session_worker_value_notes(row):
            if not value or not note:
                continue
            resolved_id = _resolve_reference_text(note, worker_lookup, issues, "sessions", row_number, "assigned_worker_ids", warn=False)
            if not resolved_id:
                continue
            key = _norm_name(value)
            if key in aliases and aliases[key] != resolved_id:
                _add_issue(
                    issues,
                    "warning",
                    "conflicting_import_sheet_worker_alias",
                    f"SW ID {value} maps to multiple workers in cell notes",
                    "sessions",
                    row_number,
                    "assigned_worker_ids",
                )
                continue
            aliases[key] = resolved_id
    worker_lookup.update(aliases)


def _session_worker_value_notes(row: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return (
        (
            _value(row, "lead_sw_id", "lead_worker_id", "lead_staff_id", "lead_support_worker"),
            _value(row, "lead_sw_id_note", "lead_worker_id_note", "lead_staff_id_note", "lead_support_worker_note"),
        ),
        (
            _value(row, "2nd_sw_id", "second_sw_id", "second_worker_id", "second_staff_id", "second_support_worker"),
            _value(row, "2nd_sw_id_note", "second_sw_id_note", "second_worker_id_note", "second_staff_id_note", "second_support_worker_note"),
        ),
    )


def _service_users(rows: Iterable[Mapping[str, str]], issues: list[AdapterIssue]) -> list[dict[str, str]]:
    output = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        name = _value(row, "name", "child_name", "client_name", "service_user", "service_user_name", "child")
        if not name:
            _add_issue(issues, "warning", "blank_service_user_name", "blank service-user row skipped", "service_users", row_number, "name")
            continue
        service_user_id = _value(row, "service_user_id", "child_id", "su_id", "service_user_id") or _stable_id("su", name)
        if service_user_id in seen:
            _add_issue(issues, "error", "duplicate_service_user_id", f"duplicate service-user id {service_user_id}", "service_users", row_number, "service_user_id")
            continue
        seen.add(service_user_id)
        output.append(
            {
                "service_user_id": service_user_id,
                "name": name,
                "gender": _gender_text(_value(row, "gender", "sex")),
                "home_label": "",
                "home_postcode": _postcode(_value(row, "postcode", "home_postcode", "child_postcode", "su_postcode", "service_user_postcode", "address")),
                "home_latitude": "",
                "home_longitude": "",
                "home_area": _area(_value(row, "area", "home_area", "postcode", "home_postcode", "child_postcode", "su_postcode", "service_user_postcode", "address")),
                "support_level": _value(row, "support_level", "support") or "standard",
                "required_worker_count": _value(row, "required_worker_count", "workers_required", "staff_required") or ("2" if _truthy_2_to_1(_value(row, "2_1", "2to1", "support_ratio")) else "1"),
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


def _add_local_service_users(
    service_user_rows: list[dict[str, str]],
    local_records: object,
    issues: list[AdapterIssue],
) -> list[dict[str, str]]:
    if local_records is None:
        return service_user_rows
    if not isinstance(local_records, list):
        _add_issue(issues, "warning", "invalid_local_service_users", "service_users local records must be a list", "service_users")
        return service_user_rows
    output = list(service_user_rows)
    seen_ids = {row["service_user_id"] for row in output}
    seen_names = {_norm_name(row["name"]) for row in output}
    for index, record in enumerate(local_records, start=1):
        if not isinstance(record, dict):
            _add_issue(issues, "warning", "invalid_local_service_user", "local service-user record must be an object", "service_users", index)
            continue
        name = str(record.get("name") or "").strip()
        if not name:
            _add_issue(issues, "warning", "invalid_local_service_user", "local service-user record requires name", "service_users", index, "name")
            continue
        service_user_id = str(record.get("service_user_id") or _stable_id("su", name)).strip()
        if service_user_id in seen_ids or _norm_name(name) in seen_names:
            continue
        seen_ids.add(service_user_id)
        seen_names.add(_norm_name(name))
        postcode = _postcode(str(record.get("postcode") or record.get("home_postcode") or ""))
        required_count = str(record.get("required_worker_count") or ("2" if _truthy_2_to_1(str(record.get("support_level") or "")) else "1"))
        output.append(
            {
                "service_user_id": service_user_id,
                "name": name,
                "gender": _gender_text(str(record.get("gender") or record.get("sex") or "")),
                "home_label": "",
                "home_postcode": postcode,
                "home_latitude": "",
                "home_longitude": "",
                "home_area": _area(postcode),
                "support_level": str(record.get("support_level") or "standard"),
                "required_worker_count": required_count,
                "requires_driver": _bool_text(str(record.get("requires_driver") or "")),
                "required_skills": _pipe(str(record.get("required_skills") or "")),
                "communication_needs": _pipe(str(record.get("communication_needs") or "")),
                "interests": _pipe(str(record.get("interests") or "")),
                "risk_tags": _pipe(str(record.get("risk_tags") or "")),
                "preferred_worker_ids": _pipe(str(record.get("preferred_worker_ids") or "")),
                "avoided_worker_ids": _pipe(str(record.get("avoided_worker_ids") or "")),
                "profile_is_stale": _bool_text(str(record.get("profile_is_stale") or "")),
                "source_ref": "local-alias-config",
            }
        )
        _add_issue(
            issues,
            "warning",
            "service_user_added_from_alias_config",
            f"added local service-user record from alias config: {service_user_id}",
            "service_users",
            index,
            "service_user_id",
        )
    return output


def _sessions(
    rows: Iterable[Mapping[str, str]],
    worker_lookup: Mapping[str, str],
    service_user_lookup: Mapping[str, str],
    service_users_by_id: Mapping[str, Mapping[str, str]],
    issues: list[AdapterIssue],
) -> list[dict[str, str]]:
    output = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        service_user_id = _resolve_first(
            (
                _value(row, "service_user_id"),
                _value(row, "child_id"),
                _value(row, "su_id"),
                _value(row, "child_name"),
                _value(row, "client_name"),
                _value(row, "service_user"),
                _value(row, "service_user_name"),
                _value(row, "child"),
                _value(row, "name"),
            ),
            service_user_lookup,
            issues,
            "sessions",
            row_number,
            "service_user",
        )
        start = _datetime_text(row, "start", "session_start")
        end = _datetime_text(row, "end", "session_end")
        if not service_user_id or not start or not end:
            _add_issue(issues, "warning", "incomplete_session_row", "session row skipped because child/start/end is incomplete", "sessions", row_number)
            continue
        starts_and_ends = _recurring_session_datetimes(row, start, end, issues, row_number)
        base_session_id = _value(row, "session_id", "booking_id") or _stable_id("session", f"{service_user_id}-{start}-{end}")
        assigned_ids = _assigned_workers(row, worker_lookup, issues, row_number)
        required_count = _required_worker_count(row, assigned_ids)
        for index, (occurrence_start, occurrence_end) in enumerate(starts_and_ends):
            session_id = _occurrence_id(base_session_id, occurrence_start, index, len(starts_and_ends))
            if session_id in seen:
                _add_issue(issues, "error", "duplicate_session_id", f"duplicate session id {session_id}", "sessions", row_number, "session_id")
                continue
            seen.add(session_id)
            location = _session_location_fields(row, service_users_by_id.get(service_user_id))
            output.append(
                {
                    "session_id": session_id,
                    "service_user_id": service_user_id,
                    "start": occurrence_start,
                    "end": occurrence_end,
                    "location_label": location["label"],
                    "location_postcode": location["postcode"],
                    "location_latitude": location["latitude"],
                    "location_longitude": location["longitude"],
                    "location_area": location["area"],
                    "required_worker_count": str(required_count),
                    "assigned_worker_ids": "|".join(assigned_ids),
                    "assigned_roles": "|".join(_roles(len(assigned_ids))),
                    "requires_driver": _bool_text(_value(row, "requires_driver", "driver_required", "needs_driver", "car")),
                    "priority": _value(row, "priority") or "1.0",
                    "status": _status(_value(row, "status"), assigned_ids, required_count),
                    "travel_buffer_before_minutes": _value(row, "travel_buffer_before_minutes"),
                    "travel_buffer_after_minutes": _value(row, "travel_buffer_after_minutes"),
                    "source_ref": _source_ref("sessions", row_number),
                }
            )
    return output


def _session_location_fields(
    row: Mapping[str, str],
    service_user_row: Mapping[str, str] | None,
) -> dict[str, str]:
    explicit = {
        "label": _value(row, "location", "venue", "location_label"),
        "postcode": _postcode(_value(row, "location_postcode", "postcode")),
        "latitude": _value(row, "location_latitude"),
        "longitude": _value(row, "location_longitude"),
        "area": _area(_value(row, "area", "location_area", "postcode", "location_postcode")),
    }
    if any(explicit.values()) or not service_user_row:
        return explicit
    return {
        "label": service_user_row.get("home_label", ""),
        "postcode": service_user_row.get("home_postcode", ""),
        "latitude": service_user_row.get("home_latitude", ""),
        "longitude": service_user_row.get("home_longitude", ""),
        "area": service_user_row.get("home_area", ""),
    }


def _deduplicate_sessions(
    rows: Iterable[Mapping[str, str]],
    issues: list[AdapterIssue],
) -> list[dict[str, str]]:
    merged: dict[tuple[str, str, str], dict[str, str]] = {}
    order: list[tuple[str, str, str]] = []
    for row in rows:
        start = _canonical_datetime_text(row["start"])
        end = _canonical_datetime_text(row["end"])
        key = (row["service_user_id"], start, end)
        normalized = dict(row)
        normalized["start"] = start
        normalized["end"] = end
        if key not in merged:
            merged[key] = normalized
            order.append(key)
            continue
        _merge_duplicate_session(merged[key], normalized, issues)
    return [merged[key] for key in order]


def _merge_duplicate_session(
    target: dict[str, str],
    duplicate: Mapping[str, str],
    issues: list[AdapterIssue],
) -> None:
    source_refs = _merge_pipe_values(target.get("source_ref", ""), duplicate.get("source_ref", ""))
    _add_issue(
        issues,
        "warning",
        "duplicate_logical_session_merged",
        (
            "merged duplicate session rows for "
            f"{target['service_user_id']} {target['start']} - {target['end']}: {source_refs}"
        ),
        "sessions",
        None,
        "session_id",
    )

    for field in (
        "location_label",
        "location_postcode",
        "location_latitude",
        "location_longitude",
        "location_area",
    ):
        _merge_prefer_existing_location(target, duplicate, field, issues)

    for field in ("required_worker_count", "requires_driver", "priority", "travel_buffer_before_minutes", "travel_buffer_after_minutes"):
        if target.get(field, "") != duplicate.get(field, "") and target.get(field) and duplicate.get(field):
            _add_issue(
                issues,
                "warning",
                "duplicate_session_detail_conflict",
                f"duplicate session rows disagree on {field}: {target.get(field)} / {duplicate.get(field)}",
                "sessions",
                None,
                field,
            )

    assigned_ids, assigned_roles = _merge_assignments(
        target.get("assigned_worker_ids", ""),
        target.get("assigned_roles", ""),
        duplicate.get("assigned_worker_ids", ""),
        duplicate.get("assigned_roles", ""),
        issues,
    )
    required_count = max(
        _int_or_default(target.get("required_worker_count", ""), 1),
        _int_or_default(duplicate.get("required_worker_count", ""), 1),
        len(assigned_ids),
    )
    target["required_worker_count"] = str(required_count)
    target["assigned_worker_ids"] = "|".join(assigned_ids)
    target["assigned_roles"] = "|".join(assigned_roles)
    target["requires_driver"] = "true" if target.get("requires_driver") == "true" or duplicate.get("requires_driver") == "true" else "false"
    target["priority"] = _max_decimal_text(target.get("priority", ""), duplicate.get("priority", ""))
    target["status"] = _merged_session_status(target.get("status", ""), duplicate.get("status", ""), assigned_ids, required_count, issues)
    target["source_ref"] = source_refs


def _merge_prefer_existing_location(
    target: dict[str, str],
    duplicate: Mapping[str, str],
    field: str,
    issues: list[AdapterIssue],
) -> None:
    existing = target.get(field, "")
    incoming = duplicate.get(field, "")
    if not existing and incoming:
        target[field] = incoming
        return
    if existing and incoming and existing != incoming:
        _add_issue(
            issues,
            "warning",
            "duplicate_session_detail_conflict",
            f"duplicate session rows disagree on {field}: {existing} / {incoming}",
            "sessions",
            None,
            field,
        )


def _merge_assignments(
    left_ids_text: str,
    left_roles_text: str,
    right_ids_text: str,
    right_roles_text: str,
    issues: list[AdapterIssue],
) -> tuple[list[str], list[str]]:
    assigned: dict[str, str] = {}
    for worker_id, role in _assignment_pairs(left_ids_text, left_roles_text) + _assignment_pairs(right_ids_text, right_roles_text):
        if worker_id in assigned:
            if assigned[worker_id] != role:
                _add_issue(
                    issues,
                    "warning",
                    "duplicate_session_detail_conflict",
                    f"duplicate session rows assign {worker_id} with conflicting roles: {assigned[worker_id]} / {role}",
                    "sessions",
                    None,
                    "assigned_roles",
                )
            continue
        assigned[worker_id] = role
    worker_ids = list(assigned)
    roles = list(assigned.values())
    if len(worker_ids) > 1 and len(set(roles)) < len(roles):
        roles = _roles(len(worker_ids))
    return worker_ids, roles


def _assignment_pairs(ids_text: str, roles_text: str) -> list[tuple[str, str]]:
    worker_ids = _split(ids_text)
    roles = _split(roles_text)
    if len(roles) < len(worker_ids):
        roles.extend(_roles(len(worker_ids))[len(roles) :])
    return [(worker_id, roles[index] if index < len(roles) else "lead") for index, worker_id in enumerate(worker_ids)]


def _merged_session_status(
    left: str,
    right: str,
    assigned_ids: list[str],
    required_count: int,
    issues: list[AdapterIssue],
) -> str:
    if left and right and left != right:
        _add_issue(
            issues,
            "warning",
            "duplicate_session_detail_conflict",
            f"duplicate session rows disagree on status: {left} / {right}",
            "sessions",
            None,
            "status",
        )
    terminal = {"cancelled", "rejected", "confirmed"}
    if left == right and left in terminal:
        return left
    return _status("", assigned_ids, required_count)


def _canonical_datetime_text(value: str) -> str:
    return _parse_datetime_text(value).isoformat()


def _merge_pipe_values(*values: str) -> str:
    merged: list[str] = []
    for value in values:
        for item in _split(value):
            if item not in merged:
                merged.append(item)
    return "|".join(merged)


def _int_or_default(value: str, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _max_decimal_text(left: str, right: str) -> str:
    values = []
    for value in (left, right):
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            pass
    if not values:
        return left or right
    return str(max(values))


def _availability(
    rows: Iterable[Mapping[str, str]],
    worker_lookup: Mapping[str, str],
    issues: list[AdapterIssue],
) -> list[dict[str, str]]:
    output = []
    for row_number, row in enumerate(rows, start=2):
        worker_name = _value(
            row,
            "worker_id",
            "staff_id",
            "sw_id",
            "support_worker_id",
            "staff_name",
            "support_worker",
            "worker",
            "name",
        )
        worker_id = _resolve_reference_text(worker_name, worker_lookup, issues, "availability", row_number, "worker")
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
                "source_ref": _value(row, "source_ref") or _source_ref("availability", row_number),
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
        worker_id = _resolve_name(_value(row, "worker_id", "staff_id", "sw_id", "support_worker_id", "staff_name", "support_worker", "worker"), worker_lookup, issues, "compatibility", row_number, "worker")
        service_user_id = _resolve_name(_value(row, "service_user_id", "child_id", "su_id", "child_name", "service_user", "service_user_name", "child"), service_user_lookup, issues, "compatibility", row_number, "service_user")
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
    *,
    worker_rows: Sequence[Mapping[str, str]] = (),
    service_user_rows: Sequence[Mapping[str, str]] = (),
    llm_resolver: AnthropicAdHocResolver | None = None,
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    # Rows eligible for LLM gap-filling (ad-hoc-workbook-sourced only, see the
    # "_source_kind" tag applied in _read_source_tables) that the deterministic
    # pass either dropped (unresolved service user) or left with no schedule
    # info at all. Keyed by row_number so results can be merged back precisely.
    gap_rows: dict[int, dict[str, object]] = {}

    for row_number, row in enumerate(rows, start=2):
        client_text = _value(
            row,
            "service_user_id", "child_id", "su_id", "child_name", "client_name",
            "service_user", "service_user_name", "child", "name",
        )
        service_user_ids = _resolve_service_user_references(
            client_text, service_user_lookup, issues, "family_requests", row_number, "service_user",
        )
        requested = _resolve_many(_value(row, "requested_worker_ids", "requested_staff", "preferred_worker", "staff", "support_worker", "worker", "sw_requested", "specific_sw"), worker_lookup, issues, "family_requests", row_number, "requested_worker_ids")
        avoided = _resolve_many(_value(row, "avoided_worker_ids", "avoided_staff", "avoid_staff", "avoid_worker"), worker_lookup, issues, "family_requests", row_number, "avoided_worker_ids")

        is_llm_eligible = llm_resolver is not None and row.get("_source_kind") == "ad_hoc_workbook"

        if not service_user_ids:
            if is_llm_eligible:
                gap_rows[row_number] = {"row": row, "client_text": client_text, "requested": requested, "avoided": avoided, "dropped": True}
            continue

        base_request_id = _value(row, "family_request_id", "request_id")
        preferred_days = _preferred_request_days(row)
        preferred_time_ranges = _family_request_time_ranges(row, issues, row_number)
        notes = _value(row, "notes", "request", "sessions_notes", "notes_for_july")
        if is_llm_eligible and not preferred_days and not preferred_time_ranges:
            gap_rows[row_number] = {
                "row": row, "client_text": client_text, "requested": requested, "avoided": avoided,
                "dropped": False, "service_user_ids": service_user_ids, "base_request_id": base_request_id, "notes": notes,
            }
        for index, service_user_id in enumerate(service_user_ids):
            request_id = base_request_id or _stable_id("request", f"{service_user_id}-{row_number}")
            if len(service_user_ids) > 1:
                request_id = f"{request_id}-{index + 1}"
            output.append(
                {
                    "family_request_id": request_id,
                    "service_user_id": service_user_id,
                    "requested_worker_ids": "|".join(requested),
                    "avoided_worker_ids": "|".join(avoided),
                    "preferred_days": preferred_days,
                    "preferred_time_ranges": "|".join(preferred_time_ranges),
                    "unavailable_time_ranges": _pipe(_value(row, "unavailable_time_ranges")),
                    "notes": notes,
                    "source_ref": _source_ref("family_requests", row_number),
                }
            )

    if llm_resolver is not None and gap_rows:
        _apply_llm_gap_filling(output, gap_rows, worker_rows, service_user_rows, llm_resolver, issues)

    return output


def _apply_llm_gap_filling(
    output: list[dict[str, str]],
    gap_rows: Mapping[int, Mapping[str, object]],
    worker_rows: Sequence[Mapping[str, str]],
    service_user_rows: Sequence[Mapping[str, str]],
    llm_resolver: AnthropicAdHocResolver,
    issues: list[AdapterIssue],
) -> None:
    llm_input = [
        {
            "source_ref": f"row-{row_number}",
            "client_name": gap["client_text"],
            "raw_notes": _row_raw_notes_text(gap["row"]),
        }
        for row_number, gap in gap_rows.items()
    ]
    try:
        results = llm_resolver.resolve_gaps(llm_input, worker_rows, service_user_rows)
    except LlmCallLimitExceeded as error:
        _add_issue(issues, "warning", "llm_call_limit_exceeded", str(error), "family_requests")
        return
    except Exception as error:  # noqa: BLE001 -- an external API call; never fail the whole adapter run for it
        _add_issue(issues, "warning", "llm_ad_hoc_parsing_failed", f"LLM-assisted parsing failed: {error}", "family_requests")
        return

    results_by_row = {int(result.source_ref.removeprefix("row-")): result for result in results}
    min_confidence = llm_resolver.config.min_confidence_to_auto_apply
    for row_number, gap in gap_rows.items():
        result = results_by_row.get(row_number)
        if result is None:
            continue
        _merge_llm_result(output, row_number, gap, result, min_confidence, issues)


def _row_raw_notes_text(row: Mapping[str, str]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in row.items() if key != "_source_kind" and value)


def _merge_llm_result(
    output: list[dict[str, str]],
    row_number: int,
    gap: Mapping[str, object],
    result: GapRowResult,
    min_confidence: str,
    issues: list[AdapterIssue],
) -> None:
    high_confidence_requested = [w.resolved_id for w in result.requested_workers if w.resolved_id and meets_confidence(w.confidence, min_confidence)]
    high_confidence_avoided = [w.resolved_id for w in result.avoided_workers if w.resolved_id and meets_confidence(w.confidence, min_confidence)]
    for person in (*result.requested_workers, *result.avoided_workers):
        if person.resolved_id and not meets_confidence(person.confidence, min_confidence):
            _add_issue(
                issues, "warning", "llm_suggested_name_match",
                f"LLM suggested {person.name_as_written} -> {person.resolved_id} at {person.confidence} confidence; not applied automatically",
                "family_requests", row_number, "requested_worker_ids",
            )
        elif person.resolved_id is None and person.name_as_written:
            _add_issue(
                issues, "warning", "llm_suggested_name_match",
                f"LLM could not confidently resolve name: {person.name_as_written}",
                "family_requests", row_number, "requested_worker_ids",
            )
    if result.note:
        _add_issue(issues, "warning", "llm_flagged_ambiguity", result.note, "family_requests", row_number)

    # Preserve the LLM's priority order (most preferred first) -- do not sort
    # numerically, that would discard "Sunday preferred, Saturday fallback".
    # preferred_days/target_sessions_per_week are the flattened, backward-compatible
    # view for display; preferred_day_patterns (see docs/NORMALIZED_DATA_CONTRACT.md)
    # is the source of truth a planner should use, since it keeps independent day
    # groups (e.g. "1 midweek + 1 weekend") separate instead of merging them into one
    # list a flat count can't correctly consume.
    flattened_days: list[int] = []
    for pattern in result.day_patterns:
        flattened_days.extend(pattern.days)
    preferred_days = "|".join(str(day) for day in dict.fromkeys(flattened_days))
    total_sessions_per_week = sum(pattern.sessions_per_week for pattern in result.day_patterns)
    preferred_day_patterns = "|".join(
        f"{','.join(str(day) for day in dict.fromkeys(pattern.days))}:{pattern.sessions_per_week}"
        for pattern in result.day_patterns
    )
    preferred_time_ranges = "|".join(result.exact_date_intervals)
    notes_parts = [value for value in (gap.get("notes"), result.notes_addition) if value]
    notes = " | ".join(dict.fromkeys(notes_parts))
    target_sessions_per_week = str(total_sessions_per_week) if total_sessions_per_week else ""
    target_session_duration_minutes = (
        str(result.target_session_duration_minutes) if result.target_session_duration_minutes else ""
    )
    preferred_time_window_start = result.preferred_time_window_start or ""
    preferred_time_window_end = result.preferred_time_window_end or ""

    if gap["dropped"]:
        if not result.resolved_service_user_id:
            return  # still unresolved -- the original unresolved_name warning stands
        request_id = _stable_id("request", f"{result.resolved_service_user_id}-{row_number}")
        output.append(
            {
                "family_request_id": request_id,
                "service_user_id": result.resolved_service_user_id,
                "requested_worker_ids": "|".join(dict.fromkeys([*gap["requested"], *high_confidence_requested])),
                "avoided_worker_ids": "|".join(dict.fromkeys([*gap["avoided"], *high_confidence_avoided])),
                "preferred_days": preferred_days,
                "preferred_time_ranges": preferred_time_ranges,
                "unavailable_time_ranges": "",
                "notes": notes,
                "target_sessions_per_week": target_sessions_per_week,
                "target_session_duration_minutes": target_session_duration_minutes,
                "preferred_time_window_start": preferred_time_window_start,
                "preferred_time_window_end": preferred_time_window_end,
                "preferred_day_patterns": preferred_day_patterns,
                "source_ref": _source_ref("family_requests", row_number),
            }
        )
        _add_issue(
            issues, "info", "llm_recovered_family_request",
            f"LLM resolved previously-unresolved service user to {result.resolved_service_user_id}; row recovered",
            "family_requests", row_number, "service_user",
        )
        return

    for row in output:
        if row["source_ref"] == _source_ref("family_requests", row_number):
            if not row["preferred_days"]:
                row["preferred_days"] = preferred_days
            if not row["preferred_time_ranges"]:
                row["preferred_time_ranges"] = preferred_time_ranges
            row["notes"] = " | ".join(dict.fromkeys(part for part in (row["notes"], result.notes_addition) if part))
            if high_confidence_requested:
                row["requested_worker_ids"] = "|".join(dict.fromkeys([*_split(row["requested_worker_ids"]), *high_confidence_requested]))
            if high_confidence_avoided:
                row["avoided_worker_ids"] = "|".join(dict.fromkeys([*_split(row["avoided_worker_ids"]), *high_confidence_avoided]))
            if target_sessions_per_week:
                row["target_sessions_per_week"] = target_sessions_per_week
            if target_session_duration_minutes:
                row["target_session_duration_minutes"] = target_session_duration_minutes
            if preferred_time_window_start:
                row["preferred_time_window_start"] = preferred_time_window_start
            if preferred_time_window_end:
                row["preferred_time_window_end"] = preferred_time_window_end
            if preferred_day_patterns:
                row["preferred_day_patterns"] = preferred_day_patterns
    _add_issue(
        issues, "info", "llm_assisted_family_request",
        "LLM-assisted parsing filled in day/time details the deterministic parser could not extract",
        "family_requests", row_number,
    )


def _read_csv(path: Path) -> tuple[dict[str, str], ...]:
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        return tuple({_normalize_header(key): (value or "").strip() for key, value in row.items() if key} for row in reader)


def _assigned_workers(
    row: Mapping[str, str],
    worker_lookup: Mapping[str, str],
    issues: list[AdapterIssue],
    row_number: int,
) -> list[str]:
    values = [
        (
            _value(row, "assigned_worker_ids", "staff", "worker", "support_worker"),
            "",
        )
    ] + list(_session_worker_value_notes(row))
    resolved: list[str] = []
    for value, note in values:
        for worker_id in _resolve_many_candidates((value, note), worker_lookup, issues, "sessions", row_number, "assigned_worker_ids"):
            if worker_id not in resolved:
                resolved.append(worker_id)
    return resolved


def _required_worker_count(row: Mapping[str, str], assigned_ids: list[str]) -> int:
    explicit = _value(row, "required_worker_count", "workers_required", "staff_required")
    if explicit:
        try:
            return max(1, int(float(explicit)))
        except ValueError:
            return 1
    if _truthy(_value(row, "2to1_required", "2_1_required", "two_to_one_required")):
        return 2
    return max(1, len(assigned_ids)) if assigned_ids else 1


def _preferred_request_days(row: Mapping[str, str]) -> str:
    explicit = _pipe(_value(row, "preferred_days"))
    if explicit:
        return explicit
    ranges = _family_request_time_ranges(row, [], 0)
    days = []
    for interval in ranges:
        start = interval.split("/", 1)[0]
        weekday = str(_date_from_datetime_text(start).weekday())
        if weekday not in days:
            days.append(weekday)
    return "|".join(days)


def _family_request_time_ranges(
    row: Mapping[str, str],
    issues: list[AdapterIssue],
    row_number: int,
) -> list[str]:
    explicit = _pipe(_value(row, "preferred_time_ranges", "requested_windows"))
    intervals = explicit.split("|") if explicit else []
    if not intervals:
        start = _datetime_text(row, "start", "session_start", "available_from")
        end = _datetime_text(row, "end", "session_end", "available_to")
        if start and end:
            intervals = [f"{start}/{end}"]
    if not intervals:
        interval = _specific_day_time_interval(row, issues, row_number)
        if interval:
            intervals = [interval]
    if not intervals:
        return []
    frequency = _ad_hoc_recurrence_frequency(row)
    if frequency is None:
        return intervals
    step = _recurrence_step(frequency)
    if step is None:
        if issues:
            _add_issue(
                issues,
                "warning",
                "unsupported_ad_hoc_recurrence",
                f"unsupported ad hoc recurrence {frequency}; only the supplied request window was imported",
                "family_requests",
                row_number,
                "recurring",
            )
        return intervals
    expanded: list[str] = []
    for interval in intervals:
        if "/" not in interval:
            expanded.append(interval)
            continue
        start, end = interval.split("/", 1)
        expanded.extend(
            f"{item_start}/{item_end}"
            for item_start, item_end in _expand_interval(start, end, step, _request_recurrence_until(row, start))
        )
    return list(dict.fromkeys(expanded))


def _specific_day_time_interval(
    row: Mapping[str, str],
    issues: list[AdapterIssue],
    row_number: int,
) -> str:
    day_text = _value(row, "specific_day", "date", "session_date", "booking_date")
    time_text = _value(row, "specific_time", "time", "session_time", "requested_time")
    if not day_text or not time_text:
        return ""
    time_range = _parse_time_range_text(time_text)
    if time_range is None:
        _add_issue(
            issues,
            "warning",
            "unparsed_specific_time",
            f"could not parse specific_time value: {time_text}",
            "family_requests",
            row_number,
            "specific_time",
        )
        return ""
    start_time, end_time = time_range
    try:
        day = _coerce_date(day_text)
    except ValueError:
        if issues:
            _add_issue(
                issues,
                "warning",
                "unparsed_specific_day",
                f"could not parse specific_day value: {day_text}",
                "family_requests",
                row_number,
                "specific_day",
            )
        return ""
    return f"{day}T{start_time}+01:00/{day}T{end_time}+01:00"


def _parse_time_range_text(value: str) -> tuple[str, str] | None:
    text = value.strip().lower()
    text = text.replace("–", "-").replace("—", "-").replace(" to ", "-")
    match = re.search(
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*-\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
        text,
    )
    if not match:
        return None
    start = _coerce_clock_text(match.group(1), match.group(2))
    end = _coerce_clock_text(match.group(2), match.group(1))
    if start >= end:
        return None
    return start, end


def _coerce_clock_text(value: str, other: str = "") -> str:
    text = value.strip().lower().replace(" ", "")
    meridiem = ""
    if text.endswith(("am", "pm")):
        meridiem = text[-2:]
        text = text[:-2]
    else:
        other_text = other.strip().lower().replace(" ", "")
        if other_text.endswith(("am", "pm")):
            meridiem = other_text[-2:]
    if ":" in text:
        hour_text, minute_text = text.split(":", 1)
    else:
        hour_text, minute_text = text, "00"
    hour = int(hour_text)
    minute = int(minute_text[:2])
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}:00"


def _ad_hoc_recurrence_frequency(row: Mapping[str, str]) -> str | None:
    return _recurrence_frequency(_value(row, "recurring", "recurrence", "repeat", "frequency"))


def _recurring_session_datetimes(
    row: Mapping[str, str],
    start: str,
    end: str,
    issues: list[AdapterIssue],
    row_number: int,
) -> list[tuple[str, str]]:
    frequency = _session_recurrence_frequency(row)
    if frequency is None:
        return [(start, end)]
    if frequency != "weekly":
        _add_issue(
            issues,
            "warning",
            "unsupported_session_recurrence",
            f"unsupported session recurrence {frequency}; only the first occurrence was imported",
            "sessions",
            row_number,
            "recurring",
        )
        return [(start, end)]
    return _expand_interval(start, end, timedelta(days=7), _month_end(_date_from_datetime_text(start)))


def _session_recurrence_frequency(row: Mapping[str, str]) -> str | None:
    value = _value(row, "recurring", "recurrence", "repeat")
    if value:
        return _recurrence_frequency(value)
    if _value(row, "booking_date") and not _value(row, "session_id"):
        return "weekly"
    return None


def _recurrence_frequency(value: str) -> str | None:
    text = _norm_name(value)
    if not text or text in {"n", "no", "false", "0", "once", "one off", "single", "non recurring"}:
        return None
    if text in {"y", "yes", "true", "1", "weekly", "week", "every week", "recurring"}:
        return "weekly"
    if text in {"fortnightly", "biweekly", "bi weekly", "every other week", "every 2 weeks"}:
        return "fortnightly"
    if text in {"daily", "every day"}:
        return "daily"
    return text


def _recurrence_step(frequency: str) -> timedelta | None:
    if frequency == "weekly":
        return timedelta(days=7)
    if frequency == "fortnightly":
        return timedelta(days=14)
    if frequency == "daily":
        return timedelta(days=1)
    return None


def _request_recurrence_until(row: Mapping[str, str], start: str) -> date:
    explicit = _value(row, "recurrence_until", "until", "end_date", "repeat_until")
    if explicit:
        return _parse_date_text(explicit)
    return _month_end(_date_from_datetime_text(start))


def _occurrence_id(base_session_id: str, occurrence_start: str, index: int, total: int) -> str:
    if total == 1:
        return base_session_id
    day = _date_from_datetime_text(occurrence_start).strftime("%Y%m%d")
    return f"{base_session_id}-{day}" if index > 0 else base_session_id


def _expand_interval(start: str, end: str, step: timedelta, until: date) -> list[tuple[str, str]]:
    start_dt = _parse_datetime_text(start)
    end_dt = _parse_datetime_text(end)
    occurrences = []
    for _ in range(366):
        if start_dt.date() > until:
            break
        occurrences.append((start_dt.isoformat(), end_dt.isoformat()))
        start_dt += step
        end_dt += step
    return occurrences


def _date_from_datetime_text(value: str) -> date:
    return _parse_datetime_text(value).date()


def _month_end(value: date) -> date:
    if value.month == 12:
        return date(value.year, 12, 31)
    return date(value.year, value.month + 1, 1) - timedelta(days=1)


def _read_request_files(source_dir: Path, issues: list[AdapterIssue]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in REQUEST_FILE_EXTENSIONS:
            continue
        if "date request" not in _norm_name(str(path.parent)) and "date request" not in _norm_name(path.name):
            continue
        rows.append(_request_file_row(path, issues))
    return rows


def _request_file_row(path: Path, issues: list[AdapterIssue]) -> dict[str, str]:
    text = _request_text(path, issues)
    child_name = _request_child_name(path, text)
    source = str(path)
    if path.suffix.lower() == ".msg":
        _add_issue(
            issues,
            "warning",
            "manual_request_file_review",
            "Outlook .msg date request was discovered but needs manual text extraction or an exported .eml/.docx copy",
            source,
        )
    if text and not _request_intervals(text):
        _add_issue(
            issues,
            "warning",
            "low_confidence_request_parse",
            "date request text was captured but no ISO-style date/time interval was parsed",
            source,
        )
    return {
        "request_id": _stable_id("request", str(path.relative_to(path.parents[0]))),
        "child_name": child_name,
        "requested_staff": _requested_staff_text(text),
        "avoided_staff": _avoided_staff_text(text),
        "preferred_days": _preferred_days_text(text),
        "preferred_time_ranges": "|".join(_request_intervals(text)),
        "unavailable_time_ranges": "",
        "notes": text.strip() if text else "date request file requires manual review",
        "source_ref": source,
    }


def _read_availability_report_pdfs(
    source_dir: Path,
    issues: list[AdapterIssue],
    *,
    target_month: tuple[int, int] | None = None,
) -> list[dict[str, str]]:
    """Parse availability report PDFs found under ``source_dir``.

    A tenant's ``availability report/`` archive typically holds one PDF per
    month, but nothing stops every month's report from being discovered in
    the same run (they all match "availability" in their path). Without
    scoping, rows for staff who simply aren't on the *current* roster --
    because the report is for a different month than the one being imported
    -- generate an unresolved-name warning per row, which can dwarf the
    genuine warning count for the actual target month. When ``target_month``
    is given (as an inferred ``(year, month)`` from the parsed session
    dates), rows outside it are excluded before name resolution ever runs,
    so they cost nothing but one summary line per skipped PDF.
    """
    rows: list[dict[str, str]] = []
    for path in sorted(source_dir.rglob("*.pdf")):
        if "availability" not in _norm_name(str(path)):
            continue
        text = _pdf_text(path, issues)
        if not text:
            _add_issue(
                issues,
                "warning",
                "availability_report_pdf_requires_review",
                "availability report PDF discovered but could not be read; export it to availability.csv or an availability workbook before standardized import",
                str(path),
            )
            continue
        parsed_rows = _availability_report_rows_from_text(text, path, issues)
        if not parsed_rows:
            _add_issue(
                issues,
                "warning",
                "availability_report_pdf_requires_review",
                "availability report PDF discovered but no availability rows were parsed; export it to availability.csv or an availability workbook before standardized import",
                str(path),
            )
            continue
        in_scope_rows = (
            [row for row in parsed_rows if _row_year_month(row) == target_month]
            if target_month is not None
            else parsed_rows
        )
        if not in_scope_rows:
            _add_issue(
                issues,
                "info",
                "availability_report_pdf_out_of_scope_month",
                (
                    f"availability report PDF parsed {len(parsed_rows)} rows, all outside the "
                    f"target import month {target_month[0]:04d}-{target_month[1]:02d}; excluded"
                ),
                str(path),
            )
            continue
        rows.extend(in_scope_rows)
        scope_note = (
            f" ({len(in_scope_rows)} of {len(parsed_rows)}, scoped to target month)"
            if len(in_scope_rows) != len(parsed_rows)
            else ""
        )
        _add_issue(
            issues,
            "info",
            "availability_report_pdf_parsed",
            f"availability report PDF parsed into {len(in_scope_rows)} availability windows{scope_note}",
            str(path),
        )
    return rows


def _row_year_month(row: Mapping[str, str]) -> tuple[int, int] | None:
    start = row.get("start", "")
    if len(start) < 7 or start[4] != "-":
        return None
    try:
        return int(start[:4]), int(start[5:7])
    except ValueError:
        return None


def _target_month_from_sessions(session_rows: Iterable[Mapping[str, str]]) -> tuple[int, int] | None:
    months: Counter[tuple[int, int]] = Counter()
    for row in session_rows:
        year_month = _row_year_month(row)
        if year_month is not None:
            months[year_month] += 1
    if not months:
        return None
    return months.most_common(1)[0][0]


def _pdf_text(path: Path, issues: list[AdapterIssue]) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        _add_issue(
            issues,
            "warning",
            "availability_pdf_parser_unavailable",
            "pypdf is not installed, so availability report PDFs cannot be parsed automatically",
            str(path),
        )
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as error:  # pragma: no cover - pypdf can raise several parser-specific errors.
        _add_issue(issues, "warning", "unreadable_availability_pdf", f"could not read availability report PDF: {error}", str(path))
        return ""


def _availability_report_rows_from_text(
    text: str,
    path: Path,
    issues: list[AdapterIssue],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    row_number = 0
    index = 0
    while index < len(lines) - 2:
        parsed_date = _availability_report_date_at(lines, index)
        if not parsed_date:
            index += 1
            continue

        day, index = parsed_date
        fragments: list[str] = []
        while index < len(lines):
            if _availability_report_date_at(lines, index):
                break
            fragments.append(lines[index])
            if len(_availability_report_flags(" ".join(fragments))) >= 2:
                break
            index += 1

        row_number += 1
        rows.extend(_availability_windows_from_report_row(day, " ".join(fragments), path, row_number, issues))
    return rows


def _availability_report_date_at(lines: list[str], index: int) -> tuple[date, int] | None:
    if index + 1 >= len(lines):
        return None
    date_match = re.fullmatch(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*(\d{1,2})(?:\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec))?",
        lines[index],
    )
    if not date_match:
        return None
    month = date_match.group(2)
    if month:
        year_match = re.fullmatch(r"(\d{4})", lines[index + 1])
        if not year_match:
            return None
        day_text = f"{int(date_match.group(1)):02d} {month} {year_match.group(1)}"
    else:
        month_match = re.fullmatch(
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})",
            lines[index + 1],
        )
        if not month_match:
            return None
        day_text = f"{int(date_match.group(1)):02d} {month_match.group(1)} {month_match.group(2)}"
    return datetime.strptime(day_text, "%d %b %Y").date(), index + 2


def _availability_windows_from_report_row(
    day: date,
    row_text: str,
    path: Path,
    row_number: int,
    issues: list[AdapterIssue],
) -> list[dict[str, str]]:
    normalized = _space_availability_report_flags(row_text)
    # Column order, confirmed against the source PDF's own table structure (pdfplumber
    # extract_tables, not just word position): Day | Support Worker | Unavailable |
    # All day (8am - 11pm) | Free from...To... | Free from...To.... So the first YES/NO
    # flag is "Unavailable" (a hard "do not book me at all today" signal) and the second
    # is "Available all day (8am-11pm)" -- these are NOT "all day available" then
    # "unavailable all day" as the group names might suggest at a glance.
    match = re.match(r"(?P<name>.+?)\s+(?P<unavailable>YES|NO)\s+(?P<available_all_day>YES|NO)\s*(?P<rest>.*)$", normalized)
    if not match:
        _add_issue(
            issues,
            "warning",
            "unparsed_availability_report_row",
            "availability report row could not be parsed and was skipped",
            str(path),
            row_number,
        )
        return []

    worker_name = _clean_availability_report_name(match.group("name"))
    if not worker_name:
        return []
    if _is_recruitment_pipeline_name(worker_name):
        return []

    times = re.findall(r"\b\d{1,2}:\d{2}\b", match.group("rest"))
    if len(times) % 2:
        _add_issue(
            issues,
            "warning",
            "unpaired_availability_report_time",
            "availability report row has an odd number of time values; trailing time was ignored",
            str(path),
            row_number,
        )

    if match.group("unavailable") == "YES":
        if times:
            _add_issue(
                issues,
                "warning",
                "contradictory_availability_report_row",
                "availability report row is marked Unavailable but also gives specific free times; "
                "treated as unavailable for the whole day",
                str(path),
                row_number,
            )
        return []

    if times:
        if match.group("available_all_day") == "YES":
            _add_issue(
                issues,
                "info",
                "availability_report_specific_times_override_all_day",
                "availability report row is marked available all day but also gives narrower specific "
                "times; the narrower times were used",
                str(path),
                row_number,
            )
        pairs = [(_coerce_report_time(times[index]), _coerce_report_time(times[index + 1])) for index in range(0, len(times) - 1, 2)]
    elif match.group("available_all_day") == "YES":
        pairs = [(AVAILABILITY_REPORT_ALL_DAY_START, AVAILABILITY_REPORT_ALL_DAY_END)]
    else:
        _add_issue(
            issues,
            "warning",
            "empty_availability_report_row",
            "availability report row is not marked unavailable or available all day, and gives no "
            "specific free times; no availability window could be determined",
            str(path),
            row_number,
        )
        return []

    output = []
    for window_index, (start_time, end_time) in enumerate(pairs, start=1):
        if start_time >= end_time:
            _add_issue(
                issues,
                "warning",
                "invalid_availability_report_window",
                "availability report row has an end time that is not after the start time; window was skipped",
                str(path),
                row_number,
            )
            continue
        start = f"{day.isoformat()}T{start_time}+01:00"
        end = f"{day.isoformat()}T{end_time}+01:00"
        output.append(
            {
                "availability_id": _availability_report_id(path, row_number, worker_name, start, end, window_index),
                "staff_name": worker_name,
                "start": start,
                "end": end,
                "source_ref": f"{path}:availability-report-row-{row_number}",
            }
        )
    return output


def _availability_report_flags(value: str) -> list[str]:
    return re.findall(r"(?<![A-Z])(YES|NO)(?![A-Z])", value)


def _space_availability_report_flags(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"(?<![A-Z])(YES|NO)(?![A-Z])", r" \1 ", value)).strip()


def _clean_availability_report_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("_", " ")).strip(" -")


def _is_recruitment_pipeline_name(value: str) -> bool:
    return re.search(r"\(\s*R\s*\)", value, flags=re.IGNORECASE) is not None


def _coerce_report_time(value: str) -> str:
    hour, minute = value.split(":", 1)
    return f"{int(hour):02d}:{int(minute):02d}:00"


def _availability_report_id(
    path: Path,
    row_number: int,
    worker_name: str,
    start: str,
    end: str,
    window_index: int,
) -> str:
    key = f"{path}:{row_number}:{worker_name}:{start}:{end}:{window_index}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"availability-report-{digest}"


def _request_text(path: Path, issues: list[AdapterIssue]) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix == ".eml":
            return _eml_text(path)
        if suffix == ".docx":
            return _docx_text(path)
        if suffix == ".txt":
            return path.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        _add_issue(issues, "warning", "unreadable_request_file", f"could not read date request file: {error}", str(path))
    return ""


def _eml_text(path: Path) -> str:
    message = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    parts = [message.get("subject", "")]
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                parts.append(part.get_content())
    elif message.get_content_type() == "text/plain":
        parts.append(message.get_content())
    return "\n".join(part for part in parts if part).strip()


def _docx_text(path: Path) -> str:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    return "\n".join(text.text or "" for text in root.findall(".//w:t", ns)).strip()


def _request_child_name(path: Path, text: str) -> str:
    for pattern in (
        r"(?:child|service user|su)\s*[:\-]\s*([A-Za-z][A-Za-z .'-]{1,80})",
        r"(?:date requests?|dates?)\s+(?:for\s+)?([A-Za-z][A-Za-z .'-]{1,80})",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_name_fragment(match.group(1))
    stem = re.sub(r"\b(date|dates|requests?|away|july|with|without|sw)\b", " ", path.stem, flags=re.IGNORECASE)
    return _clean_name_fragment(stem)


def _clean_name_fragment(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z .'-]+", " ", value)).strip(" -")


def _requested_staff_text(text: str) -> str:
    return _line_value(text, ("requested staff", "preferred worker", "support worker", "staff"))


def _avoided_staff_text(text: str) -> str:
    return _line_value(text, ("avoid staff", "avoided staff", "avoid worker", "avoided worker"))


def _line_value(text: str, labels: tuple[str, ...]) -> str:
    for line in text.splitlines():
        normalized = _norm_name(line)
        for label in labels:
            if normalized.startswith(_norm_name(label)):
                return re.sub(r"^[^:;-]+[:;-]\s*", "", line).strip()
    return ""


def _preferred_days_text(text: str) -> str:
    day_map = {
        "monday": "0",
        "tuesday": "1",
        "wednesday": "2",
        "thursday": "3",
        "friday": "4",
        "saturday": "5",
        "sunday": "6",
    }
    found = []
    lowered = text.lower()
    for name, value in day_map.items():
        if re.search(rf"\b{name}\b", lowered) and value not in found:
            found.append(value)
    return "|".join(found)


def _request_intervals(text: str) -> list[str]:
    intervals = []
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)?)"
        r"\s*(?:/|to|-)\s*"
        r"(\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)?)"
    )
    for start, end in pattern.findall(text):
        if ":" not in start or ":" not in end:
            continue
        intervals.append(f"{_coerce_datetime(start)}/{_coerce_datetime(end)}")
    return intervals


def _read_workbook_tables(path: Path, issues: list[AdapterIssue]) -> dict[str, tuple[dict[str, str], ...]]:
    tables: dict[str, tuple[dict[str, str], ...]] = {}
    try:
        sheets = _xlsx_sheets(path)
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        _add_issue(issues, "error", "invalid_workbook", f"could not read workbook structure: {error}", str(path))
        return tables
    is_booking_workbook = "Bookings" in sheets or "import sheet" in _norm_name(path.name)
    if is_booking_workbook:
        for sheet_name in BOOKING_SHEETS:
            if sheet_name not in sheets:
                _add_issue(issues, "warning", "missing_workbook_sheet", f"workbook sheet is missing: {sheet_name}", str(path))
    for sheet_name, rows in sheets.items():
        target = _workbook_sheet_target(sheet_name)
        if target is None:
            continue
        normalized_rows = rows
        if target == "service_users" and _looks_like_headerless_service_user_sheet(rows):
            normalized_rows = tuple(_headerless_service_user_rows(rows))
        if not normalized_rows:
            _add_issue(issues, "warning", "missing_workbook_sheet", f"workbook sheet is missing: {sheet_name}", str(path))
            continue
        tables[target] = tuple(tables.get(target, ())) + tuple(normalized_rows)
    return tables


def _workbook_sheet_target(sheet_name: str) -> str | None:
    if sheet_name in BOOKING_SHEETS:
        return BOOKING_SHEETS[sheet_name]
    normalized = _norm_name(sheet_name)
    if normalized in {"staff", "support workers", "support worker", "workers"}:
        return "workers"
    if normalized in {"su", "service users", "service user", "children", "childs"}:
        return "service_users"
    if normalized in {"bookings", "booking", "import sheet"}:
        return "sessions"
    if normalized in {"availability", "availability report"}:
        return "availability"
    if normalized in {"matching", "matches", "compatibility"}:
        return "compatibility"
    if normalized in {"ad hocs", "ad hoc", "date requests", "date request", "phase 1", "phase 2"}:
        return "family_requests"
    return None


def _looks_like_headerless_service_user_sheet(rows: tuple[dict[str, str], ...]) -> bool:
    if not rows:
        return False
    known = {
        "name",
        "child_name",
        "client_name",
        "service_user",
        "service_user_name",
        "postcode",
        "home_postcode",
        "child_postcode",
        "su_postcode",
    }
    first_keys = set(rows[0])
    return not first_keys.intersection(known) and any(_postcode(key) for key in first_keys)


def _headerless_service_user_rows(rows: tuple[dict[str, str], ...]) -> list[dict[str, str]]:
    reconstructed: list[list[str]] = [list(rows[0].keys())]
    reconstructed.extend(list(row.values()) for row in rows)
    normalized = []
    for values in reconstructed:
        values = [value.replace("_", " ").strip() for value in values if value.strip()]
        if not values:
            continue
        postcode = next((_postcode(value) for value in values if _postcode(value)), "")
        gender = next((_gender_text(value) for value in values if _norm_name(value) in {"male", "female"}), "")
        name_parts = []
        for value in values:
            if _postcode(value) or _truthy_2_to_1(value) or _norm_name(value) in {"male", "female"}:
                continue
            name_parts.append(value)
            if len(name_parts) == 2:
                break
        name = " ".join(name_parts).strip().title()
        if not name:
            continue
        normalized.append(
            {
                "name": name,
                "postcode": postcode,
                "gender": gender,
                "required_worker_count": "2" if any(_truthy_2_to_1(value) for value in values) else "1",
                "support_level": "2_to_1" if any(_truthy_2_to_1(value) for value in values) else "standard",
            }
        )
    return normalized


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
            rows = _worksheet_rows(archive.read(worksheet_path), shared_strings, ns)
            comments = _worksheet_comments(archive, worksheet_path, ns)
            sheets[name] = _worksheet_rows_to_dicts(rows, comments)
        return sheets


def _shared_strings(archive: zipfile.ZipFile, ns: Mapping[str, str]) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    values = []
    for item in root.findall("main:si", ns):
        values.append("".join(text.text or "" for text in item.findall(".//main:t", ns)))
    return values


def _worksheet_rows(xml_bytes: bytes, shared_strings: list[str], ns: Mapping[str, str]) -> list[tuple[int, dict[int, tuple[str, str]]]]:
    root = ElementTree.fromstring(xml_bytes)
    rows = []
    for row in root.findall("main:sheetData/main:row", ns):
        values: dict[int, tuple[str, str]] = {}
        for cell in row.findall("main:c", ns):
            cell_ref = cell.attrib.get("r", "")
            index = _column_index(re.sub(r"\d+", "", cell_ref)) if cell_ref else len(values)
            values[index] = (cell_ref, _cell_value(cell, shared_strings, ns))
        row_number = int(row.attrib.get("r", len(rows) + 1))
        rows.append((row_number, values))
    return rows


def _worksheet_comments(
    archive: zipfile.ZipFile,
    worksheet_path: str,
    ns: Mapping[str, str],
) -> dict[str, str]:
    rels_path = posixpath.join(
        posixpath.dirname(worksheet_path),
        "_rels",
        posixpath.basename(worksheet_path) + ".rels",
    )
    if rels_path not in archive.namelist():
        return {}
    root = ElementTree.fromstring(archive.read(rels_path))
    comments_path = ""
    for rel in root.findall("pkg:Relationship", ns):
        if rel.attrib.get("Type", "").endswith("/comments"):
            comments_path = posixpath.normpath(posixpath.join(posixpath.dirname(worksheet_path), rel.attrib["Target"]))
            break
    if not comments_path or comments_path not in archive.namelist():
        return {}
    return _comments_from_xml(archive.read(comments_path), ns)


def _comments_from_xml(xml_bytes: bytes, ns: Mapping[str, str]) -> dict[str, str]:
    root = ElementTree.fromstring(xml_bytes)
    authors = [
        "".join(author.itertext()).strip()
        for author in root.findall(".//main:author", ns)
    ]
    comments = {}
    for comment in root.findall(".//main:comment", ns):
        ref = comment.attrib.get("ref", "")
        if not ref:
            continue
        author_id = int(comment.attrib.get("authorId", "0") or 0)
        author = authors[author_id] if author_id < len(authors) else ""
        text = " ".join("".join(item.text or "" for item in comment.findall(".//main:t", ns)).split())
        comments[ref] = _strip_comment_author(text, author)
    return comments


def _strip_comment_author(text: str, author: str) -> str:
    if author and text.lower().startswith(author.lower() + ":"):
        return text[len(author) + 1 :].strip()
    return text.strip()


def _cell_value(cell, shared_strings: list[str], ns: Mapping[str, str]) -> str:
    if cell.attrib.get("t") == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//main:t", ns)).strip()
    value = cell.find("main:v", ns)
    if value is None or value.text is None:
        return ""
    if cell.attrib.get("t") == "s":
        return shared_strings[int(value.text)].strip()
    return value.text.strip()


def _worksheet_rows_to_dicts(
    worksheet_rows: list[tuple[int, dict[int, tuple[str, str]]]],
    comments: Mapping[str, str],
) -> tuple[dict[str, str], ...]:
    while worksheet_rows and not any(value.strip() for _, value in worksheet_rows[0][1].values()):
        worksheet_rows.pop(0)
    if not worksheet_rows:
        return ()
    header_cells = worksheet_rows[0][1]
    max_index = max(header_cells) if header_cells else -1
    headers = [
        _normalize_header(header_cells.get(index, ("", ""))[1])
        for index in range(max_index + 1)
    ]
    rows = []
    for _, cells in worksheet_rows[1:]:
        if not any(value.strip() for _, value in cells.values()):
            continue
        row = {}
        for index, header in enumerate(headers):
            if not header:
                continue
            cell_ref, value = cells.get(index, ("", ""))
            row[header] = value.strip()
            if cell_ref and comments.get(cell_ref):
                row[f"{header}_note"] = comments[cell_ref]
        rows.append(row)
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
        for name_key in _name_lookup_keys(row[name_field]):
            name_counts[name_key] = name_counts.get(name_key, 0) + 1
    for row in cached_rows:
        row_id = row[id_field]
        lookup[_norm_name(row_id)] = row_id
        for name_key in _name_lookup_keys(row[name_field]):
            if name_counts[name_key] == 1:
                lookup[name_key] = row_id
    return lookup


def _name_lookup_keys(value: str) -> tuple[str, ...]:
    keys = [_norm_name(value)]
    without_parentheses = re.sub(r"\([^)]*\)", " ", value)
    keys.append(_norm_name(without_parentheses))
    keys.append(_norm_name(re.sub(r"\((?:P|NR|DRIVER)\)", " ", value, flags=re.IGNORECASE)))
    parenthetical_match = re.match(r"\s*(?P<prefix>.+?)\s+\([^)]+\)\s+(?P<rest>.+?)\s*$", value)
    if parenthetical_match:
        keys.append(_norm_name(parenthetical_match.group("prefix")))
        keys.append(_norm_name(f"{parenthetical_match.group('prefix')} {parenthetical_match.group('rest')}"))
    nickname_match = re.match(r"\s*(?P<first>[A-Za-z'-]+)\s+\((?P<nickname>[^)]+)\)\s+(?P<rest>.+?)\s*$", value)
    if nickname_match:
        nickname = nickname_match.group("nickname")
        rest = nickname_match.group("rest")
        keys.append(_norm_name(f"{nickname} {rest}"))
        keys.append(_norm_name(f"{nickname_match.group('first')} {rest}"))
    return tuple(dict.fromkeys(key for key in keys if key))


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


def _resolve_first(
    values: Iterable[str],
    lookup: Mapping[str, str],
    issues: list[AdapterIssue],
    source: str,
    row_number: int,
    field: str,
) -> str:
    unresolved = []
    for value in values:
        if not value:
            continue
        key = _norm_name(value)
        if key in lookup:
            return lookup[key]
        unresolved.append(value)
    if unresolved:
        _add_issue(
            issues,
            "warning",
            "unresolved_name",
            f"could not resolve {field}: {' / '.join(unresolved)}",
            source,
            row_number,
            field,
        )
    return ""


def _resolve_service_user_references(
    value: str,
    lookup: Mapping[str, str],
    issues: list[AdapterIssue],
    source: str,
    row_number: int,
    field: str,
) -> list[str]:
    resolved = _resolve_name(value, lookup, [], source, row_number, field)
    if resolved:
        return [resolved]

    parts = _split_service_user_reference(value)
    if len(parts) <= 1:
        _resolve_name(value, lookup, issues, source, row_number, field)
        return []

    resolved_parts = []
    for part in parts:
        part_id = _resolve_name(part, lookup, [], source, row_number, field)
        if not part_id:
            _resolve_name(value, lookup, issues, source, row_number, field)
            return []
        if part_id not in resolved_parts:
            resolved_parts.append(part_id)
    return resolved_parts


def _split_service_user_reference(value: str) -> list[str]:
    normalized = re.sub(r"\s+(?:and|&|\+)\s+", "|", value.strip(), flags=re.IGNORECASE)
    pieces = [piece.strip(" ,;/") for piece in normalized.split("|") if piece.strip(" ,;/")]
    if len(pieces) > 1:
        return pieces

    tokens = _norm_name(value).split()
    if len(tokens) == 4 and len(tokens[1]) == 1 and len(tokens[3]) == 1:
        return [f"{tokens[0]} {tokens[1]}", f"{tokens[2]} {tokens[3]}"]
    return [value.strip()] if value.strip() else []


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
        item_id = _resolve_reference_text(item, lookup, issues, source, row_number, field)
        if item_id and item_id not in resolved:
            resolved.append(item_id)
    return resolved


def _resolve_many_candidates(
    values: Iterable[str],
    lookup: Mapping[str, str],
    issues: list[AdapterIssue],
    source: str,
    row_number: int,
    field: str,
) -> list[str]:
    candidates = [item for value in values for item in _split(value)]
    resolved = []
    for item in candidates:
        item_id = _resolve_reference_text(item, lookup, issues, source, row_number, field, warn=False)
        if item_id and item_id not in resolved:
            resolved.append(item_id)
    if resolved:
        return resolved
    if candidates:
        _resolve_reference_text(candidates[-1], lookup, issues, source, row_number, field, warn=True)
    return []


def _resolve_reference_text(
    value: str,
    lookup: Mapping[str, str],
    issues: list[AdapterIssue],
    source: str,
    row_number: int,
    field: str,
    *,
    warn: bool = True,
) -> str:
    if not value:
        return ""
    key = _norm_name(value)
    if key in lookup:
        return lookup[key]

    matches = {
        resolved_id
        for lookup_key, resolved_id in lookup.items()
        if _lookup_key_in_reference(lookup_key, key)
    }
    if not matches:
        matches = _partial_reference_matches(key, lookup)
    if len(matches) == 1:
        return next(iter(matches))
    if len(matches) > 1:
        if warn:
            _add_issue(
                issues,
                "warning",
                "ambiguous_reference_text",
                f"could not choose one {field} from note text: {value}",
                source,
                row_number,
                field,
            )
        return ""

    if warn:
        _add_issue(issues, "warning", "unresolved_name", f"could not resolve {field}: {value}", source, row_number, field)
    return ""


def _lookup_key_in_reference(lookup_key: str, reference_key: str) -> bool:
    if len(lookup_key) < 4 or not reference_key:
        return False
    return re.search(rf"(^| ){re.escape(lookup_key)}($| )", reference_key) is not None


def _partial_reference_matches(reference_key: str, lookup: Mapping[str, str]) -> set[str]:
    reference_tokens = reference_key.split()
    if not reference_tokens:
        return set()
    matches = set()
    for lookup_key, resolved_id in lookup.items():
        lookup_tokens = lookup_key.split()
        if len(lookup_tokens) < 2:
            continue
        if lookup_tokens[0] == "worker":
            continue
        first = lookup_tokens[0]
        last = lookup_tokens[-1]
        if len(reference_tokens) == 1:
            if len(reference_tokens[0]) >= 3 and reference_tokens[0] == first:
                matches.add(resolved_id)
        elif len(reference_tokens) == 2:
            ref_first, ref_last = reference_tokens
            if ref_first == first and len(ref_last) == 1 and last.startswith(ref_last):
                matches.add(resolved_id)
            elif ref_first == first and ref_last == last:
                matches.add(resolved_id)
    return matches


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
    date = _value(row, "date", "session_date", "booking_date", "available_date", "specific_day")
    start = _value(row, "start_time", "from", "booking_start_time", "available_start", "available_start_time")
    end_key = keys[-1] if keys else ""
    wants_end = any(token in key for key in keys for token in ("end", "finish", "to"))
    if wants_end:
        start = _value(row, "end_time", "to", "booking_finish_time", "finish_time", "available_end", "available_end_time")
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
    slash_datetime = re.fullmatch(r"(\d{1,2}/\d{1,2}/\d{4})[ T]+(\d{1,2}:\d{2}(?::\d{2})?)", text)
    if slash_datetime:
        normalized = f"{_parse_date_text(slash_datetime.group(1)).isoformat()}T{_coerce_time(slash_datetime.group(2))}"
        return normalized + "+01:00"
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
    return _parse_date_text(text).isoformat()


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


def _parse_datetime_text(value: str) -> datetime:
    text = _coerce_datetime(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def _parse_date_text(value: str) -> date:
    text = value.strip()
    if _is_number(text):
        return _excel_serial_datetime(float(text)).date()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    if "T" in text:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    raise ValueError(f"invalid date {value}")


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


def _truthy_2_to_1(value: str) -> bool:
    text = _norm_name(value)
    return text in {"2 1", "2 to 1", "2to1", "two to one", "yes", "y", "true", "1"}


def _bool_text(value: str) -> str:
    return "true" if _truthy(value) else "false"


def _gender_text(value: str) -> str:
    text = _norm_name(value).replace(" ", "_")
    aliases = {
        "f": "female",
        "woman": "female",
        "girl": "female",
        "m": "male",
        "man": "male",
        "boy": "male",
        "nonbinary": "non_binary",
        "non_binary": "non_binary",
        "prefer_not_to_say": "prefer_not_to_say",
        "not_disclosed": "prefer_not_to_say",
        "not_specified": "unknown",
    }
    normalized = aliases.get(text, text)
    return normalized if normalized in {"female", "male", "non_binary", "other", "prefer_not_to_say", "unknown"} else ""


def _postcode(value: str) -> str:
    normalized = value.replace("_", " ").upper()
    match = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", normalized)
    if not match:
        text = normalized.strip()
        if re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?(?:\s*\d[A-Z]{2})?", text):
            return text
        return ""
    postcode = match.group(1).replace(" ", "")
    return f"{postcode[:-3]} {postcode[-3:]}"


def _area(value: str) -> str:
    value = value.strip().upper()
    if not value:
        return ""
    return value.split()[0]


def _status(value: str, assigned_ids: list[str], required_worker_count: int = 1) -> str:
    text = value.strip().lower()
    allowed = {"unallocated", "partially_allocated", "allocated", "confirmed", "cancelled", "rejected"}
    if text in allowed:
        return text
    if len(assigned_ids) >= required_worker_count:
        return "allocated"
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
    else:
        refs.update(str(path) for path in sorted(source_dir.glob("*.xlsx")))
    for path in sorted(source_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in REQUEST_FILE_EXTENSIONS:
            refs.add(str(path))
        if path.is_file() and path.suffix.lower() == ".pdf" and "availability" in _norm_name(str(path)):
            refs.add(str(path))
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
