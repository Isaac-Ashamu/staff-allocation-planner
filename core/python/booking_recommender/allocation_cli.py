"""Command-line entrypoint for end-to-end allocation planning."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from .allocation_report import (
    allocation_report_to_dict,
    build_allocation_report,
    render_allocation_report,
)
from .importer import StandardizedImportError, load_standardized_csv
from .persistence import persist_allocation_report
from .provisioning import provision_sessions_from_family_requests
from .travel import (
    FallbackTravelEstimator,
    TravelConfig,
    TravelRunStats,
    build_live_travel_estimator,
    prefetch_state_postcodes,
)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a proposed allocation plan from a standardized CSV v1 folder."
    )
    parser.add_argument("folder", help="Folder containing import_manifest.json and standardized CSV files.")
    parser.add_argument(
        "--output-dir",
        help="Derived output folder for the planning snapshot, proposed plan, recommendations, and report.",
    )
    parser.add_argument("--top-n", type=int, default=5, help="Number of candidates to retain per planning decision.")
    parser.add_argument(
        "--include-infeasible",
        action="store_true",
        help="Include candidates that failed hard constraints in recommendation details.",
    )
    parser.add_argument(
        "--compare-single-session",
        action="store_true",
        help="Include one-session rankings for comparison with the full greedy allocation plan.",
    )
    parser.add_argument(
        "--audit-db",
        help=(
            "Optional audit database for persisted snapshots, recommendations, and decisions: "
            "a SQLite file path, or a postgresql://... DSN."
        ),
    )
    parser.add_argument(
        "--tenant-id",
        help="Optional tenant ID override for audit persistence. Defaults to tenant rules from the import.",
    )
    parser.add_argument(
        "--provision-from-family-requests",
        action="store_true",
        help=(
            "Create sessions for family/ad hoc requests with no matching session in the period, when "
            "at least one worker looks feasible for a candidate day/time. Off by default: without this "
            "flag, unmatched requests stay inert preference data as they do today."
        ),
    )
    parser.add_argument(
        "--restrict-male-worker-female-service-user",
        action="store_true",
        help=(
            "Tenant policy hard constraint: never suggest a male worker for a female service user. "
            "Off by default -- this is a tenant-specific convention, not a universal rule."
        ),
    )
    add_live_travel_arguments(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        import_result = load_standardized_csv(args.folder)
    except StandardizedImportError as error:
        print("Standardized CSV import failed")
        for issue in error.report.issues:
            print(f"- {issue.severity}:{issue.code}: {issue.message}")
        return 1

    state = import_result.state
    if args.restrict_male_worker_female_service_user:
        state = replace(
            state,
            tenant_rules=replace(state.tenant_rules, restrict_male_workers_from_female_service_users=True),
        )

    travel_estimator, travel_stats = build_travel_estimator_from_args(args, state)

    provisioning = None
    if args.provision_from_family_requests:
        provisioning = provision_sessions_from_family_requests(
            state, travel_estimator or FallbackTravelEstimator()
        )
        print(
            f"Provisioned {len(provisioning.created)} session(s) from family requests "
            f"({len(provisioning.unscheduled)} request-week(s) could not be scheduled)"
        )

    report = build_allocation_report(
        source_folder=args.folder,
        state=state,
        import_report=import_result.report,
        travel_estimator=travel_estimator,
        top_n=max(1, args.top_n),
        include_infeasible=args.include_infeasible,
        compare_single_session=args.compare_single_session,
        provisioning=provisioning,
    )
    rendered = render_allocation_report(report)
    output_dir = _output_dir(args.output_dir, args.folder, report)
    written = write_allocation_outputs(report, rendered, output_dir)

    print(rendered, end="")
    if travel_stats is not None:
        print(render_travel_stats(travel_stats))
    print(f"Saved derived outputs to {output_dir}")
    for path in written:
        print(f"- {path.name}")
    if args.audit_db:
        persisted = persist_allocation_report(args.audit_db, report, tenant_id=args.tenant_id)
        print(f"Saved audit records to {args.audit_db}")
        print(f"- planning_snapshot_id: {persisted.planning_snapshot_id}")
        print(f"- recommendations: {len(persisted.recommendation_ids)}")
    return 0


def write_allocation_outputs(report, rendered: str, output_dir: Path) -> tuple[Path, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    data = allocation_report_to_dict(report)
    files = {
        "allocation_report.json": data,
        "planning_snapshot.json": data["planning_snapshot"],
        "proposed_plan.json": data["proposed_plan"],
        "recommendation_details.json": data["recommendation_details"],
    }
    written = []
    for filename, payload in files.items():
        path = output_dir / filename
        with path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
            file.write("\n")
        written.append(path)

    text_path = output_dir / "allocation_report.txt"
    text_path.write_text(rendered, encoding="utf-8")
    written.append(text_path)
    return tuple(written)


def add_live_travel_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--live-travel",
        action="store_true",
        help=(
            "Geocode postcodes via postcodes.io and route with OpenRouteService (ORS_API_KEY) or "
            "OSRM (OSRM_BASE_URL). Without credentials this still geocodes and uses postcode distance. "
            "Off by default so planning never calls external services unless asked."
        ),
    )
    parser.add_argument(
        "--travel-cache-db",
        help="SQLite file for persistent travel-estimate and postcode-geocode caches (used with --live-travel).",
    )
    parser.add_argument(
        "--max-external-route-calls",
        type=int,
        help=(
            "Override the per-run cap on metered routing API calls -- OpenRouteService, "
            "Google Routes -- (default: 100). Does not limit self-hosted OSRM, which has "
            "no quota to protect. Only takes effect with --live-travel."
        ),
    )


def build_travel_estimator_from_args(args: argparse.Namespace, state):
    """Return (estimator, stats). Both None when --live-travel is not set (deterministic fallback)."""
    if not getattr(args, "live_travel", False):
        return None, None
    stats = TravelRunStats()
    tenant_id = getattr(args, "tenant_id", None) or state.tenant_rules.tenant_id
    cache_db = getattr(args, "travel_cache_db", None)
    config = None
    max_calls = getattr(args, "max_external_route_calls", None)
    if max_calls is not None:
        config = TravelConfig(max_external_route_calls_per_run=max_calls)
    estimator = build_live_travel_estimator(
        tenant_id=tenant_id,
        travel_cache_db=cache_db,
        geocode_cache_db=cache_db,
        config=config,
        stats=stats,
    )
    prefetched = prefetch_state_postcodes(state, estimator.geocoder)
    order = ", ".join(estimator.config.travel_provider_order)
    print(f"Live travel enabled: provider order {order}; geocoded {prefetched} new postcodes")
    return estimator, stats


def render_travel_stats(stats: TravelRunStats) -> str:
    counts = stats.to_dict()
    live = (
        counts["estimates_from_ors"] + counts["estimates_from_osrm"] + counts["estimates_from_google"]
        + counts["estimates_from_gtfs_transit"]
    )
    return (
        "Travel estimates: "
        f"{live} live routed ({counts['estimates_from_ors']} ORS, {counts['estimates_from_osrm']} OSRM, "
        f"{counts['estimates_from_gtfs_transit']} GTFS transit), "
        f"{counts['estimates_from_postcode_distance']} postcode-distance, "
        f"{counts['estimates_from_fallback']} fallback, {counts['estimates_from_cache']} cached, "
        f"{counts['provider_failures']} provider failures, {counts['external_route_calls']} external calls"
    )


def _output_dir(value: str | None, source_folder: str, report) -> Path:
    if value:
        return Path(value)
    import_run_id = report.plan.final_state.import_audit.import_run_id if report.plan.final_state.import_audit else "manual"
    safe_run_id = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in import_run_id)
    return Path.cwd() / "allocation_outputs" / safe_run_id


if __name__ == "__main__":
    raise SystemExit(main())
