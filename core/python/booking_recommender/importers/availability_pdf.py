"""Parser for availability report PDFs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
import re

from ..models import AvailabilityWindow


DATE_LINE_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*(\d{1,2})$")
MONTH_LINE_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{4})$")
STATUS_RE = re.compile(
    r"^(?P<name>.*?)(?P<unavailable>YES|NO)\s+(?P<all_day>YES|NO)(?:\s+(?P<times>.*))?$"
)
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


@dataclass(frozen=True)
class AvailabilityRecord:
    worker_name: str
    day: date
    unavailable: bool
    all_day: bool
    windows: tuple[AvailabilityWindow, ...]
    source_ref: str


def parse_availability_reports(path: str | Path) -> list[AvailabilityRecord]:
    path = Path(path)
    if not path.exists():
        return []
    if path.is_file():
        return parse_availability_report(path)

    records: list[AvailabilityRecord] = []
    for pdf in sorted(path.glob("*.pdf")):
        records.extend(parse_availability_report(pdf))
    return records


def parse_availability_report(path: str | Path) -> list[AvailabilityRecord]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is required to parse availability report PDFs") from exc

    path = Path(path)
    reader = PdfReader(path)
    records: list[AvailabilityRecord] = []
    current_day: date | None = None
    pending_day_number: int | None = None
    name_parts: list[str] = []

    for page_index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        for raw_line in text.splitlines():
            line = _clean_line(raw_line)
            if not line or _is_header_line(line):
                continue

            date_match = DATE_LINE_RE.match(line)
            if date_match:
                pending_day_number = int(date_match.group(2))
                name_parts = []
                continue

            month_match = MONTH_LINE_RE.match(line)
            if month_match and pending_day_number is not None:
                current_day = date(
                    int(month_match.group(2)),
                    _month_number(month_match.group(1)),
                    pending_day_number,
                )
                pending_day_number = None
                name_parts = []
                continue

            if current_day is None:
                continue

            status_match = STATUS_RE.match(line)
            if status_match:
                inline_name = status_match.group("name").strip()
                parts = name_parts + ([inline_name] if inline_name else [])
                worker_name = _clean_worker_name(" ".join(parts))
                name_parts = []
                if not worker_name:
                    continue

                windows = _availability_windows(
                    current_day=current_day,
                    unavailable=status_match.group("unavailable") == "YES",
                    all_day=status_match.group("all_day") == "YES",
                    times=status_match.group("times") or "",
                )
                records.append(
                    AvailabilityRecord(
                        worker_name=worker_name,
                        day=current_day,
                        unavailable=status_match.group("unavailable") == "YES",
                        all_day=status_match.group("all_day") == "YES",
                        windows=windows,
                        source_ref=f"{path}#page={page_index}",
                    )
                )
                continue

            name_parts.append(line)

    return records


def _availability_windows(
    *,
    current_day: date,
    unavailable: bool,
    all_day: bool,
    times: str,
) -> tuple[AvailabilityWindow, ...]:
    if unavailable:
        return ()
    if all_day:
        return (
            AvailabilityWindow(
                start=datetime.combine(current_day, time(8, 0)),
                end=datetime.combine(current_day, time(23, 0)),
            ),
        )

    parsed_times = [time(int(hour), int(minute)) for hour, minute in TIME_RE.findall(times)]
    windows = []
    for index in range(0, len(parsed_times) - 1, 2):
        start = datetime.combine(current_day, parsed_times[index])
        end = datetime.combine(current_day, parsed_times[index + 1])
        if start < end:
            windows.append(AvailabilityWindow(start=start, end=end))
    return tuple(windows)


def _clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.replace("\u00a0", " ")).strip()


def _clean_worker_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("- ", "-")).strip()


def _is_header_line(line: str) -> bool:
    lowered = line.lower()
    return lowered in {
        "availability reporting",
        "day support worker unavailable",
        "all day",
        "(8am -",
        "11pm)",
        "free",
        "from... to ...",
        "free from... to ...",
        "to ...",
    } or lowered.startswith("for the month:")


def _month_number(month: str) -> int:
    return {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }[month]

