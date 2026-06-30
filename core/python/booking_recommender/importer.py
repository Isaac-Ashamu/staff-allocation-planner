"""Standardized CSV v1 importer.

This module loads the tenant-agnostic import contract documented in
``docs/NORMALIZED_DATA_CONTRACT.md``. Tenant-specific workbook, email, or report
parsing belongs upstream; this importer only accepts the normalized files.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from .models import (
    Allocation,
    AvailabilityWindow,
    Compatibility,
    FamilyRequest,
    ImportAuditMetadata,
    Location,
    PairHistory,
    PlanningState,
    ServiceUser,
    Session,
    SourceReference,
    TenantRules,
    TravelMode,
    Worker,
)


REQUIRED_CSV_FILES = (
    "workers.csv",
    "service_users.csv",
    "sessions.csv",
    "availability.csv",
    "unavailability.csv",
    "compatibility.csv",
    "family_requests.csv",
    "pair_history.csv",
)

SESSION_STATUSES = {
    "unallocated",
    "partially_allocated",
    "allocated",
    "confirmed",
    "cancelled",
    "rejected",
}
COMPATIBILITY_STATUSES = {"appropriate", "inappropriate", "unknown"}
GENDER_VALUES = {"female", "male", "non_binary", "other", "prefer_not_to_say", "unknown"}


@dataclass(frozen=True)
class ImportIssue:
    severity: str
    code: str
    message: str
    table: str | None = None
    row_number: int | None = None
    field: str | None = None
    source_ref: str | None = None


@dataclass(frozen=True)
class ImportReport:
    row_counts: Mapping[str, int]
    issues: tuple[ImportIssue, ...] = ()

    @property
    def errors(self) -> tuple[ImportIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[ImportIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    @property
    def infos(self) -> tuple[ImportIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "info")


@dataclass(frozen=True)
class StandardizedImportResult:
    state: PlanningState
    report: ImportReport


class StandardizedImportError(ValueError):
    def __init__(self, report: ImportReport):
        self.report = report
        messages = "; ".join(_format_issue(issue) for issue in report.errors)
        super().__init__(messages or "standardized import failed")


def load_standardized_csv(
    folder: str | Path,
    *,
    tenant_rules: TenantRules | None = None,
    decision_time: datetime | None = None,
) -> StandardizedImportResult:
    """Load a standardized CSV v1 folder into a validated ``PlanningState``."""

    folder_path = Path(folder)
    issues: list[ImportIssue] = []
    rows_by_file = _read_all_tables(folder_path, issues)
    manifest = _read_manifest(folder_path, issues)
    row_counts = {filename.removesuffix(".csv"): len(rows_by_file.get(filename, ())) for filename in REQUIRED_CSV_FILES}

    import_run_id = str(manifest.get("import_run_id") or "").strip()
    imported_at = _parse_datetime_value(
        str(manifest.get("imported_at") or ""),
        issues,
        table="import_manifest",
        row_number=None,
        field="imported_at",
    )
    source_system = str(manifest.get("source_system") or "").strip()
    source_refs_value = manifest.get("source_refs", [])
    source_refs = tuple(str(item) for item in source_refs_value) if isinstance(source_refs_value, list) else ()

    if not import_run_id:
        _add_issue(issues, "error", "missing_import_run_id", "manifest import_run_id is required", "import_manifest", None, "import_run_id")
    if imported_at is None:
        imported_at = datetime.fromtimestamp(0).astimezone()
    if not source_system:
        _add_issue(issues, "error", "missing_source_system", "manifest source_system is required", "import_manifest", None, "source_system")
    if not isinstance(source_refs_value, list):
        _add_issue(issues, "error", "invalid_source_refs", "manifest source_refs must be an array", "import_manifest", None, "source_refs")

    workers = _parse_workers(rows_by_file.get("workers.csv", ()), import_run_id, issues)
    service_users = _parse_service_users(rows_by_file.get("service_users.csv", ()), import_run_id, workers, issues)
    availability = _parse_windows(rows_by_file.get("availability.csv", ()), "availability", import_run_id, workers, issues)
    unavailability = _parse_windows(rows_by_file.get("unavailability.csv", ()), "unavailability", import_run_id, workers, issues)
    workers = _attach_worker_windows(workers, availability, unavailability)
    sessions = _parse_sessions(rows_by_file.get("sessions.csv", ()), import_run_id, service_users, workers, issues)
    workers = _attach_worker_allocations(workers, sessions)
    compatibility = _parse_compatibility(rows_by_file.get("compatibility.csv", ()), import_run_id, workers, service_users, issues)
    pair_history = _parse_pair_history(rows_by_file.get("pair_history.csv", ()), import_run_id, workers, service_users, issues)
    family_requests = _parse_family_requests(rows_by_file.get("family_requests.csv", ()), import_run_id, workers, service_users, issues)

    _add_quality_warnings(workers, service_users, sessions, compatibility, issues)

    report = ImportReport(row_counts=row_counts, issues=tuple(sorted(issues, key=_issue_sort_key)))
    if report.errors:
        raise StandardizedImportError(report)

    warning_codes = tuple(_format_issue(issue) for issue in report.warnings)
    audit = ImportAuditMetadata(
        import_run_id=import_run_id,
        imported_at=imported_at,
        source_system=source_system,
        source_refs=source_refs,
        row_counts=row_counts,
        validation_warnings=warning_codes,
        generated_state_snapshot_id=_optional_text(manifest.get("generated_state_snapshot_id")),
    )
    state = PlanningState(
        tenant_rules=tenant_rules or TenantRules(tenant_id="standardized-csv"),
        decision_time=decision_time or imported_at,
        workers=workers,
        service_users=service_users,
        sessions=sessions,
        compatibility=compatibility,
        pair_history=pair_history,
        family_requests=family_requests,
        data_quality_warnings=warning_codes,
        import_audit=audit,
    )
    return StandardizedImportResult(state=state, report=report)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load a standardized CSV v1 import folder.")
    parser.add_argument("folder", help="Folder containing import_manifest.json and standardized CSV files.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        result = load_standardized_csv(args.folder)
    except StandardizedImportError as error:
        _print_summary(error.report)
        return 1

    _print_summary(result.report)
    return 0


def _read_all_tables(folder: Path, issues: list[ImportIssue]) -> dict[str, tuple[dict[str, str], ...]]:
    rows_by_file: dict[str, tuple[dict[str, str], ...]] = {}
    for filename in REQUIRED_CSV_FILES:
        path = folder / filename
        if not path.is_file():
            _add_issue(issues, "error", "missing_file", f"required file is missing: {filename}", filename.removesuffix(".csv"))
            rows_by_file[filename] = ()
            continue
        with path.open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            rows = []
            for index, row in enumerate(reader, start=2):
                normalized = {key: (value or "").strip() for key, value in row.items() if key is not None}
                normalized["_row_number"] = str(index)
                rows.append(normalized)
            rows_by_file[filename] = tuple(rows)
    return rows_by_file


def _read_manifest(folder: Path, issues: list[ImportIssue]) -> dict:
    path = folder / "import_manifest.json"
    if not path.is_file():
        _add_issue(issues, "error", "missing_manifest", "required file is missing: import_manifest.json", "import_manifest")
        return {}
    try:
        with path.open(encoding="utf-8") as file:
            value = json.load(file)
    except json.JSONDecodeError as error:
        _add_issue(issues, "error", "invalid_manifest_json", str(error), "import_manifest")
        return {}
    if not isinstance(value, dict):
        _add_issue(issues, "error", "invalid_manifest", "manifest must be a JSON object", "import_manifest")
        return {}
    return value


def _parse_workers(rows, import_run_id: str, issues: list[ImportIssue]) -> dict[str, Worker]:
    workers: dict[str, Worker] = {}
    seen: set[str] = set()
    for row in rows:
        row_number = _row_number(row)
        worker_id = _required(row, "worker_id", "workers", issues)
        if not worker_id:
            continue
        if worker_id in seen:
            _add_row_issue(issues, "error", "duplicate_worker_id", f"duplicate worker_id {worker_id}", "workers", row, "worker_id")
            continue
        seen.add(worker_id)
        name = _required(row, "name", "workers", issues)
        travel_modes = _travel_modes(row, "travel_modes", "workers", issues)
        has_car = _bool(row, "has_car", "workers", issues)
        current_hours = _optional_float(row, "current_hours", "workers", issues, default=0.0)
        max_hours = _optional_float(row, "max_hours", "workers", issues)
        reliability = _optional_float(row, "reliability_score", "workers", issues)
        if reliability is not None and not 0 <= reliability <= 1:
            _add_row_issue(issues, "error", "invalid_reliability_score", "reliability_score must be between 0 and 1", "workers", row, "reliability_score")
        workers[worker_id] = Worker(
            id=worker_id,
            name=name,
            home_location=_location(row, "home", "workers", issues),
            travel_modes=frozenset(travel_modes),
            gender=_optional_gender(row, "gender", "workers", issues),
            has_car=bool(has_car),
            skills=frozenset(_list(row.get("skills", ""))),
            languages=frozenset(_list(row.get("languages", ""))),
            willing_areas=frozenset(_list(row.get("willing_areas", ""))),
            current_hours=current_hours or 0.0,
            max_hours=max_hours,
            preference_tags=frozenset(_list(row.get("preference_tags", ""))),
            reliability_score=reliability,
            source_reference=_source(import_run_id, "workers", row, row_number),
        )
    return workers


def _parse_service_users(rows, import_run_id: str, workers: Mapping[str, Worker], issues: list[ImportIssue]) -> dict[str, ServiceUser]:
    service_users: dict[str, ServiceUser] = {}
    seen: set[str] = set()
    for row in rows:
        service_user_id = _required(row, "service_user_id", "service_users", issues)
        if not service_user_id:
            continue
        if service_user_id in seen:
            _add_row_issue(issues, "error", "duplicate_service_user_id", f"duplicate service_user_id {service_user_id}", "service_users", row, "service_user_id")
            continue
        seen.add(service_user_id)
        preferred = frozenset(_list(row.get("preferred_worker_ids", "")))
        avoided = frozenset(_list(row.get("avoided_worker_ids", "")))
        _warn_unknown_ids(issues, preferred, workers, "unknown_preferred_worker", "service_users", row, "preferred_worker_ids")
        _warn_unknown_ids(issues, avoided, workers, "unknown_avoided_worker", "service_users", row, "avoided_worker_ids")
        profile_is_stale = _bool(row, "profile_is_stale", "service_users", issues)
        if profile_is_stale:
            _add_row_issue(issues, "warning", "stale_service_user_profile", f"service user {service_user_id} profile is stale", "service_users", row, "profile_is_stale")
        service_users[service_user_id] = ServiceUser(
            id=service_user_id,
            name=_required(row, "name", "service_users", issues),
            home_location=_location(row, "home", "service_users", issues),
            support_level=_required(row, "support_level", "service_users", issues),
            gender=_optional_gender(row, "gender", "service_users", issues),
            required_worker_count=_positive_int(row, "required_worker_count", "service_users", issues, minimum=1) or 1,
            requires_driver=bool(_bool(row, "requires_driver", "service_users", issues)),
            required_skills=frozenset(_list(row.get("required_skills", ""))),
            communication_needs=frozenset(_list(row.get("communication_needs", ""))),
            interests=frozenset(_list(row.get("interests", ""))),
            risk_tags=frozenset(_list(row.get("risk_tags", ""))),
            preferred_worker_ids=preferred,
            avoided_worker_ids=avoided,
            profile_is_stale=bool(profile_is_stale),
            source_reference=_source(import_run_id, "service_users", row, _row_number(row)),
        )
    return service_users


def _parse_windows(rows, table: str, import_run_id: str, workers: Mapping[str, Worker], issues: list[ImportIssue]) -> dict[str, list[AvailabilityWindow]]:
    windows: dict[str, list[AvailabilityWindow]] = {}
    id_field = f"{table}_id"
    seen: set[str] = set()
    for row in rows:
        row_id = _required(row, id_field, table, issues)
        if row_id in seen:
            _add_row_issue(issues, "error", f"duplicate_{id_field}", f"duplicate {id_field} {row_id}", table, row, id_field)
            continue
        seen.add(row_id)
        worker_id = _required(row, "worker_id", table, issues)
        if worker_id and worker_id not in workers:
            _add_row_issue(issues, "error", "unknown_worker_id", f"unknown worker_id {worker_id}", table, row, "worker_id")
        start, end = _start_end(row, table, issues)
        if worker_id and start and end:
            windows.setdefault(worker_id, []).append(AvailabilityWindow(start, end))
        _source(import_run_id, table, row, _row_number(row))
    return windows


def _attach_worker_windows(
    workers: Mapping[str, Worker],
    availability: Mapping[str, list[AvailabilityWindow]],
    unavailability: Mapping[str, list[AvailabilityWindow]],
) -> dict[str, Worker]:
    from dataclasses import replace

    return {
        worker_id: replace(
            worker,
            availability=tuple(sorted(availability.get(worker_id, ()), key=lambda window: window.start)),
            unavailability=tuple(sorted(unavailability.get(worker_id, ()), key=lambda window: window.start)),
        )
        for worker_id, worker in workers.items()
    }


def _parse_sessions(rows, import_run_id: str, service_users: Mapping[str, ServiceUser], workers: Mapping[str, Worker], issues: list[ImportIssue]) -> dict[str, Session]:
    sessions: dict[str, Session] = {}
    seen: set[str] = set()
    for row in rows:
        session_id = _required(row, "session_id", "sessions", issues)
        if not session_id:
            continue
        if session_id in seen:
            _add_row_issue(issues, "error", "duplicate_session_id", f"duplicate session_id {session_id}", "sessions", row, "session_id")
            continue
        seen.add(session_id)
        service_user_id = _required(row, "service_user_id", "sessions", issues)
        if service_user_id and service_user_id not in service_users:
            _add_row_issue(issues, "error", "unknown_service_user_id", f"unknown service_user_id {service_user_id}", "sessions", row, "service_user_id")
        start, end = _start_end(row, "sessions", issues)
        required_worker_count = _positive_int(row, "required_worker_count", "sessions", issues, minimum=1) or 1
        assigned_ids = _list(row.get("assigned_worker_ids", ""))
        assigned_roles = _list(row.get("assigned_roles", ""))
        if len(assigned_ids) != len(assigned_roles):
            _add_row_issue(issues, "error", "assigned_role_count_mismatch", "assigned_worker_ids and assigned_roles must have the same length", "sessions", row, "assigned_roles")
        for worker_id in assigned_ids:
            if worker_id not in workers:
                _add_row_issue(issues, "error", "unknown_assigned_worker_id", f"unknown assigned worker_id {worker_id}", "sessions", row, "assigned_worker_ids")
        if len(set(assigned_ids)) != len(assigned_ids):
            _add_row_issue(issues, "error", "duplicate_assigned_worker_id", "assigned_worker_ids contains a duplicate worker", "sessions", row, "assigned_worker_ids")
        if len(assigned_ids) > required_worker_count:
            _add_row_issue(issues, "error", "too_many_assigned_workers", "assigned workers exceed required_worker_count", "sessions", row, "assigned_worker_ids")
        status = _required(row, "status", "sessions", issues)
        if status and status not in SESSION_STATUSES:
            _add_row_issue(issues, "error", "invalid_session_status", f"invalid session status {status}", "sessions", row, "status")
        priority = _optional_float(row, "priority", "sessions", issues, default=1.0)
        if priority is not None and priority <= 0:
            _add_row_issue(issues, "error", "invalid_priority", "priority must be greater than 0", "sessions", row, "priority")
        if start and end:
            sessions[session_id] = Session(
                id=session_id,
                service_user_id=service_user_id,
                start=start,
                end=end,
                location=_location(row, "location", "sessions", issues),
                required_worker_count=required_worker_count,
                assigned=tuple(Allocation(worker_id=worker_id, role=role) for worker_id, role in zip(assigned_ids, assigned_roles)),
                requires_driver=bool(_bool(row, "requires_driver", "sessions", issues)),
                priority=priority or 1.0,
                status=status or "unallocated",
                travel_buffer_before_minutes=_optional_int(row, "travel_buffer_before_minutes", "sessions", issues, minimum=0),
                travel_buffer_after_minutes=_optional_int(row, "travel_buffer_after_minutes", "sessions", issues, minimum=0),
                source_reference=_source(import_run_id, "sessions", row, _row_number(row)),
            )
    return sessions


def _attach_worker_allocations(workers: Mapping[str, Worker], sessions: Mapping[str, Session]) -> dict[str, Worker]:
    from dataclasses import replace

    allocations: dict[str, list[str]] = {worker_id: list(worker.current_allocations) for worker_id, worker in workers.items()}
    for session in sorted(sessions.values(), key=lambda item: (item.start, item.id)):
        for allocation in session.assigned:
            if allocation.worker_id in allocations and session.id not in allocations[allocation.worker_id]:
                allocations[allocation.worker_id].append(session.id)
    return {
        worker_id: replace(worker, current_allocations=tuple(allocations[worker_id]))
        for worker_id, worker in workers.items()
    }


def _parse_compatibility(rows, import_run_id: str, workers, service_users, issues) -> dict[tuple[str, str], Compatibility]:
    compatibility = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        worker_id = _required(row, "worker_id", "compatibility", issues)
        service_user_id = _required(row, "service_user_id", "compatibility", issues)
        key = (worker_id, service_user_id)
        if key in seen:
            _add_row_issue(issues, "error", "duplicate_compatibility", f"duplicate compatibility record {worker_id}/{service_user_id}", "compatibility", row)
            continue
        seen.add(key)
        _require_known_pair(worker_id, service_user_id, workers, service_users, "compatibility", row, issues)
        status = _required(row, "status", "compatibility", issues)
        if status not in COMPATIBILITY_STATUSES:
            _add_row_issue(issues, "error", "invalid_compatibility_status", f"invalid compatibility status {status}", "compatibility", row, "status")
        elif status == "unknown":
            _add_row_issue(issues, "warning", "unknown_compatibility", f"compatibility is unknown for {worker_id}/{service_user_id}", "compatibility", row, "status")
        if worker_id and service_user_id and status in COMPATIBILITY_STATUSES:
            compatibility[key] = Compatibility(worker_id, service_user_id, status, row.get("reason") or None, _source(import_run_id, "compatibility", row, _row_number(row)))
    return compatibility


def _parse_pair_history(rows, import_run_id: str, workers, service_users, issues) -> dict[tuple[str, str], PairHistory]:
    histories = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        worker_id = _required(row, "worker_id", "pair_history", issues)
        service_user_id = _required(row, "service_user_id", "pair_history", issues)
        key = (worker_id, service_user_id)
        if key in seen:
            _add_row_issue(issues, "error", "duplicate_pair_history", f"duplicate pair_history record {worker_id}/{service_user_id}", "pair_history", row)
            continue
        seen.add(key)
        _require_known_pair(worker_id, service_user_id, workers, service_users, "pair_history", row, issues)
        histories[key] = PairHistory(
            worker_id=worker_id,
            service_user_id=service_user_id,
            completed_sessions=_positive_int(row, "completed_sessions", "pair_history", issues, minimum=0) or 0,
            positive_feedback_count=_positive_int(row, "positive_feedback_count", "pair_history", issues, minimum=0) or 0,
            incident_count=_positive_int(row, "incident_count", "pair_history", issues, minimum=0) or 0,
            cancellation_count=_positive_int(row, "cancellation_count", "pair_history", issues, minimum=0) or 0,
            last_session_at=_optional_datetime(row, "last_session_at", "pair_history", issues),
            source_reference=_source(import_run_id, "pair_history", row, _row_number(row)),
        )
    return histories


def _parse_family_requests(rows, import_run_id: str, workers, service_users, issues) -> tuple[FamilyRequest, ...]:
    requests = []
    seen: set[str] = set()
    for row in rows:
        request_id = _required(row, "family_request_id", "family_requests", issues)
        if not request_id:
            continue
        if request_id in seen:
            _add_row_issue(issues, "error", "duplicate_family_request_id", f"duplicate family_request_id {request_id}", "family_requests", row, "family_request_id")
            continue
        seen.add(request_id)
        service_user_id = _required(row, "service_user_id", "family_requests", issues)
        if service_user_id and service_user_id not in service_users:
            _add_row_issue(issues, "error", "unknown_service_user_id", f"unknown service_user_id {service_user_id}", "family_requests", row, "service_user_id")
        requested = frozenset(_list(row.get("requested_worker_ids", "")))
        avoided = frozenset(_list(row.get("avoided_worker_ids", "")))
        _warn_unknown_ids(issues, requested, workers, "unknown_requested_worker", "family_requests", row, "requested_worker_ids")
        _warn_unknown_ids(issues, avoided, workers, "unknown_avoided_worker", "family_requests", row, "avoided_worker_ids")
        requests.append(
            FamilyRequest(
                id=request_id,
                service_user_id=service_user_id,
                requested_worker_ids=requested,
                avoided_worker_ids=avoided,
                preferred_days=frozenset(_preferred_days(row, issues)),
                preferred_time_ranges=tuple(_intervals(row, "preferred_time_ranges", "family_requests", issues)),
                unavailable_windows=tuple(_intervals(row, "unavailable_time_ranges", "family_requests", issues)),
                notes=row.get("notes", ""),
                source_ref=row.get("source_ref") or None,
                source_reference=_source(import_run_id, "family_requests", row, _row_number(row)),
            )
        )
    return tuple(requests)


def _add_quality_warnings(workers, service_users, sessions, compatibility, issues) -> None:
    for worker in workers.values():
        if not worker.travel_modes:
            _add_issue(issues, "warning", "missing_travel_mode", f"worker {worker.id} has no travel modes", "workers", worker.source_reference.row_number if worker.source_reference else None, "travel_modes", worker.source_reference.source_ref if worker.source_reference else None)
        if not worker.availability:
            _add_issue(issues, "warning", "missing_availability", f"worker {worker.id} has no availability windows", "availability")
        if not _has_usable_location(worker.home_location):
            _add_issue(issues, "warning", "missing_worker_location", f"worker {worker.id} has no usable home location", "workers", worker.source_reference.row_number if worker.source_reference else None)
    for service_user in service_users.values():
        if not _has_usable_location(service_user.home_location):
            _add_issue(
                issues,
                "warning",
                "missing_service_user_location",
                f"service user {service_user.id} ({service_user.name}) has no usable home location",
                "service_users",
                service_user.source_reference.row_number if service_user.source_reference else None,
                source_ref=service_user.source_reference.source_ref if service_user.source_reference else None,
            )
    for session in sessions.values():
        service_user = service_users.get(session.service_user_id)
        if not _has_usable_location(session.location):
            if service_user is not None and not _has_usable_location(service_user.home_location):
                _add_issue(
                    issues,
                    "warning",
                    "missing_session_location_due_to_missing_service_user_location",
                    (
                        f"session {session.id} has no usable location because service user "
                        f"{service_user.id} ({service_user.name}) has no usable home location to backfill"
                    ),
                    "sessions",
                    session.source_reference.row_number if session.source_reference else None,
                    "location",
                    session.source_reference.source_ref if session.source_reference else None,
                )
            else:
                _add_issue(
                    issues,
                    "warning",
                    "missing_session_location",
                    f"session {session.id} has no usable explicit or backfilled location",
                    "sessions",
                    session.source_reference.row_number if session.source_reference else None,
                    "location",
                    session.source_reference.source_ref if session.source_reference else None,
                )
        for allocation in session.assigned:
            worker = workers.get(allocation.worker_id)
            if worker is None:
                continue
            if not worker.availability:
                _add_issue(
                    issues,
                    "warning",
                    "existing_assignment_missing_availability",
                    (
                        f"source assignment {worker.id} ({worker.name}) on session {session.id} "
                        "has no explicit availability evidence; this is an imported booking audit, "
                        "not a recommender-selected assignment"
                    ),
                    "sessions",
                    session.source_reference.row_number if session.source_reference else None,
                    "assigned_worker_ids",
                    session.source_reference.source_ref if session.source_reference else None,
                )
            elif not any(_contains(window, session.start, session.end) for window in worker.availability):
                _add_issue(
                    issues,
                    "warning",
                    "existing_assignment_outside_availability",
                    (
                        f"source assignment {worker.id} ({worker.name}) on session {session.id} is outside "
                        "that worker's explicit availability window; this is an imported booking audit, "
                        "not a recommender-selected assignment"
                    ),
                    "sessions",
                    session.source_reference.row_number if session.source_reference else None,
                    "assigned_worker_ids",
                    session.source_reference.source_ref if session.source_reference else None,
                )
            if any(_overlaps(window, session.start, session.end) for window in worker.unavailability):
                _add_issue(
                    issues,
                    "warning",
                    "existing_assignment_overlaps_unavailability",
                    (
                        f"source assignment {worker.id} ({worker.name}) on session {session.id} overlaps "
                        "that worker's explicit unavailability; this is an imported booking audit, "
                        "not a recommender-selected assignment"
                    ),
                    "sessions",
                    session.source_reference.row_number if session.source_reference else None,
                    "assigned_worker_ids",
                    session.source_reference.source_ref if session.source_reference else None,
                )


def _start_end(row, table: str, issues: list[ImportIssue]) -> tuple[datetime | None, datetime | None]:
    start = _parse_datetime_value(row.get("start", ""), issues, table=table, row_number=_row_number(row), field="start", source_ref=row.get("source_ref"))
    end = _parse_datetime_value(row.get("end", ""), issues, table=table, row_number=_row_number(row), field="end", source_ref=row.get("source_ref"))
    if start and end and end <= start:
        _add_row_issue(issues, "error", "invalid_time_range", "end must be after start", table, row, "end")
    return start, end


def _location(row, prefix: str, table: str, issues: list[ImportIssue]) -> Location:
    label = row.get(f"{prefix}_label", "")
    latitude = _optional_float(row, f"{prefix}_latitude", table, issues)
    longitude = _optional_float(row, f"{prefix}_longitude", table, issues)
    if (latitude is None) != (longitude is None):
        _add_row_issue(issues, "error", "incomplete_coordinates", f"{prefix}_latitude and {prefix}_longitude must be provided together", table, row)
    location = Location(
        label=label,
        postcode=row.get(f"{prefix}_postcode") or None,
        latitude=latitude,
        longitude=longitude,
        area=row.get(f"{prefix}_area") or None,
    )
    return location


def _has_usable_location(location: Location) -> bool:
    return bool(location.label or location.postcode or location.area or (location.latitude is not None and location.longitude is not None))


def _contains(window: AvailabilityWindow, start: datetime, end: datetime) -> bool:
    return window.start <= start and end <= window.end


def _overlaps(window: AvailabilityWindow, start: datetime, end: datetime) -> bool:
    return start < window.end and end > window.start


def _required(row, field: str, table: str, issues: list[ImportIssue]) -> str:
    value = row.get(field, "").strip()
    if not value:
        _add_row_issue(issues, "error", "missing_required_field", f"{field} is required", table, row, field)
    return value


def _bool(row, field: str, table: str, issues: list[ImportIssue]) -> bool | None:
    value = _required(row, field, table, issues)
    if value in {"true", "false"}:
        return value == "true"
    if value:
        _add_row_issue(issues, "error", "invalid_boolean", f"{field} must be true or false", table, row, field)
    return None


def _travel_modes(row, field: str, table: str, issues: list[ImportIssue]) -> tuple[TravelMode, ...]:
    values = _list(row.get(field, ""))
    if not values:
        _add_row_issue(issues, "warning", "missing_travel_mode", "travel_modes is empty", table, row, field)
    modes = []
    for value in values:
        try:
            modes.append(TravelMode(value))
        except ValueError:
            _add_row_issue(issues, "error", "invalid_travel_mode", f"invalid travel mode {value}", table, row, field)
    return tuple(modes)


def _list(value: str) -> tuple[str, ...]:
    items = tuple(item.strip() for item in value.split("|") if item.strip())
    return items


def _optional_gender(row, field: str, table: str, issues: list[ImportIssue]) -> str | None:
    value = row.get(field, "").strip()
    if not value:
        return None
    normalized = value.lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "f": "female",
        "woman": "female",
        "girl": "female",
        "m": "male",
        "man": "male",
        "boy": "male",
        "nonbinary": "non_binary",
        "non_binary": "non_binary",
        "non_b": "non_binary",
        "prefer_not_to_say": "prefer_not_to_say",
        "not_disclosed": "prefer_not_to_say",
        "not_specified": "unknown",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in GENDER_VALUES:
        _add_row_issue(
            issues,
            "error",
            "invalid_gender",
            f"{field} must be one of {', '.join(sorted(GENDER_VALUES))}",
            table,
            row,
            field,
        )
        return None
    return normalized


def _optional_text(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_float(row, field: str, table: str, issues: list[ImportIssue], default=None):
    value = row.get(field, "")
    if value == "":
        return default
    try:
        return float(value)
    except ValueError:
        _add_row_issue(issues, "error", "invalid_decimal", f"{field} must be a decimal number", table, row, field)
        return default


def _optional_int(row, field: str, table: str, issues: list[ImportIssue], minimum: int | None = None):
    value = row.get(field, "")
    if value == "":
        return None
    try:
        parsed = int(value)
    except ValueError:
        _add_row_issue(issues, "error", "invalid_integer", f"{field} must be an integer", table, row, field)
        return None
    if minimum is not None and parsed < minimum:
        _add_row_issue(issues, "error", "integer_below_minimum", f"{field} must be >= {minimum}", table, row, field)
    return parsed


def _positive_int(row, field: str, table: str, issues: list[ImportIssue], minimum: int):
    value = _required(row, field, table, issues)
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        _add_row_issue(issues, "error", "invalid_integer", f"{field} must be an integer", table, row, field)
        return None
    if parsed < minimum:
        _add_row_issue(issues, "error", "integer_below_minimum", f"{field} must be >= {minimum}", table, row, field)
    return parsed


def _optional_datetime(row, field: str, table: str, issues: list[ImportIssue]) -> datetime | None:
    value = row.get(field, "")
    if not value:
        return None
    return _parse_datetime_value(value, issues, table=table, row_number=_row_number(row), field=field, source_ref=row.get("source_ref"))


def _parse_datetime_value(value: str, issues: list[ImportIssue], *, table: str, row_number: int | None, field: str, source_ref: str | None = None) -> datetime | None:
    if not value:
        _add_issue(issues, "error", "missing_datetime", f"{field} is required", table, row_number, field, source_ref)
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _add_issue(issues, "error", "invalid_datetime", f"{field} must be ISO 8601 with UTC offset", table, row_number, field, source_ref)
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _add_issue(issues, "error", "naive_datetime", f"{field} must include a UTC offset", table, row_number, field, source_ref)
        return None
    return parsed


def _preferred_days(row, issues: list[ImportIssue]) -> tuple[int, ...]:
    days = []
    for value in _list(row.get("preferred_days", "")):
        try:
            day = int(value)
        except ValueError:
            _add_row_issue(issues, "error", "invalid_preferred_day", f"preferred day {value} is not an integer", "family_requests", row, "preferred_days")
            continue
        if day not in range(7):
            _add_row_issue(issues, "error", "invalid_preferred_day", f"preferred day {day} must be 0..6", "family_requests", row, "preferred_days")
            continue
        days.append(day)
    return tuple(days)


def _intervals(row, field: str, table: str, issues: list[ImportIssue]) -> tuple[AvailabilityWindow, ...]:
    windows = []
    for value in _list(row.get(field, "")):
        if "/" not in value:
            _add_row_issue(issues, "error", "invalid_interval", f"{field} interval must use start/end", table, row, field)
            continue
        start_value, end_value = value.split("/", maxsplit=1)
        start = _parse_datetime_value(start_value, issues, table=table, row_number=_row_number(row), field=field, source_ref=row.get("source_ref"))
        end = _parse_datetime_value(end_value, issues, table=table, row_number=_row_number(row), field=field, source_ref=row.get("source_ref"))
        if start and end:
            if end <= start:
                _add_row_issue(issues, "error", "invalid_interval", f"{field} interval end must be after start", table, row, field)
            else:
                windows.append(AvailabilityWindow(start, end))
    return tuple(windows)


def _warn_unknown_ids(issues, ids, known, code, table, row, field):
    for item_id in ids:
        if item_id not in known:
            _add_row_issue(issues, "warning", code, f"unknown worker_id {item_id}", table, row, field)


def _require_known_pair(worker_id, service_user_id, workers, service_users, table, row, issues):
    if worker_id and worker_id not in workers:
        _add_row_issue(issues, "error", "unknown_worker_id", f"unknown worker_id {worker_id}", table, row, "worker_id")
    if service_user_id and service_user_id not in service_users:
        _add_row_issue(issues, "error", "unknown_service_user_id", f"unknown service_user_id {service_user_id}", table, row, "service_user_id")


def _source(import_run_id: str, table: str, row, row_number: int | None) -> SourceReference:
    return SourceReference(
        import_run_id=import_run_id,
        table=table,
        source_ref=row.get("source_ref") or f"{table}.csv",
        row_number=row_number,
    )


def _row_number(row) -> int | None:
    value = row.get("_row_number")
    return int(value) if value else None


def _add_row_issue(issues, severity, code, message, table, row, field=None):
    _add_issue(issues, severity, code, message, table, _row_number(row), field, row.get("source_ref"))


def _add_issue(issues, severity, code, message, table=None, row_number=None, field=None, source_ref=None):
    issues.append(ImportIssue(severity, code, message, table, row_number, field, source_ref))


def _issue_sort_key(issue: ImportIssue):
    severity_order = {"error": 0, "warning": 1, "info": 2}
    return (
        severity_order.get(issue.severity, 9),
        issue.table or "",
        issue.row_number or 0,
        issue.code,
        issue.field or "",
        issue.message,
    )


def _format_issue(issue: ImportIssue) -> str:
    location = issue.table or "import"
    if issue.row_number is not None:
        location = f"{location}:{issue.row_number}"
    if issue.field:
        location = f"{location}.{issue.field}"
    return f"{issue.severity}:{issue.code}:{location}: {issue.message}"


def _print_summary(report: ImportReport) -> None:
    print("Standardized CSV import summary")
    for table, count in sorted(report.row_counts.items()):
        print(f"- {table}: {count}")
    print(f"errors: {len(report.errors)}")
    print(f"warnings: {len(report.warnings)}")
    for issue in report.issues:
        print(_format_issue(issue))


if __name__ == "__main__":
    raise SystemExit(main())
