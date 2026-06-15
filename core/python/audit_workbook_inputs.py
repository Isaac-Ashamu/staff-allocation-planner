"""Export data-quality and date-request audit artifacts."""

from __future__ import annotations

import csv
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from booking_recommender.importers import import_workbook_planning_state  # noqa: E402
from booking_recommender.importers.date_requests import parse_date_request_folder  # noqa: E402
from export_workbook_recommendations import (  # noqa: E402
    DEFAULT_ADHOCS_WORKBOOK,
    DEFAULT_ALIASES,
    DEFAULT_AVAILABILITY_REPORTS,
    DEFAULT_DATE_REQUESTS,
    DEFAULT_IMPORT_WORKBOOK,
    DEFAULT_POSTCODE_WORKBOOK,
)


OUTPUT_DIR = Path(r".\RecommenderSystem\output")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result = import_workbook_planning_state(
        import_workbook=DEFAULT_IMPORT_WORKBOOK,
        postcode_workbook=DEFAULT_POSTCODE_WORKBOOK,
        ad_hocs_workbook=DEFAULT_ADHOCS_WORKBOOK,
        availability_reports=DEFAULT_AVAILABILITY_REPORTS,
        date_requests_path=DEFAULT_DATE_REQUESTS,
        aliases_path=DEFAULT_ALIASES,
    )
    _write_warnings(result.warnings)
    _write_date_requests()
    _write_worker_availability_summary(result.state)
    print(f"Wrote audit outputs to {OUTPUT_DIR}")
    return 0


def _write_warnings(warnings: tuple[str, ...]) -> None:
    with (OUTPUT_DIR / "data-quality-warnings.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Severity", "Warning"])
        writer.writeheader()
        for warning in warnings:
            writer.writerow({"Severity": _severity(warning), "Warning": warning})


def _write_date_requests() -> None:
    parsed = parse_date_request_folder(DEFAULT_DATE_REQUESTS)
    with (OUTPUT_DIR / "date-requests-parsed.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "Source",
                "Subject",
                "PreferredWindows",
                "UnavailableWindows",
                "PreferredDays",
                "BodyPreview",
            ],
        )
        writer.writeheader()
        for item in parsed:
            writer.writerow(
                {
                    "Source": item.source_path,
                    "Subject": item.subject,
                    "PreferredWindows": "; ".join(f"{w.start.isoformat()} to {w.end.isoformat()}" for w in item.preferred_windows),
                    "UnavailableWindows": "; ".join(f"{w.start.isoformat()} to {w.end.isoformat()}" for w in item.unavailable_windows),
                    "PreferredDays": ", ".join(str(day) for day in sorted(item.preferred_days)),
                    "BodyPreview": " ".join(item.body.split())[:500],
                }
            )


def _write_worker_availability_summary(state) -> None:
    with (OUTPUT_DIR / "worker-availability-summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["WorkerId", "WorkerName", "AvailabilityWindows", "UnavailableWindows"],
        )
        writer.writeheader()
        for worker in sorted(state.workers.values(), key=lambda item: item.name):
            writer.writerow(
                {
                    "WorkerId": worker.id,
                    "WorkerName": worker.name,
                    "AvailabilityWindows": len(worker.availability),
                    "UnavailableWindows": len(worker.unavailability),
                }
            )


def _severity(warning: str) -> str:
    lowered = warning.lower()
    if "not matched" in lowered or "could not match" in lowered:
        return "review"
    if "not found" in lowered or "required" in lowered:
        return "error"
    return "info"


if __name__ == "__main__":
    raise SystemExit(main())

