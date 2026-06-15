"""Export recommendations from source files through the SaaS core."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from booking_recommender import (  # noqa: E402
    CachedGeocoder,
    GeocodedHeuristicTravelEstimator,
    HeuristicTravelEstimator,
    OSRMTravelEstimator,
    PostcodesIoGeocoder,
    rank_session_candidates,
)
from booking_recommender.importers import import_workbook_planning_state  # noqa: E402


DEFAULT_IMPORT_WORKBOOK = (
    r"C:\path\to\source-data\booking-import.xlsx"
)
DEFAULT_POSTCODE_WORKBOOK = r".\data\Staff and SU names and postcode.xlsx"
DEFAULT_ADHOCS_WORKBOOK = r".\data\July 2026 Ad hocs.xlsx"
DEFAULT_AVAILABILITY_REPORTS = r".\data\availability report"
DEFAULT_DATE_REQUESTS = (
    r"C:\path\to\source-data\Date requests"
)
DEFAULT_ALIASES = r".\RecommenderSystem\config\aliases.json"
DEFAULT_OUTPUT = r".\RecommenderSystem\output\july-2026-recommendations-python.csv"
DEFAULT_TRAVEL_CACHE = r".\RecommenderSystem\output\travel-cache.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-workbook", default=DEFAULT_IMPORT_WORKBOOK)
    parser.add_argument("--postcode-workbook", default=DEFAULT_POSTCODE_WORKBOOK)
    parser.add_argument("--ad-hocs-workbook", default=DEFAULT_ADHOCS_WORKBOOK)
    parser.add_argument("--availability-reports", default=DEFAULT_AVAILABILITY_REPORTS)
    parser.add_argument("--date-requests", default=DEFAULT_DATE_REQUESTS)
    parser.add_argument("--aliases", default=DEFAULT_ALIASES)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--travel-cache", default=DEFAULT_TRAVEL_CACHE)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument(
        "--travel-provider",
        choices=("heuristic", "geocoded", "osrm"),
        default="geocoded",
        help="Use cached postcode geocoding by default; use heuristic for fully offline runs.",
    )
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
    rows = _recommendation_rows(result.state, estimator, args.top_n)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_fieldnames())
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} recommendations to {output}")
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


def _travel_estimator(provider: str, cache_path: Path):
    if provider == "heuristic":
        return HeuristicTravelEstimator()

    geocoder = CachedGeocoder(provider=PostcodesIoGeocoder(), cache_path=cache_path)
    if provider == "osrm":
        return OSRMTravelEstimator(geocoder=geocoder, cache_path=cache_path)
    return GeocodedHeuristicTravelEstimator(geocoder=geocoder)


def _recommendation_rows(state, estimator, top_n: int) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for session in sorted(state.sessions.values(), key=lambda item: (item.start, item.service_user_id, item.id)):
        if len(session.assigned) >= session.required_worker_count:
            continue
        open_roles = _open_roles(session)
        for role in open_roles:
            recommendations = rank_session_candidates(
                state=state,
                session_id=session.id,
                role=role,
                travel_estimator=estimator,
                top_n=top_n,
            )
            service_user = state.service_users[session.service_user_id]
            for recommendation in recommendations:
                worker = state.workers[recommendation.action.worker_id]
                travel = recommendation.travel_estimate
                rows.append(
                    {
                        "Score": f"{recommendation.score:.3f}",
                        "SessionId": session.id,
                        "BookingDate": session.start.strftime("%d/%m/%Y"),
                        "Start": session.start.strftime("%H:%M"),
                        "Finish": session.end.strftime("%H:%M"),
                        "ServiceUserId": service_user.id,
                        "ServiceUserName": service_user.name,
                        "Slot": role,
                        "WorkerId": worker.id,
                        "WorkerName": worker.name,
                        "TravelMode": travel.mode.value if travel else "",
                        "TravelMinutes": f"{travel.minutes:.1f}" if travel else "",
                        "TravelProvider": travel.provider if travel else "",
                        "Reasons": "; ".join(recommendation.reasons),
                        "Warnings": "; ".join(recommendation.warnings),
                        "Components": "; ".join(
                            f"{key}={value:.2f}" for key, value in recommendation.components.items()
                        ),
                    }
                )
    return rows


def _open_roles(session) -> list[str]:
    assigned_roles = {allocation.role for allocation in session.assigned}
    roles = []
    if "lead" not in assigned_roles:
        roles.append("lead")
    while len(roles) + len(session.assigned) < session.required_worker_count:
        roles.append("second")
    return roles


def _fieldnames() -> list[str]:
    return [
        "Score",
        "SessionId",
        "BookingDate",
        "Start",
        "Finish",
        "ServiceUserId",
        "ServiceUserName",
        "Slot",
        "WorkerId",
        "WorkerName",
        "TravelMode",
        "TravelMinutes",
        "TravelProvider",
        "Reasons",
        "Warnings",
        "Components",
    ]


if __name__ == "__main__":
    raise SystemExit(main())
