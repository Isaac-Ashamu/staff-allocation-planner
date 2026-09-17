"""LLM-assisted ad hoc gap-filling: confidence gating, recovery, and cost cap.

Uses a stub resolver -- no real network/API access, matching the mocking
pattern already used for live external calls in test_live_travel.py.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from booking_recommender.adapters.llm_ad_hoc_parser import (
    GapRowResult,
    LlmAdHocParsingConfig,
    LlmCallLimitExceeded,
    ResolvedPerson,
)
from booking_recommender.adapters.workbook import _family_requests


WORKER_ROWS = [
    {"worker_id": "worker-maya", "name": "Maya Fenwick"},
    {"worker_id": "worker-nell", "name": "Nell Harper"},
]
SERVICE_USER_ROWS = [
    {"service_user_id": "su-theo", "name": "Theo Sandford"},
]
WORKER_LOOKUP = {"maya fenwick": "worker-maya", "nell harper": "worker-nell"}
SERVICE_USER_LOOKUP = {"theo sandford": "su-theo"}


class StubResolver:
    """Same interface as AnthropicAdHocResolver, no network access."""

    def __init__(self, results_by_ref: dict[str, GapRowResult], *, config: LlmAdHocParsingConfig | None = None, fail_with: Exception | None = None):
        self.results_by_ref = results_by_ref
        self.config = config or LlmAdHocParsingConfig(enabled=True)
        self.fail_with = fail_with
        self.calls: list[list[dict]] = []

    def resolve_gaps(self, rows, workers, service_users):
        self.calls.append(list(rows))
        if self.fail_with is not None:
            raise self.fail_with
        return [self.results_by_ref[row["source_ref"]] for row in rows if row["source_ref"] in self.results_by_ref]


def _row(**overrides) -> dict[str, str]:
    base = {
        "_source_kind": "ad_hoc_workbook",
        "client_name": "Theo Sandford",
        "notes": "",
        "sessions_notes": "",
        "specific_sw": "",
        "sw_requested": "",
    }
    base.update(overrides)
    return base


class LlmGapFillingTests(unittest.TestCase):
    def test_row_that_parses_fine_is_never_sent_to_the_llm(self):
        rows = [_row(specific_day="2026-07-05", specific_time="10:00-12:00")]
        resolver = StubResolver({})
        _family_requests(
            rows, WORKER_LOOKUP, SERVICE_USER_LOOKUP, [],
            worker_rows=WORKER_ROWS, service_user_rows=SERVICE_USER_ROWS, llm_resolver=resolver,
        )
        # No gap rows at all means resolve_gaps is never invoked.
        self.assertEqual(len(resolver.calls), 0)

    def test_high_confidence_name_applied_medium_confidence_is_not(self):
        rows = [_row(sessions_notes="6 hrs pw on a Sat TT")]
        result = GapRowResult(
            source_ref="row-2",
            resolved_service_user_id=None,
            preferred_days=(5,),
            exact_date_intervals=(),
            requested_workers=(
                ResolvedPerson("Maya", "worker-maya", "high"),
                ResolvedPerson("Nell", "worker-nell", "medium"),
            ),
            avoided_workers=(),
            notes_addition="6 hours on Saturday, term time",
            note="",
        )
        issues = []
        resolver = StubResolver({"row-2": result})
        output = _family_requests(
            rows, WORKER_LOOKUP, SERVICE_USER_LOOKUP, issues,
            worker_rows=WORKER_ROWS, service_user_rows=SERVICE_USER_ROWS, llm_resolver=resolver,
        )
        self.assertEqual(len(output), 1)
        self.assertIn("worker-maya", output[0]["requested_worker_ids"].split("|"))
        self.assertNotIn("worker-nell", output[0]["requested_worker_ids"].split("|"))
        self.assertEqual(output[0]["preferred_days"], "5")
        self.assertTrue(any(issue.code == "llm_suggested_name_match" for issue in issues))
        self.assertTrue(any(issue.code == "llm_assisted_family_request" for issue in issues))

    def test_previously_dropped_row_is_recovered_with_audit_issue(self):
        rows = [_row(client_name="Theo Sandfrod", sessions_notes="6 hrs pw on a Sat TT")]  # misspelled, fails deterministic match
        result = GapRowResult(
            source_ref="row-2",
            resolved_service_user_id="su-theo",
            preferred_days=(5,),
            exact_date_intervals=(),
            requested_workers=(),
            avoided_workers=(),
            notes_addition="6 hours on Saturday",
            note="",
        )
        issues = []
        resolver = StubResolver({"row-2": result})
        output = _family_requests(
            rows, WORKER_LOOKUP, SERVICE_USER_LOOKUP, issues,
            worker_rows=WORKER_ROWS, service_user_rows=SERVICE_USER_ROWS, llm_resolver=resolver,
        )
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["service_user_id"], "su-theo")
        self.assertTrue(any(issue.code == "unresolved_name" for issue in issues))  # original warning still fires
        self.assertTrue(any(issue.code == "llm_recovered_family_request" for issue in issues))

    def test_unrecovered_dropped_row_stays_dropped(self):
        rows = [_row(client_name="Totally Unknown Child", sessions_notes="6 hrs pw on a Sat TT")]
        result = GapRowResult(
            source_ref="row-2", resolved_service_user_id=None, preferred_days=(),
            exact_date_intervals=(), requested_workers=(), avoided_workers=(), notes_addition="", note="",
        )
        resolver = StubResolver({"row-2": result})
        output = _family_requests(
            rows, WORKER_LOOKUP, SERVICE_USER_LOOKUP, [],
            worker_rows=WORKER_ROWS, service_user_rows=SERVICE_USER_ROWS, llm_resolver=resolver,
        )
        self.assertEqual(output, [])

    def test_call_limit_exceeded_degrades_to_a_warning_not_a_crash(self):
        rows = [_row(sessions_notes="6 hrs pw on a Sat TT")]
        resolver = StubResolver({}, fail_with=LlmCallLimitExceeded("reached max_llm_calls_per_run=1"))
        issues = []
        output = _family_requests(
            rows, WORKER_LOOKUP, SERVICE_USER_LOOKUP, issues,
            worker_rows=WORKER_ROWS, service_user_rows=SERVICE_USER_ROWS, llm_resolver=resolver,
        )
        self.assertEqual(len(output), 1)  # deterministic row still produced, just without LLM enrichment
        self.assertTrue(any(issue.code == "llm_call_limit_exceeded" for issue in issues))

    def test_either_or_pattern_becomes_a_family_request_not_extra_sessions(self):
        # "Saturday or Sunday" -> both days as soft preferred_days, never mechanically
        # expanded into concrete sessions -- ad hocs have no session-generation path at all.
        rows = [_row(sessions_notes="4 hrs pw, Saturday or Sunday")]
        result = GapRowResult(
            source_ref="row-2", resolved_service_user_id=None, preferred_days=(5, 6),
            exact_date_intervals=(), requested_workers=(), avoided_workers=(),
            notes_addition="4 hours, Saturday or Sunday", note="",
        )
        resolver = StubResolver({"row-2": result})
        output = _family_requests(
            rows, WORKER_LOOKUP, SERVICE_USER_LOOKUP, [],
            worker_rows=WORKER_ROWS, service_user_rows=SERVICE_USER_ROWS, llm_resolver=resolver,
        )
        self.assertEqual(output[0]["preferred_days"], "5|6")
        self.assertEqual(output[0]["preferred_time_ranges"], "")

    def test_llm_resolver_none_leaves_behavior_unchanged(self):
        rows = [_row(sessions_notes="6 hrs pw on a Sat TT")]
        output = _family_requests(
            rows, WORKER_LOOKUP, SERVICE_USER_LOOKUP, [],
            worker_rows=WORKER_ROWS, service_user_rows=SERVICE_USER_ROWS, llm_resolver=None,
        )
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["preferred_days"], "")
        self.assertEqual(output[0]["preferred_time_ranges"], "")


if __name__ == "__main__":
    unittest.main()
