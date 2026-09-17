"""Optional LLM-assisted parsing for care-provider ad hoc scheduling notes.

Deterministic parsing in ``workbook.py`` only understands rigid columns
(``specific_day``/``specific_time`` as a literal date and a clock range). Real
ad hoc notes are mostly free text -- "Saturday preferred, Sunday if not
possible", "6 hrs pw on a Sat TT, 18 hrs pw spread across 3 days in hols" --
which the deterministic parser cannot extract, leaving `family_requests.csv`
rows with empty `preferred_days`/`preferred_time_ranges` or, when the client
name itself is unresolved, dropping the row entirely.

This module fills exactly those gaps, and nothing else:

- Off by default. Only activates when ``ANTHROPIC_API_KEY`` is set, mirroring
  the opt-in pattern ``travel.py`` uses for live routing providers
  (``TravelConfig`` / ``live_travel_config_from_env``).
- Rules run first, always. The LLM only sees rows the deterministic parser
  already tried and failed on ("gap rows"); it never reprocesses rows that
  parsed fine.
- Every name is resolved against the known roster only, never invented. Only
  ``high``-confidence worker/service-user matches are auto-applied; anything
  else is surfaced as an audit warning for a human to resolve, matching the
  existing ``unresolved_name``/``ambiguous_reference_text`` posture.
- Cost-capped and batched: all gap rows for a run are sent in as few requests
  as possible (roster sent once per request), with a hard ceiling on requests
  per run enforced at the call boundary.

Schema, prompt, and confidence-gating rules were validated in practice against
29 real ad hoc rows spanning four months before being wired in here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from os import environ
from typing import Literal, Mapping, Sequence

DEFAULT_MODEL = "claude-sonnet-5"

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class LlmAdHocParsingConfig:
    enabled: bool = False
    anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    model: str = DEFAULT_MODEL
    max_llm_calls_per_run: int = 5
    max_rows_per_call: int = 20
    min_confidence_to_auto_apply: Literal["high", "medium", "low"] = "high"


def llm_config_from_env(base: LlmAdHocParsingConfig | None = None) -> LlmAdHocParsingConfig:
    """Enable LLM-assisted ad hoc parsing only when a usable API key is present."""

    resolved = base or LlmAdHocParsingConfig()
    has_key = bool(environ.get(resolved.anthropic_api_key_env))
    return replace(resolved, enabled=has_key)


class LlmCallLimitExceeded(RuntimeError):
    """Raised when a run would exceed ``max_llm_calls_per_run``."""


@dataclass
class LlmCallStats:
    calls_made: int = 0
    rows_sent: int = 0


@dataclass(frozen=True)
class ResolvedPerson:
    name_as_written: str
    resolved_id: str | None
    confidence: Literal["high", "medium", "low"]


@dataclass(frozen=True)
class DayPatternResult:
    days: tuple[int, ...]
    sessions_per_week: int


@dataclass(frozen=True)
class GapRowResult:
    source_ref: str
    resolved_service_user_id: str | None
    day_patterns: tuple[DayPatternResult, ...]
    exact_date_intervals: tuple[str, ...]
    requested_workers: tuple[ResolvedPerson, ...]
    avoided_workers: tuple[ResolvedPerson, ...]
    notes_addition: str
    note: str
    target_session_duration_minutes: int | None = None
    preferred_time_window_start: str | None = None
    preferred_time_window_end: str | None = None


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def meets_confidence(confidence: str, minimum: str) -> bool:
    return _CONFIDENCE_RANK.get(confidence, 0) >= _CONFIDENCE_RANK.get(minimum, 2)


_ROW_RESULT_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source_ref",
                    "resolved_service_user_id",
                    "day_patterns",
                    "exact_date",
                    "requested_workers",
                    "avoided_workers",
                    "notes_addition",
                    "note",
                    "target_session_duration_minutes",
                    "preferred_time_window_start",
                    "preferred_time_window_end",
                ],
                "properties": {
                    "source_ref": {"type": "string"},
                    "resolved_service_user_id": {"type": ["string", "null"]},
                    "day_patterns": {
                        "type": "array",
                        "description": (
                            "Independent day-selection groups. Almost always exactly ONE group -- only use "
                            "more than one when the note requires a fixed count from each of several distinct "
                            "day pools every week, e.g. '1 midweek + 1 weekend session/week' is two groups: "
                            "{days: [the midweek days in play], sessions_per_week: 1} and "
                            "{days: [\"saturday\",\"sunday\"], sessions_per_week: 1}. A plain 'Saturday preferred, "
                            "Sunday fallback, 1 session/week' is ONE group: "
                            "{days: [\"saturday\",\"sunday\"], sessions_per_week: 1} -- do not split a simple "
                            "priority list into multiple single-day groups. Empty array if there is no usable "
                            "day/count structure at all."
                        ),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["days", "sessions_per_week"],
                            "properties": {
                                "days": {
                                    "type": "array",
                                    "items": {"type": "string", "enum": list(_WEEKDAYS)},
                                    "description": "Ordered most preferred first within this group -- this order decides which day gets booked when more than one is feasible.",
                                },
                                "sessions_per_week": {
                                    "type": "integer",
                                    "description": "How many sessions/week come from this group specifically (not the request's total).",
                                },
                            },
                        },
                    },
                    "exact_date": {"type": ["string", "null"], "description": "YYYY-MM-DD, only when a real single date was given"},
                    "requested_workers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name_as_written", "resolved_id", "confidence"],
                            "properties": {
                                "name_as_written": {"type": "string"},
                                "resolved_id": {"type": ["string", "null"]},
                                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                            },
                        },
                    },
                    "avoided_workers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name_as_written", "resolved_id", "confidence"],
                            "properties": {
                                "name_as_written": {"type": "string"},
                                "resolved_id": {"type": ["string", "null"]},
                                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                            },
                        },
                    },
                    "notes_addition": {
                        "type": "string",
                        "description": "A short human-readable summary of the schedule (time window, duration, sessions/week) for the notes field",
                    },
                    "note": {
                        "type": "string",
                        "description": "At most one short sentence, only if there is a genuine ambiguity worth a human's attention. Empty otherwise.",
                    },
                    "target_session_duration_minutes": {
                        "type": ["integer", "null"],
                        "description": (
                            "Typical single-session length in minutes, shared by every group in day_patterns. "
                            "Null if the note gives no usable duration, or genuinely mixes incompatible session "
                            "lengths that one number can't represent (e.g. '1x6hr plus 2x3hr sessions') -- use "
                            "'note' to flag that rather than picking an average or guessing."
                        ),
                    },
                    "preferred_time_window_start": {
                        "type": ["string", "null"],
                        "description": "Earliest acceptable session start time of day, HH:MM 24-hour, only when the note gives a real time-of-day signal (e.g. 'not before 10.45am' -> '10:45'). Null if unspecified.",
                    },
                    "preferred_time_window_end": {
                        "type": ["string", "null"],
                        "description": "Latest acceptable session end time of day, HH:MM 24-hour, same rule as preferred_time_window_start. Null if unspecified.",
                    },
                },
            },
        }
    },
}


_SYSTEM_INSTRUCTION = (
    "You convert a UK support-work booking company's raw ad hoc scheduling notes into "
    "structured records, for MANY rows at once. Process every row given and return exactly "
    "one result per input row, echoing its source_ref exactly.\n\n"
    "CRITICAL: day_patterns is a list of independent day-selection groups, almost always "
    "exactly one group. A single ranked list (one group) can express 'most preferred single "
    "day' -- e.g. 'Saturday preferred, Sunday if Saturday not possible, 1 session/week' is "
    "ONE group: {days: ['saturday','sunday'], sessions_per_week: 1}, days ordered most "
    "preferred first since that order decides which day actually gets booked when both are "
    "feasible. A single list CANNOT express 'one from category A and one from category B every "
    "week' -- for that you need TWO groups. 'Not Wednesdays; 1 midweek + 1 weekend "
    "session/week' is two groups: {days: [the midweek days in play, e.g. monday/tuesday/"
    "thursday/friday], sessions_per_week: 1} and {days: ['saturday','sunday'], "
    "sessions_per_week: 1} -- do NOT flatten this into one 4-6 day list with "
    "sessions_per_week=2, that loses the 'one of each' requirement and can silently produce "
    "two midweek sessions and zero weekend ones. Only split into multiple groups when the "
    "note genuinely requires a fixed count from each of several distinct day pools; a plain "
    "'Saturday or Sunday, 1 session/week' stays one group, never two single-day groups. Leave "
    "day_patterns empty ([]) if the note gives no usable day/count structure at all.\n\n"
    "For every named person, resolve against the provided roster ONLY -- if a name does not "
    "clearly and confidently match, set resolved_id to null and confidence to low. Never "
    "invent an id not in the roster.\n\n"
    "IMPORTANT: not every name in the notes is a requested support worker. A name that "
    "appears as an administrative sign-off, a note-taker attribution, or a reference to who "
    "logged/reported an update (for example, a name appended after '(See text messages from "
    "X)' or similar phrasing, or a name that only appears alongside a date the note was "
    "recorded) is NOT a requested worker -- do not add it to requested_workers. Only include "
    "a name when the notes actually describe that person supporting, being requested for, or "
    "being avoided for the child's sessions. If genuinely unclear, leave it out and explain "
    "in 'note' instead of guessing.\n\n"
    "Only set exact_date (YYYY-MM-DD) when the note gives a real, specific calendar date -- "
    "leave it null for recurring weekly patterns with no concrete date attached.\n\n"
    "Summarize the schedule (time window, duration, sessions/week) in notes_addition, one or "
    "two short sentences. Keep 'note' empty unless there's a genuine ambiguity worth a "
    "human's attention -- most rows need no note at all.\n\n"
    "Also extract target_session_duration_minutes, shared across every group in day_patterns, "
    "describing the session length a planner could book directly. Prefer the term-time "
    "pattern when term-time and holiday patterns differ. Set it null when the note gives no "
    "usable duration, or when it genuinely mixes incompatible session lengths that one number "
    "can't represent (e.g. '1x6hr plus 2x3hr sessions') -- use 'note' to flag that rather than "
    "picking an average. Extract preferred_time_window_start/end (HH:MM) only from a real "
    "time-of-day signal in the note "
    "(e.g. 'not before 10.45am', 'PM preferred' is NOT precise enough on its own -- leave null "
    "unless an actual clock time or a standard band like 'morning'/'afternoon'/'evening' is "
    "given, mapped to 09:00-12:00/12:00-17:00/17:00-20:00 respectively)."
)


class AnthropicAdHocResolver:
    """Batches gap rows to Claude and returns structured per-row extractions.

    The ``anthropic`` package is imported lazily inside ``resolve_gaps`` so the
    base install of this project never requires it -- only installs that
    actually enable this optional feature (``pip install .[llm]``) need it.
    """

    name = "anthropic"

    def __init__(self, *, config: LlmAdHocParsingConfig, stats: LlmCallStats | None = None) -> None:
        self.config = config
        self.stats = stats or LlmCallStats()

    def resolve_gaps(
        self,
        rows: Sequence[Mapping[str, str]],
        workers: Sequence[Mapping[str, str]],
        service_users: Sequence[Mapping[str, str]],
    ) -> list[GapRowResult]:
        if not rows:
            return []
        results: list[GapRowResult] = []
        for start in range(0, len(rows), self.config.max_rows_per_call):
            chunk = rows[start : start + self.config.max_rows_per_call]
            results.extend(self._resolve_chunk(chunk, workers, service_users))
        return results

    def _resolve_chunk(
        self,
        chunk: Sequence[Mapping[str, str]],
        workers: Sequence[Mapping[str, str]],
        service_users: Sequence[Mapping[str, str]],
    ) -> list[GapRowResult]:
        if self.stats.calls_made >= self.config.max_llm_calls_per_run:
            raise LlmCallLimitExceeded(
                f"reached max_llm_calls_per_run={self.config.max_llm_calls_per_run}"
            )

        import anthropic  # noqa: PLC0415 -- deliberately lazy, see class docstring

        client = anthropic.Anthropic()
        rows_block = "\n\n".join(
            f"### {row['source_ref']}\n"
            f"Client name as written: {row.get('client_name', '')}\n"
            f"Raw notes:\n{row.get('raw_notes', '')}"
            for row in chunk
        )
        worker_roster = [{"id": w["worker_id"], "name": w["name"]} for w in workers]
        service_user_roster = [{"id": s["service_user_id"], "name": s["name"]} for s in service_users]
        user_content = (
            f"Known workers (id, name):\n{json.dumps(worker_roster, indent=2)}\n\n"
            f"Known service users (id, name):\n{json.dumps(service_user_roster, indent=2)}\n\n"
            f"Process every one of the following {len(chunk)} rows. Return exactly {len(chunk)} "
            f"results, one per row, each echoing its source_ref exactly:\n\n{rows_block}"
        )

        self.stats.calls_made += 1
        self.stats.rows_sent += len(chunk)
        with client.with_options(timeout=600).messages.stream(
            model=self.config.model,
            max_tokens=64000,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": _ROW_RESULT_JSON_SCHEMA}},
            system=_SYSTEM_INSTRUCTION,
            messages=[{"role": "user", "content": user_content}],
        ) as stream:
            response = stream.get_final_message()

        raw_text = "".join(block.text for block in response.content if block.type == "text")
        payload = json.loads(raw_text)
        return [_row_result_from_payload(item) for item in payload["results"]]


def _row_result_from_payload(item: dict) -> GapRowResult:
    day_patterns = tuple(
        DayPatternResult(
            days=tuple(_WEEKDAYS.index(day) for day in group["days"]),
            sessions_per_week=int(group["sessions_per_week"]),
        )
        for group in item.get("day_patterns") or []
    )
    exact_date_intervals: tuple[str, ...] = ()
    if item.get("exact_date"):
        exact_date_intervals = (item["exact_date"],)
    return GapRowResult(
        source_ref=item["source_ref"],
        resolved_service_user_id=item.get("resolved_service_user_id") or None,
        day_patterns=day_patterns,
        exact_date_intervals=exact_date_intervals,
        requested_workers=tuple(_resolved_person_from_payload(p) for p in item["requested_workers"]),
        avoided_workers=tuple(_resolved_person_from_payload(p) for p in item["avoided_workers"]),
        notes_addition=item.get("notes_addition", ""),
        note=item.get("note", ""),
        target_session_duration_minutes=item.get("target_session_duration_minutes") or None,
        preferred_time_window_start=item.get("preferred_time_window_start") or None,
        preferred_time_window_end=item.get("preferred_time_window_end") or None,
    )


def _resolved_person_from_payload(item: dict) -> ResolvedPerson:
    return ResolvedPerson(
        name_as_written=item["name_as_written"],
        resolved_id=item.get("resolved_id") or None,
        confidence=item["confidence"],
    )
