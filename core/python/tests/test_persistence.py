from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
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


class PersistenceTests(unittest.TestCase):
    def test_persists_traceable_allocation_audit_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "audit.sqlite"
            report = _fixture_report()

            persisted = persist_allocation_report(database, report, tenant_id="tenant-test")
            repeated = persist_allocation_report(database, report, tenant_id="tenant-test")

            self.assertEqual(persisted.planning_snapshot_id, repeated.planning_snapshot_id)
            self.assertEqual("import-synthetic-2026-07-01", persisted.import_run_id)
            self.assertTrue(persisted.recommendation_ids)

            by_session = recommendations_for_session(database, "session-100")
            by_worker = recommendations_for_worker(database, "worker-alpha")
            by_service_user = recommendations_for_service_user(database, "su-orchard")
            by_import_run = recommendations_for_import_run(database, "import-synthetic-2026-07-01")

            self.assertTrue(by_session)
            self.assertTrue(by_worker)
            self.assertEqual({row["id"] for row in by_session}, {row["id"] for row in by_service_user})
            self.assertGreaterEqual(len(by_import_run), len(by_session))

            with sqlite3.connect(database) as connection:
                connection.row_factory = sqlite3.Row
                snapshot = connection.execute("SELECT * FROM planning_snapshots").fetchone()
                import_run = connection.execute("SELECT * FROM import_runs").fetchone()
                session_record = connection.execute(
                    """
                    SELECT * FROM normalized_records
                    WHERE table_name = 'sessions' AND record_id = 'session-100'
                    """
                ).fetchone()
                components = connection.execute("SELECT COUNT(*) FROM recommendation_components").fetchone()[0]

            self.assertEqual(persisted.planning_snapshot_id, snapshot["id"])
            self.assertEqual(snapshot["id"], import_run["generated_state_snapshot_id"])
            self.assertEqual("su-orchard", session_record["service_user_id"])
            self.assertGreater(components, 0)

    def test_human_decisions_store_separately_and_require_override_reason(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "audit.sqlite"
            report = _fixture_report()
            persist_allocation_report(database, report, tenant_id="tenant-test")
            top = recommendations_for_session(database, "session-100")[0]
            non_top = recommendations_for_session(database, "session-100")[1]

            decision_id = record_human_decision(
                database,
                recommendation_id=top["id"],
                decision_type="accept",
                actor="planner@example.test",
            )
            self.assertTrue(decision_id.startswith("decision-"))

            with self.assertRaises(PersistenceError):
                record_human_decision(
                    database,
                    recommendation_id=non_top["id"],
                    decision_type="accept",
                    actor="planner@example.test",
                )

            override_id = record_human_decision(
                database,
                recommendation_id=non_top["id"],
                decision_type="accept",
                actor="planner@example.test",
                override_reason="Reviewed family continuity notes",
            )
            self.assertTrue(override_id.startswith("decision-"))
            persist_allocation_report(database, report, tenant_id="tenant-test")

            with sqlite3.connect(database) as connection:
                count = connection.execute("SELECT COUNT(*) FROM human_decisions").fetchone()[0]
            self.assertEqual(2, count)

    def test_persistence_cli_initializes_and_seeds_without_source_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "audit.sqlite"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "booking_recommender.persistence_cli",
                    "seed-dev",
                    str(database),
                    "--tenant-id",
                    "tenant-dev",
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("Seeded development tenant tenant-dev", completed.stdout)
            with sqlite3.connect(database) as connection:
                tenant = connection.execute("SELECT id FROM tenants").fetchone()[0]
            self.assertEqual("tenant-dev", tenant)

    def test_allocation_cli_can_write_audit_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "audit.sqlite"
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
                    str(database),
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
            self.assertTrue(recommendations_for_import_run(database, "import-synthetic-2026-07-01"))

    def test_initialize_database_creates_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "audit.sqlite"
            initialize_database(database)
            seed_development_data(database, tenant_id="tenant-seed")

            with sqlite3.connect(database) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertIn("recommendations", tables)
            self.assertIn("human_decisions", tables)


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
