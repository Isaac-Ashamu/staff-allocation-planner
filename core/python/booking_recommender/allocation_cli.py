"""Command-line entrypoint for end-to-end allocation planning."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

from .allocation_report import (
    allocation_report_to_dict,
    build_allocation_report,
    render_allocation_report,
)
from .importer import StandardizedImportError, load_standardized_csv


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
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        import_result = load_standardized_csv(args.folder)
    except StandardizedImportError as error:
        print("Standardized CSV import failed")
        for issue in error.report.issues:
            print(f"- {issue.severity}:{issue.code}: {issue.message}")
        return 1

    report = build_allocation_report(
        source_folder=args.folder,
        state=import_result.state,
        import_report=import_result.report,
        top_n=max(1, args.top_n),
        include_infeasible=args.include_infeasible,
        compare_single_session=args.compare_single_session,
    )
    rendered = render_allocation_report(report)
    output_dir = _output_dir(args.output_dir, args.folder, report)
    written = write_allocation_outputs(report, rendered, output_dir)

    print(rendered, end="")
    print(f"Saved derived outputs to {output_dir}")
    for path in written:
        print(f"- {path.name}")
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


def _output_dir(value: str | None, source_folder: str, report) -> Path:
    if value:
        return Path(value)
    import_run_id = report.plan.final_state.import_audit.import_run_id if report.plan.final_state.import_audit else "manual"
    safe_run_id = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in import_run_id)
    return Path.cwd() / "allocation_outputs" / safe_run_id


if __name__ == "__main__":
    raise SystemExit(main())
