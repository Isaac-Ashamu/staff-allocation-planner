from pathlib import Path
import unittest

from booking_recommender import HeuristicTravelEstimator, rank_session_candidates
from booking_recommender.importers import import_workbook_planning_state


ROOT = Path(__file__).resolve().parents[4]
IMPORT_WORKBOOK = (
    Path(r"C:\path\to\source-data\booking-import.xlsx")
)
POSTCODE_WORKBOOK = ROOT / "data" / "Staff and SU names and postcode.xlsx"
AD_HOCS_WORKBOOK = ROOT / "data" / "July 2026 Ad hocs.xlsx"


class WorkbookImportTests(unittest.TestCase):
    def test_real_workbook_sources_import_into_planning_state(self):
        if not IMPORT_WORKBOOK.exists() or not POSTCODE_WORKBOOK.exists():
            self.skipTest("source workbooks are not available on this machine")

        result = import_workbook_planning_state(
            import_workbook=IMPORT_WORKBOOK,
            postcode_workbook=POSTCODE_WORKBOOK,
            ad_hocs_workbook=AD_HOCS_WORKBOOK,
        )

        self.assertGreaterEqual(len(result.state.workers), 50)
        self.assertGreaterEqual(len(result.state.service_users), 50)
        self.assertGreaterEqual(len(result.state.sessions), 50)

        open_session = next(
            session
            for session in result.state.sessions.values()
            if len(session.assigned) < session.required_worker_count
        )
        recommendations = rank_session_candidates(
            result.state,
            open_session.id,
            "second",
            HeuristicTravelEstimator(),
            top_n=3,
        )
        self.assertTrue(recommendations)


if __name__ == "__main__":
    unittest.main()

