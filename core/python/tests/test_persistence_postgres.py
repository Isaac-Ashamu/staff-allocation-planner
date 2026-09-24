"""Persistence audit storage against a real local Postgres backend.

Mirrors the key behaviours in test_persistence.py (traceable audit records,
override-reason enforcement, schema bootstrap) but against Postgres instead of
SQLite, to prove the dialect-translation layer in persistence.py actually
works end to end. Skipped entirely when psycopg isn't installed or no local
Postgres server answers -- SQLite stays the default, zero-setup backend for
everyone else; this file only runs for someone deliberately validating the
optional Postgres path (see docs/BUILD_PLAN.md Phase 7).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from booking_recommender import (
    FallbackTravelEstimator,
    PersistenceError,
    build_allocation_report,
    initialize_database,
    load_standardized_csv,
    persist_allocation_report,
    recommendations_for_import_run,
    recommendations_for_service_user,
    recommendations_for_session,
    recommendations_for_worker,
    record_human_decision,
    seed_development_data,
)

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "standardized_csv_v1"

try:
    import psycopg
except ImportError:
    psycopg = None

_ADMIN_DSN = "postgresql:///postgres"


def _postgres_available() -> bool:
    if psycopg is None:
        return False
    try:
        with psycopg.connect(_ADMIN_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


_SKIP_REASON = "psycopg not installed or no local Postgres server reachable at postgresql:///postgres"


@unittest.skipUnless(_postgres_available(), _SKIP_REASON)
class PostgresPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.db_name = f"recommender_test_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(_ADMIN_DSN, autocommit=True) as connection:
            connection.execute(f'CREATE DATABASE "{self.db_name}"')
        self.dsn = f"postgresql:///{self.db_name}"

    def tearDown(self):
        with psycopg.connect(_ADMIN_DSN, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{self.db_name}" WITH (FORCE)')

    def test_persists_traceable_allocation_audit_records(self):
        report = _fixture_report()

        persisted = persist_allocation_report(self.dsn, report, tenant_id="tenant-test")
        repeated = persist_allocation_report(self.dsn, report, tenant_id="tenant-test")

        self.assertEqual(persisted.planning_snapshot_id, repeated.planning_snapshot_id)
        self.assertEqual("import-synthetic-2026-07-01", persisted.import_run_id)
        self.assertTrue(persisted.recommendation_ids)

        by_session = recommendations_for_session(self.dsn, "session-100")
        by_worker = recommendations_for_worker(self.dsn, "worker-alpha")
        by_service_user = recommendations_for_service_user(self.dsn, "su-orchard")
        by_import_run = recommendations_for_import_run(self.dsn, "import-synthetic-2026-07-01")

        self.assertTrue(by_session)
        self.assertTrue(by_worker)
        self.assertEqual({row["id"] for row in by_session}, {row["id"] for row in by_service_user})
        self.assertGreaterEqual(len(by_import_run), len(by_session))

        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM recommendation_components")
                (components,) = cursor.fetchone()
        self.assertGreater(components, 0)

    def test_human_decisions_store_separately_and_require_override_reason(self):
        report = _fixture_report()
        persist_allocation_report(self.dsn, report, tenant_id="tenant-test")
        top = recommendations_for_session(self.dsn, "session-100")[0]
        non_top = recommendations_for_session(self.dsn, "session-100")[1]

        decision_id = record_human_decision(
            self.dsn,
            recommendation_id=top["id"],
            decision_type="accept",
            actor="planner@example.test",
        )
        self.assertTrue(decision_id.startswith("decision-"))

        with self.assertRaises(PersistenceError):
            record_human_decision(
                self.dsn,
                recommendation_id=non_top["id"],
                decision_type="accept",
                actor="planner@example.test",
            )

        override_id = record_human_decision(
            self.dsn,
            recommendation_id=non_top["id"],
            decision_type="accept",
            actor="planner@example.test",
            override_reason="Reviewed family continuity notes",
        )
        self.assertTrue(override_id.startswith("decision-"))

        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM human_decisions")
                (count,) = cursor.fetchone()
        self.assertEqual(2, count)

    def test_initialize_database_creates_schema(self):
        initialize_database(self.dsn)
        seed_development_data(self.dsn, tenant_id="tenant-seed")

        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
                tables = {row[0] for row in cursor.fetchall()}
        self.assertIn("recommendations", tables)
        self.assertIn("human_decisions", tables)

    def test_allocation_cli_can_write_postgres_audit_database(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "allocation-output"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "booking_recommender.allocation_cli",
                    str(FIXTURE_DIR),
                    "--output-dir",
                    str(output_dir),
                    "--include-infeasible",
                    "--audit-db",
                    self.dsn,
                    "--tenant-id",
                    "tenant-cli",
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Saved audit records", completed.stdout)
        self.assertTrue(recommendations_for_import_run(self.dsn, "import-synthetic-2026-07-01"))


def _fixture_report():
    import_result = load_standardized_csv(FIXTURE_DIR)
    return build_allocation_report(
        source_folder=FIXTURE_DIR,
        state=import_result.state,
        import_report=import_result.report,
        travel_estimator=FallbackTravelEstimator(),
        top_n=5,
        include_infeasible=True,
        compare_single_session=True,
    )


if __name__ == "__main__":
    unittest.main()
