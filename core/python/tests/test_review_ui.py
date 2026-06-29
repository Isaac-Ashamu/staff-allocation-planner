from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from booking_recommender import PersistenceError
from booking_recommender.review_ui import (
    build_review_model,
    prepare_review_run,
    render_review_dashboard,
    submit_review_decision,
)


FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "standardized_csv_v1"


class ReviewUiTests(unittest.TestCase):
    def test_review_model_shows_whole_plan_queues_and_warning_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "audit.sqlite"
            prepared = prepare_review_run(FIXTURE_DIR, audit_db=database, tenant_id="tenant-review")

            model = build_review_model(prepared.report, database_path=database)
            html = render_review_dashboard(model)

            self.assertEqual(1, model["summary"]["allocated"])
            self.assertEqual(1, model["summary"]["partially_allocated"])
            self.assertEqual(1, model["summary"]["unfilled"])
            self.assertGreaterEqual(model["summary"]["warning"], 1)
            self.assertIn("Proposed plan dashboard", html)
            self.assertIn("Ranked candidates and alternatives", html)
            self.assertIn("Hard failures", html)
            self.assertIn("Score components", html)
            self.assertIn("Worker day / route view", html)
            self.assertIn("Data-quality warnings", html)
            self.assertIn("stale service-user profile", html)
            self.assertEqual(1, model["pagination"]["page_count"])
            self.assertEqual(model["summary"]["visible"], model["summary"]["showing"])

    def test_review_filters_cover_status_worker_service_user_and_priority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "audit.sqlite"
            prepared = prepare_review_run(FIXTURE_DIR, audit_db=database, tenant_id="tenant-review")

            high_priority = build_review_model(
                prepared.report,
                database_path=database,
                filters={"priority": "high"},
            )
            alpha_worker = build_review_model(
                prepared.report,
                database_path=database,
                filters={"worker": "Alex"},
            )
            unfilled = build_review_model(
                prepared.report,
                database_path=database,
                filters={"status": "partially_allocated"},
            )
            service_user = build_review_model(
                prepared.report,
                database_path=database,
                filters={"service_user": "Orchard"},
            )

            self.assertEqual(["session-200"], [item["session_id"] for item in high_priority["sessions"]])
            self.assertEqual(["session-100", "session-200"], [item["session_id"] for item in alpha_worker["sessions"]])
            self.assertEqual(["session-200"], [item["session_id"] for item in unfilled["sessions"]])
            self.assertEqual(["session-100"], [item["session_id"] for item in service_user["sessions"]])

    def test_accept_reject_and_override_decisions_are_audited(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "audit.sqlite"
            prepared = prepare_review_run(FIXTURE_DIR, audit_db=database, tenant_id="tenant-review")
            model = build_review_model(prepared.report, database_path=database)
            session = next(item for item in model["sessions"] if item["session_id"] == "session-100")
            top = session["candidates"][0]
            non_top = session["candidates"][1]

            accepted_id = submit_review_decision(
                database,
                recommendation_id=top["recommendation_id"],
                decision_type="accept",
                actor="planner@example.test",
            )
            self.assertTrue(accepted_id.startswith("decision-"))

            with self.assertRaises(PersistenceError):
                submit_review_decision(
                    database,
                    recommendation_id=top["recommendation_id"],
                    decision_type="reject",
                    actor="planner@example.test",
                )

            rejected_id = submit_review_decision(
                database,
                recommendation_id=non_top["recommendation_id"],
                decision_type="reject",
                actor="planner@example.test",
                override_reason="Worker is unavailable after recent update",
            )
            self.assertTrue(rejected_id.startswith("decision-"))

            refreshed = build_review_model(prepared.report, database_path=database)
            self.assertEqual(1, refreshed["summary"]["approved"])
            with sqlite3.connect(database) as connection:
                decisions = connection.execute("SELECT decision_type FROM human_decisions").fetchall()
            self.assertEqual(["accept", "reject"], [row[0] for row in decisions])


if __name__ == "__main__":
    unittest.main()
