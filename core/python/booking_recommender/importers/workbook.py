"""provider spreadsheet import adapter.

This adapter is intentionally separate from the recommender core. The SaaS
model should remain tenant-agnostic; provider-specific file names and columns belong
here.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, time
import json
from pathlib import Path
import re

from ..models import (
    Allocation,
    AvailabilityWindow,
    Compatibility,
    FamilyRequest,
    Location,
    PlanningState,
    ServiceUser,
    Session,
    TenantRules,
    TravelMode,
    Worker,
)
from .xlsx import read_xlsx_sheet, read_xlsx_sheet_raw
from .availability_pdf import parse_availability_reports
from .date_requests import parse_date_request_folder


@dataclass(frozen=True)
class ImportResult:
    state: PlanningState
    warnings: tuple[str, ...]


def import_workbook_planning_state(
    *,
    import_workbook: str | Path,
    postcode_workbook: str | Path,
    ad_hocs_workbook: str | Path | None = None,
    availability_reports: str | Path | None = None,
    date_requests_path: str | Path | None = None,
    aliases_path: str | Path | None = None,
    tenant_id: str = "workbook",
    decision_time: datetime | None = None,
) -> ImportResult:
    decision_time = decision_time or datetime.now()
    warnings: list[str] = []
    aliases = _load_aliases(aliases_path)

    postcode_data = _load_postcode_data(postcode_workbook, warnings)
    workers = _load_workers(import_workbook, postcode_data, warnings)
    if availability_reports:
        workers = _apply_availability_reports(workers, availability_reports, warnings)
    service_users = _load_service_users(import_workbook, postcode_data, warnings)
    sessions = _load_sessions(import_workbook, service_users, warnings)
    compatibility = _load_compatibility(import_workbook, workers, service_users)
    family_requests = _load_family_requests(ad_hocs_workbook, service_users, workers, aliases, warnings)
    family_requests = family_requests + _load_date_request_files(
        date_requests_path,
        service_users,
        workers,
        aliases,
        warnings,
    )

    state = PlanningState(
        tenant_rules=TenantRules(tenant_id=tenant_id),
        decision_time=decision_time,
        workers=workers,
        service_users=service_users,
        sessions=sessions,
        compatibility=compatibility,
        family_requests=family_requests,
        data_quality_warnings=tuple(warnings),
    )
    return ImportResult(state=state, warnings=tuple(warnings))


def _apply_availability_reports(
    workers: dict[str, Worker],
    availability_reports: str | Path,
    warnings: list[str],
) -> dict[str, Worker]:
    try:
        records = parse_availability_reports(availability_reports)
    except RuntimeError as exc:
        warnings.append(str(exc))
        return workers

    if not records:
        warnings.append(f"No availability records parsed from {availability_reports}.")
        return workers

    worker_windows: dict[str, list[AvailabilityWindow]] = {worker_id: [] for worker_id in workers}
    worker_unavailability: dict[str, list[AvailabilityWindow]] = {worker_id: [] for worker_id in workers}
    unmatched = set()

    for record in records:
        worker = _best_worker(record.worker_name, workers)
        if not worker:
            unmatched.add(record.worker_name)
            continue
        if record.unavailable:
            worker_unavailability[worker.id].append(
                AvailabilityWindow(
                    start=datetime.combine(record.day, time(8, 0)),
                    end=datetime.combine(record.day, time(23, 0)),
                )
            )
        else:
            worker_windows[worker.id].extend(record.windows)

    updated = {}
    for worker_id, worker in workers.items():
        updated[worker_id] = replace(
            worker,
            availability=tuple(worker_windows[worker_id]),
            unavailability=tuple(worker_unavailability[worker_id]),
        )

    if unmatched:
        warnings.append(
            f"Availability report names not matched to worker list: {len(unmatched)} "
            f"({', '.join(sorted(unmatched)[:10])})"
        )
    warnings.append(f"Parsed {len(records)} availability report rows from {availability_reports}.")
    return updated


def _load_postcode_data(path: str | Path, warnings: list[str]) -> dict[str, dict[str, dict[str, str]]]:
    data = {"workers": {}, "service_users": {}}
    if not Path(path).exists():
        warnings.append(f"Postcode workbook not found: {path}")
        return data

    for row in read_xlsx_sheet(path, "STAFF"):
        name = _value(row, "Support Worker")
        if not name:
            continue
        data["workers"][_identity_key(name)] = {
            "name": name,
            "postcode": _value(row, "Address"),
            "travel": _value(row, "How they get to sessions"),
            "languages": _value(row, "Languages Spoken"),
        }

    for row in read_xlsx_sheet_raw(path, "SU")[1:]:
        first = row.values.get(1, "").strip()
        last = row.values.get(2, "").strip()
        postcode = row.values.get(3, "").strip()
        support_level = row.values.get(4, "").strip()
        name = f"{first} {last}".strip()
        if not name:
            continue
        data["service_users"][_identity_key(name)] = {
            "name": name,
            "postcode": postcode,
            "support_level": support_level,
        }

    return data


def _load_workers(
    import_workbook: str | Path,
    postcode_data: dict[str, dict[str, dict[str, str]]],
    warnings: list[str],
) -> dict[str, Worker]:
    rows = read_xlsx_sheet(import_workbook, "Support Workers")
    workers: dict[str, Worker] = {}

    for row in rows:
        worker_id = _value(row, "SUPPORT WORKER ID")
        name = _value(row, "SUPPORT WORKER NAME")
        if not worker_id or not name:
            continue
        workers[worker_id] = _worker_from_source(worker_id, name, _value(row, "CAR"), postcode_data)

    if workers:
        return workers

    warnings.append(
        "Monthly Support Workers sheet is empty; using staff/service-user postcode workbook as worker source."
    )
    for key, row in postcode_data["workers"].items():
        workers[key] = _worker_from_source(key, row["name"], "", postcode_data)
    return workers


def _worker_from_source(
    worker_id: str,
    name: str,
    car_value: str,
    postcode_data: dict[str, dict[str, dict[str, str]]],
) -> Worker:
    postcode_row = postcode_data["workers"].get(_identity_key(name), {})
    travel_text = postcode_row.get("travel", "")
    has_car = _yes(car_value) or bool(re.search(r"\b(car|drive|driver)\b", travel_text, re.I))
    travel_modes = _travel_modes(travel_text, has_car)
    postcode = postcode_row.get("postcode") or None
    area = _postcode_area(postcode)
    languages = frozenset(
        item.strip()
        for item in re.split(r"[,;/]", postcode_row.get("languages", ""))
        if item.strip()
    )
    return Worker(
        id=worker_id,
        name=name,
        home_location=Location(label=f"{name} home", postcode=postcode, area=area),
        travel_modes=travel_modes,
        has_car=has_car,
        languages=languages,
        willing_areas=frozenset({area} if area else ()),
    )


def _load_service_users(
    import_workbook: str | Path,
    postcode_data: dict[str, dict[str, dict[str, str]]],
    warnings: list[str],
) -> dict[str, ServiceUser]:
    service_users: dict[str, ServiceUser] = {}

    for row in read_xlsx_sheet(import_workbook, "Childs"):
        child_id = _value(row, "CHILD ID")
        name = _value(row, "CHILD NAME")
        if not child_id or not name:
            continue
        service_users[child_id] = _service_user_from_source(child_id, name, row, postcode_data)

    # Some monthly Childs sheets are empty; bookings still contain child IDs.
    for row in read_xlsx_sheet(import_workbook, "Bookings"):
        child_id = _value(row, "CHILD ID")
        name = _value(row, "CHILD NAME")
        if child_id and name and child_id not in service_users:
            service_users[child_id] = _service_user_from_source(child_id, name, row, postcode_data)

    existing_keys = {_identity_key(service_user.name) for service_user in service_users.values()}
    for key, row in postcode_data["service_users"].items():
        if key in existing_keys:
            continue
        service_users[f"su-{key}"] = _service_user_from_source(
            f"su-{key}",
            row["name"],
            {
                "ADDRESS": row.get("postcode", ""),
                "SUPPORT LEVEL": row.get("support_level", ""),
            },
            postcode_data,
        )

    if not service_users:
        warnings.append("No service users imported from monthly workbook.")
    return service_users


def _service_user_from_source(
    service_user_id: str,
    name: str,
    row: dict[str, str],
    postcode_data: dict[str, dict[str, dict[str, str]]],
) -> ServiceUser:
    postcode_row = _best_name_match(name, postcode_data["service_users"])
    postcode = postcode_row.get("postcode") if postcode_row else _value(row, "ADDRESS")
    support_level = postcode_row.get("support_level") if postcode_row else _value(row, "SUPPORT LEVEL")
    required_worker_count = 2 if _yes(_value(row, "2TO1")) or "2" in support_level else 1
    requires_driver = _yes(_value(row, "DRIVER REQUIRED"))
    area = _postcode_area(postcode)
    return ServiceUser(
        id=service_user_id,
        name=name,
        home_location=Location(label=f"{name} home", postcode=postcode or None, area=area),
        support_level=support_level or ("2:1" if required_worker_count >= 2 else "1:1"),
        required_worker_count=required_worker_count,
        requires_driver=requires_driver,
        required_skills=frozenset(),
    )


def _load_sessions(
    import_workbook: str | Path,
    service_users: dict[str, ServiceUser],
    warnings: list[str],
) -> dict[str, Session]:
    sessions: dict[str, Session] = {}
    for index, row in enumerate(read_xlsx_sheet(import_workbook, "Bookings"), start=1):
        child_id = _value(row, "CHILD ID")
        if not child_id:
            continue
        service_user = service_users.get(child_id)
        if not service_user:
            warnings.append(f"Booking row {index} references unknown child ID {child_id}.")
            continue
        start = _parse_datetime(_value(row, "BOOKING DATE"), _value(row, "BOOKING START TIME"))
        end = _parse_datetime(_value(row, "BOOKING DATE"), _value(row, "BOOKING FINISH TIME"))
        if not start or not end:
            warnings.append(f"Booking row {index} has an unparseable date/time.")
            continue
        assigned = []
        lead = _value(row, "Lead SW ID")
        second = _value(row, "2nd SW ID")
        if lead:
            assigned.append(Allocation(worker_id=lead, role="lead"))
        if second:
            assigned.append(Allocation(worker_id=second, role="second"))
        required_count = 2 if _yes(_value(row, "2to1 REQUIRED")) else service_user.required_worker_count
        status = "allocated" if len(assigned) >= required_count else ("partially_allocated" if assigned else "unallocated")
        session_id = _value(row, "BOOKING ID") or f"booking-{index}"
        sessions[session_id] = Session(
            id=session_id,
            service_user_id=child_id,
            start=start,
            end=end,
            location=service_user.home_location,
            required_worker_count=required_count,
            assigned=tuple(assigned),
            requires_driver=_yes(_value(row, "Car")) or service_user.requires_driver,
            status=status,
        )
    return sessions


def _load_compatibility(
    import_workbook: str | Path,
    workers: dict[str, Worker],
    service_users: dict[str, ServiceUser],
) -> dict[tuple[str, str], Compatibility]:
    compatibility: dict[tuple[str, str], Compatibility] = {}
    for row in read_xlsx_sheet(import_workbook, "Matching"):
        worker_id = _value(row, "SUPPORT WORKER ID")
        child_id = _value(row, "CHILD ID")
        appropriate = _value(row, "APPROPRIATE")
        if not worker_id or not child_id or worker_id not in workers or child_id not in service_users:
            continue
        status = "appropriate" if _yes(appropriate) else "inappropriate"
        compatibility[(worker_id, child_id)] = Compatibility(worker_id, child_id, status)
    return compatibility


def _load_family_requests(
    ad_hocs_workbook: str | Path | None,
    service_users: dict[str, ServiceUser],
    workers: dict[str, Worker],
    aliases: dict[str, dict[str, str]],
    warnings: list[str],
) -> tuple[FamilyRequest, ...]:
    if not ad_hocs_workbook or not Path(ad_hocs_workbook).exists():
        return ()
    requests: list[FamilyRequest] = []
    for sheet in ("Phase 1", "Phase 2"):
        rows = read_xlsx_sheet_raw(ad_hocs_workbook, sheet)
        if not rows:
            continue
        headers = _headers_from_raw(rows[0])
        for index, raw_row in enumerate(rows[1:], start=1):
            first_value = raw_row.values.get(1, "").strip()
            if not first_value:
                continue
            service_user = _best_service_user(first_value, service_users, aliases)
            if not service_user:
                warnings.append(f"Could not match ad hoc row '{first_value}' in sheet {sheet}.")
                continue
            note_parts = []
            for column, value in sorted(raw_row.values.items()):
                if not value:
                    continue
                header = headers.get(column) or ("Client name" if column == 1 else f"Column {column}")
                note_parts.append(f"{header}: {value}")
            notes = " | ".join(note_parts)
            requested_worker_ids = frozenset(
                worker.id for worker in workers.values() if _worker_mentioned(worker, notes, aliases)
            )
            requests.append(
                FamilyRequest(
                    id=f"adhoc-{sheet}-{index}",
                    service_user_id=service_user.id,
                    requested_worker_ids=requested_worker_ids,
                    notes=notes,
                    source_ref=f"{ad_hocs_workbook}#{sheet}!row{index + 1}",
                )
            )
    return tuple(requests)


def _load_date_request_files(
    date_requests_path: str | Path | None,
    service_users: dict[str, ServiceUser],
    workers: dict[str, Worker],
    aliases: dict[str, dict[str, str]],
    warnings: list[str],
) -> tuple[FamilyRequest, ...]:
    if not date_requests_path or not Path(date_requests_path).exists():
        return ()
    try:
        parsed = parse_date_request_folder(date_requests_path)
    except RuntimeError as exc:
        warnings.append(str(exc))
        return ()

    requests = []
    for index, document in enumerate(parsed, start=1):
        search_text = f"{Path(document.source_path).stem}\n{document.subject}\n{document.body}"
        matched_service_users = _mentioned_service_users(search_text, service_users, aliases)
        if not matched_service_users:
            warnings.append(f"Could not match date request file to service user: {document.source_path}")
            continue
        requested_worker_ids = frozenset(
            worker.id for worker in workers.values() if _worker_mentioned(worker, search_text, aliases)
        )
        for service_user in matched_service_users:
            requests.append(
                FamilyRequest(
                    id=f"date-request-{index}-{service_user.id}",
                    service_user_id=service_user.id,
                    requested_worker_ids=requested_worker_ids,
                    preferred_days=document.preferred_days,
                    preferred_time_ranges=document.preferred_windows,
                    unavailable_windows=document.unavailable_windows,
                    notes=_compact_text(search_text),
                    source_ref=document.source_path,
                )
            )
    warnings.append(f"Parsed {len(parsed)} date request files from {date_requests_path}.")
    return tuple(requests)


def _headers_from_raw(row) -> dict[int, str]:
    headers = {}
    for column, value in row.values.items():
        if value.strip():
            headers[column] = value.strip()
    return headers


def _travel_modes(text: str, has_car: bool) -> frozenset[TravelMode]:
    modes: set[TravelMode] = set()
    lowered = text.lower()
    if has_car:
        modes.add(TravelMode.DRIVE)
    if "cycle" in lowered or "bike" in lowered:
        modes.add(TravelMode.CYCLE)
    if "walk" in lowered:
        modes.add(TravelMode.WALK)
    if "tram" in lowered:
        modes.add(TravelMode.TRAM)
    if "train" in lowered:
        modes.add(TravelMode.TRAIN)
    if "bus" in lowered or "public" in lowered:
        modes.add(TravelMode.PUBLIC_TRANSPORT)
    if not modes:
        modes.add(TravelMode.PUBLIC_TRANSPORT)
    return frozenset(modes)


def _parse_datetime(date_value: str, time_value: str) -> datetime | None:
    date_part = _parse_date(date_value)
    time_part = _parse_time(time_value)
    if not date_part or not time_part:
        return None
    return datetime.combine(date_part.date(), time_part)


def _parse_date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        # Excel serial date, using Windows 1900 date system.
        serial = float(value)
        return datetime(1899, 12, 30) + _timedelta_days(serial)
    except ValueError:
        return None


def _parse_time(value: str) -> time | None:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            pass
    try:
        serial = float(value)
        seconds = round((serial % 1) * 24 * 60 * 60)
        return time(seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    except ValueError:
        return None


def _timedelta_days(days: float):
    from datetime import timedelta

    return timedelta(days=days)


def _best_service_user(
    name: str,
    service_users: dict[str, ServiceUser],
    aliases: dict[str, dict[str, str]] | None = None,
) -> ServiceUser | None:
    target = _identity_key(name)
    target = (aliases or {}).get("service_user_aliases", {}).get(target, target)
    for service_user in service_users.values():
        key = _identity_key(service_user.name)
        if key == target or key in target or target in key:
            return service_user
    return None


def _best_worker(name: str, workers: dict[str, Worker]) -> Worker | None:
    target = _identity_key(name)
    for worker in workers.values():
        key = _identity_key(worker.name)
        if key == target or key in target or target in key:
            return worker
    return None


def _mentioned_service_users(
    text: str,
    service_users: dict[str, ServiceUser],
    aliases: dict[str, dict[str, str]],
) -> list[ServiceUser]:
    text_key = _identity_key(text)
    matched = []
    for service_user in service_users.values():
        keys = {_identity_key(service_user.name)}
        keys.update(
            alias
            for alias, canonical in aliases.get("service_user_aliases", {}).items()
            if canonical == _identity_key(service_user.name)
        )
        if any(key and key in text_key for key in keys):
            matched.append(service_user)
    return matched


def _worker_mentioned(worker: Worker, text: str, aliases: dict[str, dict[str, str]]) -> bool:
    text_key = _identity_key(text)
    keys = {_identity_key(worker.name)}
    keys.update(
        alias
        for alias, canonical in aliases.get("worker_aliases", {}).items()
        if canonical == _identity_key(worker.name)
    )
    return any(key and key in text_key for key in keys)


def _load_aliases(path: str | Path | None) -> dict[str, dict[str, str]]:
    if not path:
        path = Path(__file__).resolve().parents[4] / "config" / "aliases.json"
    path = Path(path)
    if not path.exists():
        return {"service_user_aliases": {}, "worker_aliases": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        "service_user_aliases": _flatten_aliases(raw.get("service_user_aliases", {})),
        "worker_aliases": _flatten_aliases(raw.get("worker_aliases", {})),
    }


def _flatten_aliases(raw: dict[str, list[str]]) -> dict[str, str]:
    flattened = {}
    for canonical, aliases in raw.items():
        canonical_key = _identity_key(canonical)
        flattened[canonical_key] = canonical_key
        for alias in aliases:
            flattened[_identity_key(alias)] = canonical_key
    return flattened


def _compact_text(text: str, max_length: int = 1200) -> str:
    compacted = re.sub(r"\s+", " ", text).strip()
    return compacted if len(compacted) <= max_length else compacted[: max_length - 3] + "..."


def _best_name_match(name: str, rows: dict[str, dict[str, str]]) -> dict[str, str] | None:
    target = _identity_key(name)
    if target in rows:
        return rows[target]
    for key, row in rows.items():
        if key in target or target in key:
            return row
    return None


def _name_mentioned(name: str, text: str) -> bool:
    if not name or not text:
        return False
    return _identity_key(name) in _identity_key(text)


def _identity_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _value(row: dict[str, str], key: str) -> str:
    target = _identity_key(key)
    for row_key, value in row.items():
        if _identity_key(row_key) == target:
            return value.strip()
    return ""


def _yes(value: str) -> bool:
    return value.strip().lower() in {"y", "yes", "true", "1"}


def _postcode_area(postcode: str | None) -> str | None:
    if not postcode:
        return None
    outward = postcode.strip().upper().split()[0]
    letters = []
    for char in outward:
        if char.isalpha():
            letters.append(char)
        else:
            break
    return "".join(letters) or outward
