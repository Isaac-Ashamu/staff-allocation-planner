"""Parsers for family date request files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
import re
from xml.etree import ElementTree
from zipfile import ZipFile

from ..models import AvailabilityWindow


MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

DAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "mondays": 0,
    "tuesdays": 1,
    "wednesdays": 2,
    "thursdays": 3,
    "fridays": 4,
    "saturdays": 5,
    "sundays": 6,
}


@dataclass(frozen=True)
class ParsedDateRequest:
    source_path: str
    subject: str
    body: str
    preferred_windows: tuple[AvailabilityWindow, ...]
    unavailable_windows: tuple[AvailabilityWindow, ...]
    preferred_days: frozenset[int]


def parse_date_request_folder(path: str | Path, default_year: int = 2026, default_month: int = 7) -> list[ParsedDateRequest]:
    path = Path(path)
    if not path.exists():
        return []
    files = [
        file
        for file in sorted(path.rglob("*"))
        if file.is_file() and file.suffix.lower() in {".msg", ".eml", ".docx", ".txt"}
    ]
    return [parse_date_request_file(file, default_year, default_month) for file in files]


def parse_date_request_file(path: str | Path, default_year: int = 2026, default_month: int = 7) -> ParsedDateRequest:
    path = Path(path)
    subject, body = _extract_text(path)
    body = _trim_quoted_history(body)
    search_text = f"{path.stem}\n{subject}\n{body}"
    unavailable_windows = _unavailable_windows(body, default_year, default_month)
    preferred_windows = _preferred_windows(search_text, default_year, default_month, unavailable_windows)
    preferred_days = frozenset(
        day for name, day in DAY_NAMES.items() if re.search(rf"\b{name}\b", search_text, re.I)
    )
    return ParsedDateRequest(
        source_path=str(path),
        subject=subject,
        body=body,
        preferred_windows=tuple(preferred_windows),
        unavailable_windows=tuple(unavailable_windows),
        preferred_days=preferred_days,
    )


def _trim_quoted_history(body: str) -> str:
    markers = [
        "------ Original Message ------",
        "________________________________",
        "From: Bookings",
        "On Wed, 27 May 2026",
        "On Wednesday, May 27",
        "Dear Parent/Carer",
    ]
    cut = len(body)
    lowered = body.lower()
    for marker in markers:
        index = lowered.find(marker.lower())
        if index != -1:
            cut = min(cut, index)
    return body[:cut].strip()


def _extract_text(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix == ".msg":
        try:
            import extract_msg
        except ImportError as exc:
            raise RuntimeError("extract-msg is required to parse .msg date requests") from exc
        message = extract_msg.Message(str(path))
        return message.subject or "", message.body or ""
    if suffix == ".eml":
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        body_part = message.get_body(preferencelist=("plain", "html"))
        return message.get("subject") or "", body_part.get_content() if body_part else ""
    if suffix == ".docx":
        with ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        return path.stem, " ".join(root.itertext())
    return path.stem, path.read_text(encoding="utf-8", errors="ignore")


def _preferred_windows(
    text: str,
    default_year: int,
    default_month: int,
    unavailable_windows: list[AvailabilityWindow],
) -> list[AvailabilityWindow]:
    dates = _mentioned_dates(text, default_year, default_month)
    time_range = _first_time_range(text)
    if not time_range:
        if re.search(r"\b(am|morning)\b", text, re.I):
            time_range = (time(9, 0), time(13, 0))
        elif re.search(r"\b(pm|afternoon)\b", text, re.I):
            time_range = (time(13, 0), time(18, 0))
        else:
            time_range = (time(8, 0), time(23, 0))
    return _dedupe_windows([
        AvailabilityWindow(start=datetime.combine(day, time_range[0]), end=datetime.combine(day, time_range[1]))
        for day in dates
        if not _date_overlaps_windows(day, unavailable_windows)
    ])


def _unavailable_windows(text: str, default_year: int, default_month: int) -> list[AvailabilityWindow]:
    windows: list[AvailabilityWindow] = []
    lower = text.lower()
    if not re.search(r"\b(away|holiday|travelling|traveling|not available|unavailable)\b", lower):
        return windows
    for start, end in _date_ranges(text, default_year, default_month):
        current = start
        while current <= end:
            windows.append(
                AvailabilityWindow(
                    start=datetime.combine(current, time(0, 0)),
                    end=datetime.combine(current, time(23, 59)),
                )
            )
            current += timedelta(days=1)
    if not windows:
        for day in _mentioned_dates(text, default_year, default_month):
            windows.append(
                AvailabilityWindow(
                    start=datetime.combine(day, time(0, 0)),
                    end=datetime.combine(day, time(23, 59)),
                )
            )
    return _dedupe_windows(windows)


def _mentioned_dates(text: str, default_year: int, default_month: int) -> list[date]:
    dates = set()
    for start, end in _date_ranges(text, default_year, default_month):
        if start == end:
            dates.add(start)
    for match in re.finditer(r"\b(?:July|Jul)\s+(\d{1,2})(?:\s*(?:st|nd|rd|th))?\b", text, re.I):
        parsed = _safe_date(default_year, 7, int(match.group(1)))
        if parsed:
            dates.add(parsed)
    for match in re.finditer(r"\b(\d{1,2})(?:\s*(?:st|nd|rd|th))?\s+(?:of\s+)?([A-Za-z]+)\b", text, re.I):
        month = MONTHS.get(match.group(2).lower())
        if month:
            parsed = _safe_date(default_year, month, int(match.group(1)))
            if parsed:
                dates.add(parsed)
    # Bare ordinals in July-specific files.
    if re.search(r"\bjuly\b", text, re.I):
        for match in re.finditer(r"\b(\d{1,2})(?:\s*(?:st|nd|rd|th))\b", text, re.I):
            day = int(match.group(1))
            parsed = _safe_date(default_year, default_month, day)
            if parsed:
                dates.add(parsed)
    return sorted(dates)


def _date_ranges(text: str, default_year: int, default_month: int) -> list[tuple[date, date]]:
    ranges = []
    pattern = re.compile(
        r"\b(?:from\s+)?(\d{1,2})(?:\s*(?:st|nd|rd|th))?(?:\s+(?:of\s+)?([A-Za-z]+))?\s+"
        r"(?:to|-|until)\s+"
        r"(\d{1,2})(?:\s*(?:st|nd|rd|th))?(?:\s+(?:of\s+)?([A-Za-z]+))?\b",
        re.I,
    )
    for match in pattern.finditer(text):
        start_month = MONTHS.get((match.group(2) or "").lower(), default_month)
        end_month = MONTHS.get((match.group(4) or match.group(2) or "").lower(), start_month)
        start = _safe_date(default_year, start_month, int(match.group(1)))
        end = _safe_date(default_year, end_month, int(match.group(3)))
        if not start or not end:
            continue
        if end < start and end_month < start_month:
            end = date(default_year + 1, end_month, int(match.group(3)))
        ranges.append((start, end))
    return ranges



def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _dedupe_windows(windows: list[AvailabilityWindow]) -> list[AvailabilityWindow]:
    seen = set()
    output = []
    for window in windows:
        key = (window.start, window.end)
        if key in seen:
            continue
        seen.add(key)
        output.append(window)
    return output


def _first_time_range(text: str) -> tuple[time, time] | None:
    pattern = re.compile(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|to|till|until)\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|noon)?\b",
        re.I,
    )
    match = pattern.search(text)
    if not match:
        return None
    return (
        _parse_time(match.group(1), match.group(2), match.group(3), default_period=match.group(6)),
        _parse_time(match.group(4), match.group(5), match.group(6), default_period=match.group(3)),
    )


def _parse_time(hour: str, minute: str | None, period: str | None, default_period: str | None = None) -> time:
    hour_int = int(hour)
    minute_int = int(minute or 0)
    period = (period or default_period or "").lower()
    if period == "noon":
        return time(12, 0)
    if period == "pm" and hour_int < 12:
        hour_int += 12
    if period == "am" and hour_int == 12:
        hour_int = 0
    return time(hour_int, minute_int)


def _date_overlaps_windows(day: date, windows: list[AvailabilityWindow]) -> bool:
    return any(window.start.date() <= day <= window.end.date() for window in windows)
