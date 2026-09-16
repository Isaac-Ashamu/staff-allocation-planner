"""Local review UI for proposed allocation plans.

The production roadmap calls for a full web app. This module keeps Phase 8
dependency-light by serving an operational HTML dashboard from the existing
standardized importer, allocation report, and audit persistence layers.
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urlparse

from .allocation_report import (
    AllocationReport,
    FallbackTravelEstimator,
    allocation_report_to_dict,
    build_allocation_report,
)
from .allocation_cli import add_live_travel_arguments, build_travel_estimator_from_args
from .importer import StandardizedImportError, load_standardized_csv
from .persistence import (
    PersistenceError,
    initialize_database,
    persist_allocation_report,
    record_human_decision,
)


@dataclass(frozen=True)
class PreparedReviewRun:
    report: AllocationReport
    database_path: Path
    planning_snapshot_id: str
    recommendation_ids: tuple[str, ...]


def prepare_review_run(
    source_folder: str | Path,
    *,
    audit_db: str | Path,
    tenant_id: str | None = None,
    top_n: int = 5,
    include_infeasible: bool = True,
    travel_estimator=None,
) -> PreparedReviewRun:
    """Load source data, build a proposed plan, and persist it for review."""

    import_result = load_standardized_csv(source_folder)
    report = build_allocation_report(
        source_folder=source_folder,
        state=import_result.state,
        import_report=import_result.report,
        travel_estimator=travel_estimator or FallbackTravelEstimator(),
        top_n=top_n,
        include_infeasible=include_infeasible,
        compare_single_session=True,
    )
    database_path = Path(audit_db)
    persisted = persist_allocation_report(database_path, report, tenant_id=tenant_id)
    return PreparedReviewRun(
        report=report,
        database_path=database_path,
        planning_snapshot_id=persisted.planning_snapshot_id,
        recommendation_ids=persisted.recommendation_ids,
    )


def build_review_model(
    report: AllocationReport,
    *,
    database_path: str | Path | None = None,
    filters: dict[str, str] | None = None,
) -> dict:
    """Build a dashboard model from an allocation report and optional audit DB."""

    data = allocation_report_to_dict(report)
    snapshot = data["planning_snapshot"]
    sessions = snapshot["sessions"]
    workers = snapshot["workers"]
    service_users = snapshot["service_users"]
    recommendation_rows = _recommendation_rows(database_path, data["run"]["import_run_id"])
    decision_rows = _decision_rows(database_path, data["run"]["import_run_id"])
    latest_decisions = _latest_decisions_by_recommendation(decision_rows)
    candidate_lookup = _candidate_lookup(recommendation_rows)

    details_by_session: dict[str, list[dict]] = {}
    for detail in data["recommendation_details"]:
        session_id = detail["recommendation"]["session_id"]
        details_by_session.setdefault(session_id, []).append(detail)

    import_warnings = [
        issue
        for issue in data["import_report"]["issues"]
        if issue["severity"] in {"warning", "error"}
    ]
    session_cards = []
    for session_id, session in sorted(sessions.items(), key=lambda item: (item[1]["start"], item[0])):
        service_user = service_users[session["service_user_id"]]
        assigned = session.get("assigned") or []
        remaining = max(0, int(session["required_worker_count"]) - len(assigned))
        status = _plan_status(session, remaining)
        details = details_by_session.get(session_id, [])
        candidates = _candidates_for_session(
            session_id=session_id,
            report_details=details,
            db_lookup=candidate_lookup,
            workers=workers,
            latest_decisions=latest_decisions,
        )
        warnings = _session_warnings(
            session=session,
            service_user=service_user,
            details=details,
            import_warnings=import_warnings,
        )
        review_state = _review_state(candidates, warnings)
        priority_band = "high" if float(session["priority"]) >= 1.5 else "normal"
        session_cards.append(
            {
                "session_id": session_id,
                "service_user_id": session["service_user_id"],
                "service_user_name": service_user["name"],
                "start": session["start"],
                "end": session["end"],
                "date": session["start"][:10],
                "location": session["location"],
                "required_worker_count": session["required_worker_count"],
                "assigned_workers": [
                    {
                        "worker_id": item["worker_id"],
                        "worker_name": workers.get(item["worker_id"], {}).get("name", item["worker_id"]),
                        "role": item["role"],
                    }
                    for item in assigned
                ],
                "remaining_worker_count": remaining,
                "requires_driver": session["requires_driver"],
                "priority": session["priority"],
                "priority_band": priority_band,
                "status": status,
                "review_state": review_state,
                "warnings": warnings,
                "candidates": candidates,
            }
        )

    filtered_sessions = _apply_filters(session_cards, filters or {})
    page_size = _positive_int((filters or {}).get("page_size"), default=25, maximum=100)
    page = _positive_int((filters or {}).get("page"), default=1, maximum=10_000)
    paged_sessions, pagination = _paginate(filtered_sessions, page=page, page_size=page_size)
    approved_session_ids = {
        candidate["session_id"]
        for candidate in _all_candidates(session_cards)
        if candidate.get("decision_type") in {"accept", "override"}
    }
    summary = {
        "sessions": len(session_cards),
        "allocated": sum(1 for item in session_cards if item["status"] == "allocated"),
        "partially_allocated": sum(1 for item in session_cards if item["status"] == "partially_allocated"),
        "unfilled": sum(1 for item in session_cards if item["remaining_worker_count"] > 0),
        "warning": sum(1 for item in session_cards if item["warnings"]),
        "approved": len(approved_session_ids),
        "visible": len(filtered_sessions),
        "showing": len(paged_sessions),
    }

    worker_day_view = _worker_day_view(paged_sessions)
    return {
        "run": data["run"],
        "summary": summary,
        "filters": filters or {},
        "pagination": pagination,
        "sessions": paged_sessions,
        "workers": workers,
        "service_users": service_users,
        "import_warnings": import_warnings,
        "data_quality_warnings": snapshot["data_quality_warnings"],
        "worker_day_view": worker_day_view,
        "decision_count": len(decision_rows),
        "database_path": str(database_path) if database_path else "",
    }


def submit_review_decision(
    database_path: str | Path,
    *,
    recommendation_id: str,
    decision_type: str,
    actor: str,
    selected_worker_id: str | None = None,
    override_reason: str | None = None,
    notes: str = "",
) -> str:
    """Validate and persist a UI decision."""

    normalized = decision_type.strip().lower()
    reason = (override_reason or "").strip()
    if normalized in {"reject", "override"} and not reason:
        raise PersistenceError("override_reason is required for reject or override decisions")
    return record_human_decision(
        database_path,
        recommendation_id=recommendation_id,
        decision_type=normalized,
        actor=actor,
        selected_worker_id=selected_worker_id,
        override_reason=reason or None,
        notes=notes,
    )


def render_review_dashboard(model: dict, *, message: str = "", error: str = "") -> str:
    """Render the operational dashboard as a complete HTML document."""

    cards = "\n".join(_render_session_card(session) for session in model["sessions"])
    if not cards:
        cards = '<p class="empty">No sessions match the current filters.</p>'
    import_warnings = "\n".join(
        f"<li>{_h(issue['code'])}: {_h(issue['message'])}</li>"
        for issue in model["import_warnings"]
    ) or '<li class="muted">No import warnings.</li>'
    data_quality = "\n".join(f"<li>{_h(item)}</li>" for item in model["data_quality_warnings"])
    if not data_quality:
        data_quality = '<li class="muted">No state-level data-quality warnings.</li>'
    worker_day = "\n".join(_render_worker_day(item) for item in model["worker_day_view"])
    if not worker_day:
        worker_day = '<p class="empty">No assigned worker routes in the current filter.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Allocation Review</title>
  <style>{_CSS}</style>
</head>
<body>
  <header>
    <div>
      <p class="eyebrow">Allocation review</p>
      <h1>Proposed plan dashboard</h1>
      <p class="subtle">Import run {_h(model['run']['import_run_id'] or 'unknown')}</p>
    </div>
    <div class="audit-pill">Audit DB: {_h(model['database_path'] or 'not connected')}</div>
  </header>
  {_render_notice(message, 'notice')}
  {_render_notice(error, 'error')}
  <section class="summary" aria-label="Whole plan status">
    {_metric('Allocated', model['summary']['allocated'])}
    {_metric('Partial', model['summary']['partially_allocated'])}
    {_metric('Unfilled', model['summary']['unfilled'])}
    {_metric('Warnings', model['summary']['warning'])}
    {_metric('Approved', model['summary']['approved'])}
    {_metric('Visible', model['summary']['visible'])}
  </section>
  {_render_filters(model['filters'])}
  <main class="layout">
    <section>
      <div class="section-title">
        <h2>Session queue</h2>
        <span>{model['summary']['showing']} shown · {model['summary']['visible']} matched · {model['summary']['sessions']} total</span>
      </div>
      {_render_pagination(model['pagination'], model['filters'])}
      {cards}
      {_render_pagination(model['pagination'], model['filters'])}
    </section>
    <aside>
      <section class="side-panel">
        <h2>Worker day / route view</h2>
        {worker_day}
      </section>
      <section class="side-panel">
        <h2>Data-quality warnings</h2>
        <ul>{data_quality}</ul>
      </section>
      <section class="side-panel">
        <h2>Import warnings</h2>
        <ul>{import_warnings}</ul>
      </section>
    </aside>
  </main>
</body>
</html>
"""


def serve_review_ui(
    report: AllocationReport,
    *,
    database_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> ThreadingHTTPServer:
    """Create a configured HTTP server. Call ``serve_forever`` to run it."""

    database = Path(database_path)
    initialize_database(database)

    class ReviewHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/":
                self.send_error(404)
                return
            filters = _first_values(parse_qs(parsed.query))
            model = build_review_model(report, database_path=database, filters=filters)
            body = render_review_dashboard(model, message=filters.get("message", ""), error=filters.get("error", ""))
            _send_html(self, body)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/decisions":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length).decode("utf-8")
            form = _first_values(parse_qs(payload))
            try:
                submit_review_decision(
                    database,
                    recommendation_id=form.get("recommendation_id", ""),
                    decision_type=form.get("decision_type", ""),
                    actor=form.get("actor", ""),
                    selected_worker_id=form.get("selected_worker_id") or None,
                    override_reason=form.get("override_reason") or None,
                    notes=form.get("notes", ""),
                )
            except PersistenceError as exc:
                self.send_response(303)
                self.send_header("Location", "/?" + urlencode({"error": str(exc)}))
                self.end_headers()
                return
            self.send_response(303)
            self.send_header("Location", "/?" + urlencode({"message": "Decision recorded"}))
            self.end_headers()

        def log_message(self, format: str, *args) -> None:
            return

    return ThreadingHTTPServer((host, port), ReviewHandler)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve a local review UI for a standardized CSV allocation plan.")
    parser.add_argument("folder", help="Folder containing standardized CSV v1 files.")
    parser.add_argument("--audit-db", required=True, help="SQLite audit database path for decisions.")
    parser.add_argument("--tenant-id", help="Optional tenant ID override for audit persistence.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=8787, help="Port to bind.")
    parser.add_argument("--top-n", type=int, default=5, help="Number of ranked candidates to retain.")
    parser.add_argument(
        "--feasible-only",
        action="store_true",
        help="Hide hard-failed candidates from the generated review run.",
    )
    add_live_travel_arguments(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        travel_estimator = None
        if args.live_travel:
            import_result = load_standardized_csv(args.folder)
            travel_estimator, _ = build_travel_estimator_from_args(args, import_result.state)
        prepared = prepare_review_run(
            args.folder,
            audit_db=args.audit_db,
            tenant_id=args.tenant_id,
            top_n=max(1, args.top_n),
            include_infeasible=not args.feasible_only,
            travel_estimator=travel_estimator,
        )
    except StandardizedImportError as error:
        print("Standardized CSV import failed")
        for issue in error.report.issues:
            print(f"- {issue.severity}:{issue.code}: {issue.message}")
        return 1

    server = serve_review_ui(
        prepared.report,
        database_path=prepared.database_path,
        host=args.host,
        port=args.port,
    )
    print(f"Review UI running at http://{args.host}:{args.port}/")
    print(f"Audit database: {prepared.database_path}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nReview UI stopped.")
    finally:
        server.server_close()
    return 0


def _recommendation_rows(database_path: str | Path | None, import_run_id: str | None) -> list[dict]:
    if not database_path or not import_run_id:
        return []
    connection = sqlite3.connect(database_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT *
            FROM recommendations
            WHERE import_run_id = ?
            ORDER BY session_id, decision_index, rank_position, id
            """,
            (import_run_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()


def _decision_rows(database_path: str | Path | None, import_run_id: str | None) -> list[dict]:
    if not database_path or not import_run_id:
        return []
    connection = sqlite3.connect(database_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT d.*, r.session_id, r.worker_id, r.rank_position
            FROM human_decisions d
            JOIN recommendations r ON r.id = d.recommendation_id
            WHERE r.import_run_id = ?
            ORDER BY d.decided_at, d.id
            """,
            (import_run_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()


def _candidate_lookup(rows: list[dict]) -> dict[tuple[str, str, str, int], dict]:
    lookup = {}
    for row in rows:
        payload = json.loads(row["recommendation_json"])
        key = (
            payload["session_id"],
            payload.get("worker_id") or "",
            payload["role"],
            int(row["rank_position"]),
        )
        lookup[key] = {**payload, "recommendation_id": row["id"], "selected_by_plan": bool(row["selected_by_plan"])}
    return lookup


def _latest_decisions_by_recommendation(rows: list[dict]) -> dict[str, dict]:
    latest = {}
    for row in rows:
        latest[row["recommendation_id"]] = row
    return latest


def _candidates_for_session(
    *,
    session_id: str,
    report_details: list[dict],
    db_lookup: dict[tuple[str, str, str, int], dict],
    workers: dict,
    latest_decisions: dict[str, dict],
) -> list[dict]:
    candidates = []
    seen: set[tuple[str, str, str]] = set()
    for detail in report_details:
        all_candidates = list(detail.get("candidates") or [])
        if not all_candidates and detail.get("recommendation"):
            all_candidates.append(detail["recommendation"])
        for rank, recommendation in enumerate(all_candidates, start=1):
            worker_id = recommendation.get("worker_id") or ""
            key = (session_id, worker_id, recommendation["role"])
            if key in seen:
                continue
            seen.add(key)
            db_candidate = db_lookup.get((session_id, worker_id, recommendation["role"], rank))
            recommendation_id = db_candidate["recommendation_id"] if db_candidate else ""
            decision = latest_decisions.get(recommendation_id)
            candidates.append(
                {
                    "recommendation_id": recommendation_id,
                    "session_id": session_id,
                    "worker_id": worker_id,
                    "worker_name": workers.get(worker_id, {}).get("name", worker_id or "No candidate"),
                    "role": recommendation["role"],
                    "rank": rank,
                    "score": recommendation["score"],
                    "selected_by_plan": bool(detail.get("accepted") and detail["recommendation"] == recommendation)
                    or bool(db_candidate and db_candidate["selected_by_plan"]),
                    "hard_constraint_failures": recommendation["hard_constraint_failures"],
                    "reasons": recommendation["reasons"],
                    "warnings": recommendation["warnings"],
                    "components": recommendation["components"],
                    "travel_estimate": recommendation["travel_estimate"],
                    "decision_type": decision["decision_type"] if decision else "",
                    "override_reason": decision["override_reason"] if decision else "",
                    "decided_at": decision["decided_at"] if decision else "",
                }
            )
    candidates.sort(key=lambda item: (item["hard_constraint_failures"] != [], item["rank"], -float(item["score"])))
    return candidates


def _session_warnings(*, session: dict, service_user: dict, details: list[dict], import_warnings: list[dict]) -> list[str]:
    warnings: list[str] = []
    if service_user.get("profile_is_stale"):
        warnings.append("stale service-user profile")
    location = session.get("location") or {}
    if not any(location.get(key) for key in ("postcode", "latitude", "area")):
        warnings.append("missing travel data for session location")
    for detail in details:
        recommendation = detail["recommendation"]
        warnings.extend(recommendation.get("warnings") or [])
        if recommendation.get("hard_constraint_failures"):
            warnings.extend(recommendation["hard_constraint_failures"])
        for candidate in detail.get("candidates") or []:
            warnings.extend(candidate.get("warnings") or [])
            if candidate.get("hard_constraint_failures"):
                warnings.extend(candidate["hard_constraint_failures"])
    for issue in import_warnings:
        if issue.get("table") == "sessions" and issue.get("source_ref") == (session.get("source_reference") or {}).get("source_ref"):
            warnings.append(issue["message"])
    return sorted(set(warnings))


def _plan_status(session: dict, remaining: int) -> str:
    if session["status"] in {"cancelled", "rejected", "confirmed"}:
        return session["status"]
    if remaining <= 0:
        return "allocated"
    if session.get("assigned"):
        return "partially_allocated"
    return "unfilled"


def _review_state(candidates: list[dict], warnings: list[str]) -> str:
    decisions = [candidate["decision_type"] for candidate in candidates if candidate["decision_type"]]
    if any(item in {"accept", "override"} for item in decisions):
        return "accepted"
    if "reject" in decisions:
        return "rejected"
    if warnings:
        return "needs_information"
    if candidates:
        return "draft"
    return "reviewed"


def _apply_filters(sessions: list[dict], filters: dict[str, str]) -> list[dict]:
    result = sessions
    if filters.get("date"):
        result = [item for item in result if item["date"] == filters["date"]]
    if filters.get("service_user"):
        needle = filters["service_user"].lower()
        result = [item for item in result if needle in item["service_user_name"].lower() or needle in item["service_user_id"].lower()]
    if filters.get("worker"):
        needle = filters["worker"].lower()
        result = [
            item
            for item in result
            if any(needle in worker["worker_name"].lower() or needle in worker["worker_id"].lower() for worker in item["assigned_workers"])
            or any(needle in candidate["worker_name"].lower() or needle in candidate["worker_id"].lower() for candidate in item["candidates"])
        ]
    if filters.get("status"):
        result = [item for item in result if item["status"] == filters["status"] or item["review_state"] == filters["status"]]
    if filters.get("priority"):
        result = [item for item in result if item["priority_band"] == filters["priority"]]
    return result


def _positive_int(value: str | None, *, default: int, maximum: int) -> int:
    try:
        number = int(value) if value else default
    except ValueError:
        return default
    return max(1, min(maximum, number))


def _paginate(sessions: list[dict], *, page: int, page_size: int) -> tuple[list[dict], dict]:
    total = len(sessions)
    page_count = max(1, (total + page_size - 1) // page_size)
    current_page = min(page, page_count)
    start = (current_page - 1) * page_size
    end = start + page_size
    return sessions[start:end], {
        "page": current_page,
        "page_size": page_size,
        "page_count": page_count,
        "total": total,
        "has_previous": current_page > 1,
        "has_next": current_page < page_count,
    }


def _worker_day_view(sessions: list[dict]) -> list[dict]:
    rows: dict[str, dict] = {}
    for session in sessions:
        for worker in session["assigned_workers"]:
            row = rows.setdefault(worker["worker_id"], {"worker_id": worker["worker_id"], "worker_name": worker["worker_name"], "stops": []})
            row["stops"].append(
                {
                    "start": session["start"],
                    "end": session["end"],
                    "session_id": session["session_id"],
                    "service_user_name": session["service_user_name"],
                    "location": session["location"].get("label") or session["location"].get("area") or "Unknown location",
                }
            )
        for candidate in session["candidates"]:
            if not candidate["selected_by_plan"] or not candidate["worker_id"]:
                continue
            row = rows.setdefault(candidate["worker_id"], {"worker_id": candidate["worker_id"], "worker_name": candidate["worker_name"], "stops": []})
            stop = {
                "start": session["start"],
                "end": session["end"],
                "session_id": session["session_id"],
                "service_user_name": session["service_user_name"],
                "location": session["location"].get("label") or session["location"].get("area") or "Unknown location",
            }
            if stop not in row["stops"]:
                row["stops"].append(stop)
    for row in rows.values():
        row["stops"].sort(key=lambda item: (item["start"], item["session_id"]))
    return sorted(rows.values(), key=lambda item: item["worker_name"])


def _all_candidates(sessions: list[dict]) -> list[dict]:
    return [candidate for session in sessions for candidate in session["candidates"]]


def _render_session_card(session: dict) -> str:
    warnings = "".join(f"<li>{_h(item)}</li>" for item in session["warnings"]) or '<li class="muted">No warnings.</li>'
    assigned = ", ".join(f"{_h(item['worker_name'])} ({_h(item['role'])})" for item in session["assigned_workers"]) or "None yet"
    candidates = "\n".join(_render_candidate(candidate) for candidate in session["candidates"])
    if not candidates:
        candidates = '<p class="empty">No ranked candidates were retained for this session.</p>'
    return f"""
<article class="session-card" id="{_h(session['session_id'])}">
  <div class="card-head">
    <div>
      <h3>{_h(session['service_user_name'])}</h3>
      <p class="subtle">{_h(session['session_id'])} · {_h(session['start'])} to {_h(session['end'])}</p>
    </div>
    <div class="badges">
      <span>{_h(session['status'])}</span>
      <span>{_h(session['review_state'])}</span>
      <span>priority {_h(str(session['priority']))}</span>
    </div>
  </div>
  <dl class="facts">
    <div><dt>Assigned</dt><dd>{assigned}</dd></div>
    <div><dt>Needed</dt><dd>{session['remaining_worker_count']} more</dd></div>
    <div><dt>Driver</dt><dd>{'required' if session['requires_driver'] else 'not required'}</dd></div>
    <div><dt>Location</dt><dd>{_h(session['location'].get('label') or session['location'].get('area') or 'Unknown')}</dd></div>
  </dl>
  <details open>
    <summary>Warnings and hard failures</summary>
    <ul>{warnings}</ul>
  </details>
  <div class="candidates">
    <h4>Ranked candidates and alternatives</h4>
    {candidates}
  </div>
</article>
"""


def _render_candidate(candidate: dict) -> str:
    reasons = "".join(f"<li>{_h(item)}</li>" for item in candidate["reasons"]) or '<li class="muted">No positive reasons recorded.</li>'
    failures = "".join(f"<li>{_h(item)}</li>" for item in candidate["hard_constraint_failures"]) or '<li class="muted">No hard failures.</li>'
    warnings = "".join(f"<li>{_h(item)}</li>" for item in candidate["warnings"]) or '<li class="muted">No candidate warnings.</li>'
    components = "".join(
        f"<li><span>{_h(name)}</span><strong>{value}</strong></li>"
        for name, value in sorted(candidate["components"].items())
    ) or '<li class="muted">No score components.</li>'
    travel = candidate["travel_estimate"] or {}
    travel_text = "No travel estimate"
    if travel:
        travel_text = f"{_h(travel['mode'])}, {travel['minutes']} min, confidence {travel['confidence']}"
    decision = ""
    if candidate["decision_type"]:
        decision = f"<p class=\"decision\">Decision: {_h(candidate['decision_type'])} at {_h(candidate['decided_at'])}</p>"
    form = _render_decision_form(candidate)
    selected = " selected" if candidate["selected_by_plan"] else ""
    blocked = " blocked" if candidate["hard_constraint_failures"] else ""
    return f"""
<article class="candidate{selected}{blocked}">
  <div class="candidate-head">
    <div>
      <h5>{_h(candidate['worker_name'])} <span>{_h(candidate['role'])}</span></h5>
      <p class="subtle">Rank {candidate['rank']} · Score {candidate['score']} · {travel_text}</p>
    </div>
    <strong>{'Chosen by plan' if candidate['selected_by_plan'] else 'Alternative'}</strong>
  </div>
  {decision}
  <div class="candidate-grid">
    <details open><summary>Reasons</summary><ul>{reasons}</ul></details>
    <details><summary>Hard failures</summary><ul>{failures}</ul></details>
    <details><summary>Warnings</summary><ul>{warnings}</ul></details>
    <details><summary>Score components</summary><ul class="components">{components}</ul></details>
  </div>
  {form}
</article>
"""


def _render_decision_form(candidate: dict) -> str:
    if not candidate["recommendation_id"]:
        return '<p class="muted">Decision controls require an audit database.</p>'
    return f"""
<form method="post" action="/decisions" class="decision-form">
  <input type="hidden" name="recommendation_id" value="{_h(candidate['recommendation_id'])}">
  <input type="hidden" name="selected_worker_id" value="{_h(candidate['worker_id'])}">
  <input name="actor" placeholder="reviewer@example.test" required>
  <select name="decision_type" required>
    <option value="accept">Accept</option>
    <option value="reject">Reject</option>
    <option value="override">Override</option>
  </select>
  <input name="override_reason" placeholder="Reason for reject or override">
  <input name="notes" placeholder="Notes">
  <button type="submit">Record</button>
</form>
"""


def _render_worker_day(item: dict) -> str:
    stops = "".join(
        f"<li>{_h(stop['start'])}: {_h(stop['service_user_name'])} at {_h(stop['location'])}</li>"
        for stop in item["stops"]
    )
    return f"<div class=\"route\"><h3>{_h(item['worker_name'])}</h3><ol>{stops}</ol></div>"


def _render_filters(filters: dict[str, str]) -> str:
    return f"""
<form class="filters" method="get" action="/">
  <input name="date" value="{_h(filters.get('date', ''))}" placeholder="Date YYYY-MM-DD">
  <input name="service_user" value="{_h(filters.get('service_user', ''))}" placeholder="Service user">
  <input name="worker" value="{_h(filters.get('worker', ''))}" placeholder="Worker">
  <select name="status">
    {_option('', 'Any status', filters.get('status', ''))}
    {_option('allocated', 'Allocated', filters.get('status', ''))}
    {_option('partially_allocated', 'Partially allocated', filters.get('status', ''))}
    {_option('unfilled', 'Unfilled', filters.get('status', ''))}
    {_option('needs_information', 'Needs information', filters.get('status', ''))}
    {_option('accepted', 'Accepted', filters.get('status', ''))}
    {_option('rejected', 'Rejected', filters.get('status', ''))}
    {_option('draft', 'Draft', filters.get('status', ''))}
  </select>
  <select name="priority">
    {_option('', 'Any priority', filters.get('priority', ''))}
    {_option('high', 'High', filters.get('priority', ''))}
    {_option('normal', 'Normal', filters.get('priority', ''))}
  </select>
  <select name="page_size">
    {_option('25', '25 per page', filters.get('page_size', '25'))}
    {_option('50', '50 per page', filters.get('page_size', '25'))}
    {_option('100', '100 per page', filters.get('page_size', '25'))}
  </select>
  <button type="submit">Filter</button>
  <a href="/">Clear</a>
</form>
"""


def _render_pagination(pagination: dict, filters: dict[str, str]) -> str:
    if pagination["page_count"] <= 1:
        return ""
    previous_link = ""
    next_link = ""
    if pagination["has_previous"]:
        previous_link = f'<a href="/?{_h(_page_query(filters, pagination["page"] - 1))}">Previous</a>'
    if pagination["has_next"]:
        next_link = f'<a href="/?{_h(_page_query(filters, pagination["page"] + 1))}">Next</a>'
    return f"""
<nav class="pagination" aria-label="Session pages">
  {previous_link}
  <span>Page {pagination['page']} of {pagination['page_count']}</span>
  {next_link}
</nav>
"""


def _page_query(filters: dict[str, str], page: int) -> str:
    query = {key: value for key, value in filters.items() if value and key not in {"message", "error"}}
    query["page"] = str(page)
    return urlencode(query)


def _option(value: str, label: str, selected: str) -> str:
    attr = " selected" if value == selected else ""
    return f'<option value="{_h(value)}"{attr}>{_h(label)}</option>'


def _metric(label: str, value: int) -> str:
    return f'<div><strong>{value}</strong><span>{_h(label)}</span></div>'


def _render_notice(text: str, class_name: str) -> str:
    if not text:
        return ""
    return f'<p class="{class_name}">{_h(text)}</p>'


def _send_html(handler: BaseHTTPRequestHandler, body: str) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def _first_values(values: dict[str, list[str]]) -> dict[str, str]:
    return {key: items[0] for key, items in values.items() if items}


def _h(value) -> str:
    return html.escape(str(value), quote=True)


_CSS = """
:root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
body { margin: 0; background: #f6f7f4; color: #1f261f; }
header { display: flex; justify-content: space-between; gap: 24px; padding: 28px 32px 18px; background: #ffffff; border-bottom: 1px solid #dfe3da; }
h1, h2, h3, h4, h5, p { margin: 0; }
h1 { font-size: 30px; font-weight: 760; }
h2 { font-size: 18px; }
h3 { font-size: 17px; }
h4 { font-size: 15px; margin: 18px 0 10px; }
h5 { font-size: 15px; }
.eyebrow { text-transform: uppercase; letter-spacing: .08em; color: #5f6f64; font-size: 12px; font-weight: 700; margin-bottom: 6px; }
.subtle, .muted { color: #66706a; font-size: 13px; }
.audit-pill, .badges span { border: 1px solid #cbd4c9; border-radius: 999px; padding: 7px 10px; background: #f9faf7; font-size: 13px; align-self: flex-start; }
.notice, .error { margin: 16px 32px 0; padding: 10px 12px; border-radius: 6px; }
.notice { background: #e8f5ed; color: #174d2a; }
.error { background: #fae8e5; color: #7a231a; }
.summary { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; padding: 18px 32px; }
.summary div, .side-panel, .session-card { background: #ffffff; border: 1px solid #dfe3da; border-radius: 8px; }
.summary div { padding: 14px; }
.summary strong { display: block; font-size: 24px; }
.summary span { color: #66706a; font-size: 13px; }
.filters { display: grid; grid-template-columns: repeat(8, minmax(0, 1fr)); gap: 8px; padding: 0 32px 18px; align-items: center; }
input, select, button { min-height: 36px; border: 1px solid #c9d1c7; border-radius: 6px; padding: 0 10px; background: #fff; color: #1f261f; font: inherit; }
button { background: #245a45; color: #fff; border-color: #245a45; font-weight: 700; cursor: pointer; }
a { color: #245a45; font-weight: 700; text-decoration: none; }
.layout { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 18px; padding: 0 32px 32px; }
.section-title, .card-head, .candidate-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
.section-title { margin: 4px 0 12px; }
.pagination { display: flex; align-items: center; justify-content: center; gap: 14px; margin: 10px 0 14px; color: #66706a; }
.session-card { padding: 18px; margin-bottom: 14px; }
.badges { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 16px 0; }
.facts div { background: #f7f8f5; border-radius: 6px; padding: 10px; }
dt { color: #66706a; font-size: 12px; }
dd { margin: 4px 0 0; font-size: 14px; }
details { margin-top: 8px; }
summary { cursor: pointer; font-weight: 700; }
li { margin: 5px 0; }
.candidate { border: 1px solid #dfe3da; border-radius: 8px; padding: 14px; margin: 10px 0; background: #fbfcf9; }
.candidate.selected { border-color: #245a45; background: #f2f8f4; }
.candidate.blocked { border-color: #d2a29a; }
.candidate h5 span { color: #66706a; font-weight: 500; }
.candidate-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.components li { display: flex; justify-content: space-between; gap: 10px; }
.decision-form { display: grid; grid-template-columns: 1.2fr .8fr 1fr 1fr auto; gap: 8px; margin-top: 12px; }
.decision { margin-top: 8px; color: #245a45; font-weight: 700; }
.side-panel { padding: 16px; margin-bottom: 14px; }
.route { margin-top: 12px; }
.empty { color: #66706a; padding: 14px 0; }
@media (max-width: 980px) {
  header, .section-title, .card-head, .candidate-head { flex-direction: column; }
  .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .filters, .layout, .facts, .candidate-grid, .decision-form { grid-template-columns: 1fr; }
  .layout { padding: 0 18px 24px; }
  header, .summary, .filters { padding-left: 18px; padding-right: 18px; }
}
"""


if __name__ == "__main__":
    raise SystemExit(main())
