"""Persistence and audit storage for allocation recommendations.

The production roadmap calls for Postgres and API-managed migrations. This
module keeps the first implementation dependency-light by using SQLite with a
schema that maps cleanly to relational storage later.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .allocation_report import (
    AllocationReport,
    allocation_report_to_dict,
    import_report_to_dict,
    planning_state_to_dict,
    recommendation_to_dict,
)
from .models import PlanningState, Recommendation


SCHEMA_VERSION = 1


class PersistenceError(ValueError):
    """Raised when an audit write would break review/audit rules."""


@dataclass(frozen=True)
class PersistedAllocationRun:
    tenant_id: str
    import_run_id: str | None
    planning_snapshot_id: str
    recommendation_ids: tuple[str, ...]


def initialize_database(path: str | Path) -> None:
    """Create or migrate the local audit database."""

    with _connect(path) as connection:
        _initialize_connection(connection)


def seed_development_data(path: str | Path, *, tenant_id: str = "dev-tenant") -> None:
    """Create a synthetic development tenant without loading real source data."""

    with _connect(path) as connection:
        _initialize_connection(connection)
        _upsert_tenant(connection, tenant_id, name="Development Tenant")


def persist_allocation_report(
    path: str | Path,
    report: AllocationReport,
    *,
    tenant_id: str | None = None,
) -> PersistedAllocationRun:
    """Persist import audit, immutable snapshot, recommendations, and components."""

    state = report.initial_state
    final_state = report.plan.final_state
    audit = state.import_audit or final_state.import_audit
    resolved_tenant_id = tenant_id or state.tenant_rules.tenant_id
    import_run_id = audit.import_run_id if audit else None

    with _connect(path) as connection:
        _initialize_connection(connection)
        _upsert_tenant(connection, resolved_tenant_id)
        if audit:
            _persist_import_run(
                connection,
                tenant_id=resolved_tenant_id,
                audit=audit,
                import_report=import_report_to_dict(report.import_report),
            )

        snapshot_id = _persist_planning_snapshot(
            connection,
            tenant_id=resolved_tenant_id,
            import_run_id=import_run_id,
            state=state,
        )
        if audit and audit.generated_state_snapshot_id and audit.generated_state_snapshot_id != snapshot_id:
            raise PersistenceError(
                "import audit generated_state_snapshot_id does not match persisted snapshot"
            )
        if audit and not audit.generated_state_snapshot_id:
            connection.execute(
                "UPDATE import_runs SET generated_state_snapshot_id = ? WHERE id = ?",
                (snapshot_id, audit.import_run_id),
            )

        recommendation_ids = []
        decision_index = 0
        for item in report.plan.assignments:
            decision_index += 1
            candidates = item.candidates or (item.recommendation,)
            for rank, recommendation in enumerate(candidates, start=1):
                accepted = item.accepted and _same_candidate(item.recommendation, recommendation)
                recommendation_id = _persist_recommendation(
                    connection,
                    tenant_id=resolved_tenant_id,
                    import_run_id=import_run_id,
                    snapshot_id=snapshot_id,
                    recommendation=recommendation,
                    decision_index=decision_index,
                    rank_position=rank,
                    selected_by_plan=accepted,
                )
                recommendation_ids.append(recommendation_id)

        connection.commit()

    return PersistedAllocationRun(
        tenant_id=resolved_tenant_id,
        import_run_id=import_run_id,
        planning_snapshot_id=snapshot_id,
        recommendation_ids=tuple(recommendation_ids),
    )


def record_human_decision(
    path: str | Path,
    *,
    recommendation_id: str,
    decision_type: str,
    actor: str,
    selected_worker_id: str | None = None,
    override_reason: str | None = None,
    notes: str = "",
    decided_at: datetime | None = None,
) -> str:
    """Store a human accept/reject/override decision.

    Override reasons are required when the selected worker differs from the
    recommendation or when a non-top ranked candidate is accepted.
    """

    normalized_decision_type = decision_type.strip().lower()
    if normalized_decision_type not in {"accept", "reject", "override"}:
        raise PersistenceError("decision_type must be accept, reject, or override")
    if not actor.strip():
        raise PersistenceError("actor is required for human decisions")

    with _connect(path) as connection:
        _initialize_connection(connection)
        row = connection.execute(
            """
            SELECT id, tenant_id, planning_snapshot_id, worker_id, rank_position
            FROM recommendations
            WHERE id = ?
            """,
            (recommendation_id,),
        ).fetchone()
        if row is None:
            raise PersistenceError(f"unknown recommendation_id {recommendation_id}")

        chosen_worker_id = (selected_worker_id or row["worker_id"] or "").strip()
        reason = override_reason.strip() if override_reason else ""
        requires_override_reason = (
            normalized_decision_type == "override"
            or (chosen_worker_id and chosen_worker_id != (row["worker_id"] or ""))
            or (normalized_decision_type == "accept" and int(row["rank_position"]) != 1)
        )
        if requires_override_reason and not reason:
            raise PersistenceError("override_reason is required for non-top or override decisions")

        decision_time = decided_at or datetime.now(timezone.utc)
        decision_id = _stable_id(
            "decision",
            row["planning_snapshot_id"],
            recommendation_id,
            normalized_decision_type,
            actor.strip(),
            chosen_worker_id,
            _iso(decision_time),
        )
        connection.execute(
            """
            INSERT INTO human_decisions (
                id, tenant_id, recommendation_id, planning_snapshot_id, decision_type,
                actor, selected_worker_id, override_reason, notes, decided_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                row["tenant_id"],
                recommendation_id,
                row["planning_snapshot_id"],
                normalized_decision_type,
                actor.strip(),
                chosen_worker_id or None,
                reason or None,
                notes,
                _iso(decision_time),
            ),
        )
        connection.commit()
        return decision_id


def recommendations_for_session(path: str | Path, session_id: str) -> list[sqlite3.Row]:
    return _fetch_recommendations(path, "session_id = ?", (session_id,))


def recommendations_for_worker(path: str | Path, worker_id: str) -> list[sqlite3.Row]:
    return _fetch_recommendations(path, "worker_id = ?", (worker_id,))


def recommendations_for_service_user(path: str | Path, service_user_id: str) -> list[sqlite3.Row]:
    return _fetch_recommendations(path, "service_user_id = ?", (service_user_id,))


def recommendations_for_import_run(path: str | Path, import_run_id: str) -> list[sqlite3.Row]:
    return _fetch_recommendations(path, "import_run_id = ?", (import_run_id,))


def _initialize_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS import_runs (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            imported_at TEXT NOT NULL,
            source_system TEXT NOT NULL,
            source_refs_json TEXT NOT NULL,
            row_counts_json TEXT NOT NULL,
            validation_warnings_json TEXT NOT NULL,
            generated_state_snapshot_id TEXT,
            import_report_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_file_metadata (
            id TEXT PRIMARY KEY,
            import_run_id TEXT NOT NULL REFERENCES import_runs(id),
            source_ref TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS planning_snapshots (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            import_run_id TEXT REFERENCES import_runs(id),
            created_at TEXT NOT NULL,
            state_json TEXT NOT NULL,
            state_sha256 TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS normalized_records (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            import_run_id TEXT REFERENCES import_runs(id),
            planning_snapshot_id TEXT NOT NULL REFERENCES planning_snapshots(id),
            table_name TEXT NOT NULL,
            record_id TEXT NOT NULL,
            worker_id TEXT,
            service_user_id TEXT,
            session_id TEXT,
            source_ref TEXT,
            row_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recommendations (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            import_run_id TEXT REFERENCES import_runs(id),
            planning_snapshot_id TEXT NOT NULL REFERENCES planning_snapshots(id),
            decision_index INTEGER NOT NULL,
            rank_position INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            worker_id TEXT,
            service_user_id TEXT,
            role TEXT NOT NULL,
            score REAL NOT NULL,
            hard_constraint_failures_json TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL,
            travel_estimate_json TEXT,
            selected_by_plan INTEGER NOT NULL,
            recommendation_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recommendation_components (
            id TEXT PRIMARY KEY,
            recommendation_id TEXT NOT NULL REFERENCES recommendations(id),
            component_name TEXT NOT NULL,
            component_value REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS human_decisions (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            recommendation_id TEXT NOT NULL REFERENCES recommendations(id),
            planning_snapshot_id TEXT NOT NULL REFERENCES planning_snapshots(id),
            decision_type TEXT NOT NULL CHECK (decision_type IN ('accept', 'reject', 'override')),
            actor TEXT NOT NULL,
            selected_worker_id TEXT,
            override_reason TEXT,
            notes TEXT NOT NULL DEFAULT '',
            decided_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS override_reasons (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            label TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(tenant_id, label)
        );

        CREATE TABLE IF NOT EXISTS outcome_feedback (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            session_id TEXT NOT NULL,
            worker_id TEXT,
            recommendation_id TEXT REFERENCES recommendations(id),
            outcome_type TEXT NOT NULL,
            feedback_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_recommendations_session ON recommendations(session_id);
        CREATE INDEX IF NOT EXISTS idx_recommendations_worker ON recommendations(worker_id);
        CREATE INDEX IF NOT EXISTS idx_recommendations_service_user ON recommendations(service_user_id);
        CREATE INDEX IF NOT EXISTS idx_recommendations_import_run ON recommendations(import_run_id);
        CREATE INDEX IF NOT EXISTS idx_normalized_records_lookup
            ON normalized_records(table_name, record_id, import_run_id);
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, _iso(datetime.now(timezone.utc))),
    )
    connection.commit()


def _persist_import_run(connection, *, tenant_id: str, audit, import_report: dict) -> None:
    connection.execute(
        """
        INSERT INTO import_runs (
            id, tenant_id, imported_at, source_system, source_refs_json, row_counts_json,
            validation_warnings_json, generated_state_snapshot_id, import_report_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            tenant_id = excluded.tenant_id,
            imported_at = excluded.imported_at,
            source_system = excluded.source_system,
            source_refs_json = excluded.source_refs_json,
            row_counts_json = excluded.row_counts_json,
            validation_warnings_json = excluded.validation_warnings_json,
            generated_state_snapshot_id = COALESCE(import_runs.generated_state_snapshot_id, excluded.generated_state_snapshot_id),
            import_report_json = excluded.import_report_json
        """,
        (
            audit.import_run_id,
            tenant_id,
            _iso(audit.imported_at),
            audit.source_system,
            _json(audit.source_refs),
            _json(dict(audit.row_counts)),
            _json(audit.validation_warnings),
            audit.generated_state_snapshot_id,
            _json(import_report),
        ),
    )
    for source_ref in audit.source_refs:
        source_id = _stable_id("source", audit.import_run_id, source_ref)
        connection.execute(
            """
            INSERT OR IGNORE INTO source_file_metadata (id, import_run_id, source_ref, metadata_json)
            VALUES (?, ?, ?, ?)
            """,
            (source_id, audit.import_run_id, source_ref, _json({"source_ref": source_ref})),
        )


def _persist_planning_snapshot(
    connection,
    *,
    tenant_id: str,
    import_run_id: str | None,
    state: PlanningState,
) -> str:
    state_json = _json(planning_state_to_dict(state))
    state_sha = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
    snapshot_id = _stable_id("snapshot", state_sha)
    existing = connection.execute(
        "SELECT state_json FROM planning_snapshots WHERE id = ?",
        (snapshot_id,),
    ).fetchone()
    if existing and existing["state_json"] != state_json:
        raise PersistenceError("planning snapshot id collision with different state")

    connection.execute(
        """
        INSERT OR IGNORE INTO planning_snapshots (
            id, tenant_id, import_run_id, created_at, state_json, state_sha256
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            tenant_id,
            import_run_id,
            _iso(datetime.now(timezone.utc)),
            state_json,
            state_sha,
        ),
    )
    _persist_normalized_records(connection, tenant_id=tenant_id, import_run_id=import_run_id, snapshot_id=snapshot_id, state=state)
    return snapshot_id


def _persist_normalized_records(
    connection,
    *,
    tenant_id: str,
    import_run_id: str | None,
    snapshot_id: str,
    state: PlanningState,
) -> None:
    state_data = planning_state_to_dict(state)
    rows: list[tuple[str, str, dict]] = []
    rows.extend(("workers", item["id"], item) for item in state_data["workers"].values())
    rows.extend(("service_users", item["id"], item) for item in state_data["service_users"].values())
    rows.extend(("sessions", item["id"], item) for item in state_data["sessions"].values())
    rows.extend(
        ("compatibility", f"{item['worker_id']}:{item['service_user_id']}", item)
        for item in state_data["compatibility"]
    )
    rows.extend(
        ("pair_history", f"{item['worker_id']}:{item['service_user_id']}", item)
        for item in state_data["pair_history"]
    )
    rows.extend(("family_requests", item["id"], item) for item in state_data["family_requests"])

    for table_name, record_id, row in rows:
        source_reference = row.get("source_reference") or {}
        worker_id = row.get("worker_id")
        service_user_id = row.get("service_user_id")
        if table_name == "sessions":
            session_id = row.get("session_id") or row.get("id")
        else:
            session_id = row.get("session_id")
        connection.execute(
            """
            INSERT OR IGNORE INTO normalized_records (
                id, tenant_id, import_run_id, planning_snapshot_id, table_name,
                record_id, worker_id, service_user_id, session_id, source_ref, row_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _stable_id("record", snapshot_id, table_name, record_id),
                tenant_id,
                import_run_id,
                snapshot_id,
                table_name,
                record_id,
                worker_id,
                service_user_id,
                session_id,
                source_reference.get("source_ref"),
                _json(row),
            ),
        )


def _persist_recommendation(
    connection,
    *,
    tenant_id: str,
    import_run_id: str | None,
    snapshot_id: str,
    recommendation: Recommendation,
    decision_index: int,
    rank_position: int,
    selected_by_plan: bool,
) -> str:
    payload = recommendation_to_dict(recommendation)
    service_user_id = _service_user_for_session(connection, snapshot_id, recommendation.action.session_id)
    recommendation_id = _stable_id(
        "recommendation",
        snapshot_id,
        str(decision_index),
        str(rank_position),
        recommendation.action.session_id,
        recommendation.action.worker_id,
        recommendation.action.role,
        _json(payload),
    )
    connection.execute(
        """
        INSERT INTO recommendations (
            id, tenant_id, import_run_id, planning_snapshot_id, decision_index, rank_position,
            session_id, worker_id, service_user_id, role, score, hard_constraint_failures_json,
            reasons_json, warnings_json, travel_estimate_json, selected_by_plan, recommendation_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            tenant_id = excluded.tenant_id,
            import_run_id = excluded.import_run_id,
            planning_snapshot_id = excluded.planning_snapshot_id,
            decision_index = excluded.decision_index,
            rank_position = excluded.rank_position,
            session_id = excluded.session_id,
            worker_id = excluded.worker_id,
            service_user_id = excluded.service_user_id,
            role = excluded.role,
            score = excluded.score,
            hard_constraint_failures_json = excluded.hard_constraint_failures_json,
            reasons_json = excluded.reasons_json,
            warnings_json = excluded.warnings_json,
            travel_estimate_json = excluded.travel_estimate_json,
            selected_by_plan = excluded.selected_by_plan,
            recommendation_json = excluded.recommendation_json
        """,
        (
            recommendation_id,
            tenant_id,
            import_run_id,
            snapshot_id,
            decision_index,
            rank_position,
            recommendation.action.session_id,
            recommendation.action.worker_id or None,
            service_user_id,
            recommendation.action.role,
            recommendation.score,
            _json(recommendation.hard_constraint_failures),
            _json(recommendation.reasons),
            _json(recommendation.warnings),
            _json(payload["travel_estimate"]) if payload["travel_estimate"] else None,
            1 if selected_by_plan else 0,
            _json(payload),
        ),
    )
    connection.execute(
        "DELETE FROM recommendation_components WHERE recommendation_id = ?",
        (recommendation_id,),
    )
    for name, value in recommendation.components.items():
        connection.execute(
            """
            INSERT INTO recommendation_components (
                id, recommendation_id, component_name, component_value
            )
            VALUES (?, ?, ?, ?)
            """,
            (_stable_id("component", recommendation_id, name), recommendation_id, name, float(value)),
        )
    return recommendation_id


def _service_user_for_session(connection, snapshot_id: str, session_id: str) -> str | None:
    row = connection.execute(
        """
        SELECT service_user_id FROM normalized_records
        WHERE planning_snapshot_id = ? AND table_name = 'sessions' AND record_id = ?
        """,
        (snapshot_id, session_id),
    ).fetchone()
    return row["service_user_id"] if row else None


def _fetch_recommendations(path: str | Path, where: str, parameters: Iterable[str]) -> list[sqlite3.Row]:
    with _connect(path) as connection:
        _initialize_connection(connection)
        return list(
            connection.execute(
                f"""
                SELECT * FROM recommendations
                WHERE {where}
                ORDER BY decision_index, rank_position, id
                """,
                tuple(parameters),
            )
        )


def _upsert_tenant(connection, tenant_id: str, *, name: str | None = None) -> None:
    connection.execute(
        """
        INSERT INTO tenants (id, name, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET name = excluded.name
        """,
        (tenant_id, name or tenant_id, _iso(datetime.now(timezone.utc))),
    )


def _same_candidate(left: Recommendation, right: Recommendation) -> bool:
    return (
        left.action.worker_id == right.action.worker_id
        and left.action.session_id == right.action.session_id
        and left.action.role == right.action.role
    )


def _connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    return connection


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"
