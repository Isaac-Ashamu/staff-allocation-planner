"""Create a provisional sequential allocation plan from source files."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from booking_recommender import greedy_allocate_open_sessions  # noqa: E402
from booking_recommender.importers import import_workbook_planning_state  # noqa: E402
from export_workbook_recommendations import (  # noqa: E402
    DEFAULT_ADHOCS_WORKBOOK,
    DEFAULT_IMPORT_WORKBOOK,
    DEFAULT_POSTCODE_WORKBOOK,
    DEFAULT_AVAILABILITY_REPORTS,
    DEFAULT_DATE_REQUESTS,
    DEFAULT_TRAVEL_CACHE,
    _travel_estimator,
)


DEFAULT_OUTPUT = r".\RecommenderSystem\output\july-2026-greedy-plan.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-workbook", default=DEFAULT_IMPORT_WORKBOOK)
    parser.add_argument("--postcode-workbook", default=DEFAULT_POSTCODE_WORKBOOK)
    parser.add_argument("--ad-hocs-workbook", default=DEFAULT_ADHOCS_WORKBOOK)
    parser.add_argument("--availability-reports", default=DEFAULT_AVAILABILITY_REPORTS)
    parser.add_argument("--date-requests", default=DEFAULT_DATE_REQUESTS)
    parser.add_argument("--aliases", default=r".\RecommenderSystem\config\aliases.json")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--travel-cache", default=DEFAULT_TRAVEL_CACHE)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--travel-provider", choices=("heuristic", "geocoded", "osrm"), default="geocoded")
    args = parser.parse_args()

    result = import_workbook_planning_state(
        import_workbook=args.import_workbook,
        postcode_workbook=args.postcode_workbook,
        ad_hocs_workbook=args.ad_hocs_workbook,
        availability_reports=args.availability_reports,
        date_requests_path=args.date_requests,
        aliases_path=args.aliases,
    )
    estimator = _travel_estimator(args.travel_provider, Path(args.travel_cache))
    plan = greedy_allocate_open_sessions(result.state, estimator, top_n=args.top_n)

    rows = []
    for item in plan.assignments:
        rec = item.recommendation
        session = result.state.sessions.get(rec.action.session_id) or plan.final_state.sessions.get(rec.action.session_id)
        service_user = result.state.service_users.get(session.service_user_id) if session else None
        worker = result.state.workers.get(rec.action.worker_id)
        travel = rec.travel_estimate
        rows.append(
            {
                "Accepted": str(item.accepted),
                "Reason": item.reason,
                "Score": f"{rec.score:.3f}",
                "SessionId": rec.action.session_id,
                "BookingDate": session.start.strftime("%d/%m/%Y") if session else "",
                "Start": session.start.strftime("%H:%M") if session else "",
                "Finish": session.end.strftime("%H:%M") if session else "",
                "ServiceUserName": service_user.name if service_user else "",
                "Role": rec.action.role,
                "WorkerId": rec.action.worker_id,
                "WorkerName": worker.name if worker else "",
                "TravelMode": travel.mode.value if travel else "",
                "TravelMinutes": f"{travel.minutes:.1f}" if travel else "",
                "Reasons": "; ".join(rec.reasons),
                "Warnings": "; ".join(rec.warnings),
                "HardFailures": "; ".join(rec.hard_constraint_failures),
            }
        )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_fieldnames())
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} planned assignment decisions to {output}")
    print(
        "Imported "
        f"{len(result.state.workers)} workers, "
        f"{len(result.state.service_users)} service users, "
        f"{len(result.state.sessions)} sessions, "
        f"{len(result.state.family_requests)} family/ad hoc requests."
    )
    for warning in result.warnings[:20]:
        print(f"WARNING: {warning}")
    if len(result.warnings) > 20:
        print(f"WARNING: {len(result.warnings) - 20} more warnings omitted.")
    return 0


def _fieldnames() -> list[str]:
    return [
        "Accepted",
        "Reason",
        "Score",
        "SessionId",
        "BookingDate",
        "Start",
        "Finish",
        "ServiceUserName",
        "Role",
        "WorkerId",
        "WorkerName",
        "TravelMode",
        "TravelMinutes",
        "Reasons",
        "Warnings",
        "HardFailures",
    ]


if __name__ == "__main__":
    raise SystemExit(main())
