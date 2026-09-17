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
class GapRowResult:
    source_ref: str
    resolved_service_user_id: str | None
    preferred_days: tuple[int, ...]
    exact_date_intervals: tuple[str, ...]
    requested_workers: tuple[ResolvedPerson, ...]
    avoided_workers: tuple[ResolvedPerson, ...]
    notes_addition: str
    note: str


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
                    "day_selection",
                    "day_candidates",
                    "exact_date",
                    "requested_workers",
                    "avoided_workers",
                    "notes_addition",
                    "note",
                ],
                "properties": {
                    "source_ref": {"type": "string"},
                    "resolved_service_user_id": {"type": ["string", "null"]},
                    "day_selection": {
                        "type": "string",
                        "enum": ["fixed_all_listed", "pick_n_by_priority", "not_applicable"],
                    },
                    "day_candidates": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(_WEEKDAYS)},
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
                },
            },
        }
    },
}


_SYSTEM_INSTRUCTION = (
    "You convert a UK support-work booking company's raw ad hoc scheduling notes into "
    "structured records, for MANY rows at once. Process every row given and return exactly "
    "one result per input row, echoing its source_ref exactly.\n\n"
    "CRITICAL: distinguish 'this happens on all of these days every week' (day_selection="
    "fixed_all_listed) from 'pick one or more days per week from these options, not "
    "necessarily every one of them' (day_selection=pick_n_by_priority). 'Saturday preferred, "
    "Sunday if Saturday not possible' is pick_n_by_priority -- it is NOT both days every week. "
    "'Any midweek day' as a holiday-only top-up on a weekend pattern is also "
    "pick_n_by_priority, not every weekday. List every day that could plausibly be used in "
    "day_candidates regardless of day_selection -- priority ordering is not tracked here, "
    "only which days are genuinely in play.\n\n"
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
    "human's attention -- most rows need no note at all."
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
    day_candidates = tuple(_WEEKDAYS.index(day) for day in item["day_candidates"])
    exact_date_intervals: tuple[str, ...] = ()
    if item.get("exact_date"):
        exact_date_intervals = (item["exact_date"],)
    return GapRowResult(
        source_ref=item["source_ref"],
        resolved_service_user_id=item.get("resolved_service_user_id") or None,
        preferred_days=day_candidates,
        exact_date_intervals=exact_date_intervals,
        requested_workers=tuple(_resolved_person_from_payload(p) for p in item["requested_workers"]),
        avoided_workers=tuple(_resolved_person_from_payload(p) for p in item["avoided_workers"]),
        notes_addition=item.get("notes_addition", ""),
        note=item.get("note", ""),
    )


def _resolved_person_from_payload(item: dict) -> ResolvedPerson:
    return ResolvedPerson(
        name_as_written=item["name_as_written"],
        resolved_id=item.get("resolved_id") or None,
        confidence=item["confidence"],
    )
