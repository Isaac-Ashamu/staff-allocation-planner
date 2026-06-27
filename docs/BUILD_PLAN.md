# Build Plan

This document turns the recommender roadmap into an implementation checklist. It
tracks what already exists, what should be built next, and what should wait until
the foundations are stable.

## Current Status

The project is in foundation stage.

Done:

- Tenant-agnostic Python domain model exists in `core/python/booking_recommender`.
- Explainable assignment scoring exists.
- Single-session candidate ranking exists.
- Greedy chronological allocation baseline exists.
- Project packaging exists with `pyproject.toml`.
- Repeatable unittest discovery exists from the repo root.
- Focused core tests cover scoring, transitions, travel constraints, greedy
  allocation, and exogenous repair.
- Sequential decision model is documented.
- Normalized data contract is documented.
- Executable standardized CSV v1 import contract is documented with synthetic
  fixtures.
- Standardized CSV v1 importer loads validated fixtures into `PlanningState`.
- First care-provider adapter converts synthetic workbook-shaped source exports into
  standardized CSV v1 output.
- Upgrade roadmap is documented.

Partly done:

- Hard constraints exist and Phase 1 guardrails are covered by tests; broader
  production validation will continue as importer and UI surfaces are added.
- Travel is represented in the model, but only via a pluggable estimator.
- Exogenous events exist for worker unavailability, family requests, and stale
  service-user profiles; Phase 1 repair behavior is covered by tests.
- Local source-data examples exist; the first adapter path is available, but
  broader raw email/document adapters are still pending.

Not done:

- Data-quality validation report.
- Broader tenant-specific email/document adapters.
- Persistent database.
- API service.
- Review UI.
- Real travel-time provider integration.
- Decision audit storage.
- Feedback/outcome learning loop.
- SaaS tenancy, security, deployment, and operations.

## Build Principles

- Keep source files read-only.
- Convert messy tenant data into a standardized contract before scoring.
- Make every recommendation explainable.
- Generate a complete proposed allocation plan, but keep human review,
  adjustment, approval, and audit in the workflow.
- Prefer rules and auditable constraints before machine learning.
- Store enough input state to reproduce every recommendation later.
- Keep tenant-specific parsing separate from tenant-agnostic planning.
- Protect personal and operational data from accidental commits or logs.

## Foundation Decisions To Lock Before Build

These decisions should be treated as part of the foundation, not deferred product
features.

- Availability means bookable session time. Travel buffers do not shrink a
  declared free window.
- Travel feasibility is separate from availability and must consider home,
  previous session, candidate session, next session, transport mode, departure
  time, confidence, and warnings.
- Names are display fields. Worker, service-user, session, and import-run IDs
  are the identity fields used by the planner.
- Aliases and fuzzy name matching belong in import/name-resolution code, not in
  scoring or planning.
- State transitions must be defensive. Invalid or duplicate actions must not
  corrupt assignments, hours, statuses, or allocations.
- Hard constraints should be explicit and test-covered before importer/UI work
  starts.
- Candidate ranking is a building block. The product goal is full-period
  allocation planning across all sessions.
- The greedy planner is the first full-plan baseline. It is useful for producing
  a complete draft plan and identifying blocked sessions, but it is not the final
  optimizer.
- Every imported row and generated recommendation should be traceable back to an
  import run, source reference, and validation outcome.
- Real tenant data belongs in ignored local folders or managed storage. Committed
  fixtures must be synthetic or anonymized.

## Phase 1: Stabilize The Core

Status: Done.

Goal: make the existing in-memory recommender trustworthy enough to build on.

Implemented with unittest coverage for hard constraints, soft scoring, defensive
state transitions, travel-mode and driver-route checks, greedy multi-worker
allocation, and exogenous repair after sickness or family changes.

Tasks:

- Add project packaging with `pyproject.toml`.
- Add a test command that works from a clean checkout.
- Add focused unit tests for the current public API.
- Fix unavailability transitions so removed assignments also update worker
  `current_allocations` and `current_hours`.
- Make `apply_assign_action` reject or ignore duplicate worker/session
  assignments.
- Enforce `worker.travel_modes` against returned travel estimates.
- Enforce driver requirements with a drive/car-capable route, not only
  `worker.has_car`.
- Treat declared availability as bookable session time, for example `14:00-17:00`
  can cover a `14:00-17:00` session.
- Keep travel buffers as separate route-feasibility checks between home, previous
  sessions, candidate sessions, and next sessions.
- Add guardrails for unknown worker IDs, unknown session IDs, and missing service
  users.
- Add guardrails for full sessions and actions that failed hard constraints.
- Make hard-constraint results easy to inspect in tests and recommendation
  output.
- Add tests for greedy allocation across multi-worker sessions.
- Add tests for exogenous repair after sickness or family changes.

Acceptance criteria:

- `python -m unittest` or `pytest` runs successfully from the repo root.
- Core tests cover hard constraints, soft scoring, state transitions, and greedy
  allocation.
- A worker removed from a session no longer keeps stale allocation or hour state.
- A bus-only worker cannot be recommended using a drive estimate.
- A driver-required session cannot pass with a walking/public-transport route.
- A worker declared free `14:00-17:00` can be considered available for a
  `14:00-17:00` session, while route feasibility is checked separately.
- Duplicate assignment attempts do not corrupt session assignments or hours.

## Phase 2: Protect Local And Tenant Data

Status: Done.

Goal: prevent sensitive operational files from being committed or mutated.

Implemented:

- Added `RecommenderSystem/local-data/` to `.gitignore`.
- Decided that real aliases are tenant/local configuration, not sample data.
- Removed real aliases from the active committed path and added a synthetic
  sample alias fixture under `RecommenderSystem/fixtures/`.
- Created a `fixtures/` area for tiny synthetic test data.
- Documented that raw tenant files are read-only source records.
- Added import audit metadata and row source-reference fields to the domain
  model before adapter work begins.

Tasks:

- Add `RecommenderSystem/local-data/` to `.gitignore`.
- Decide whether `config/aliases.json` is sample data, tenant data, or generated
  local configuration.
- Move real aliases into ignored local data or replace them with anonymized
  fixtures.
- Create a `fixtures/` area for tiny synthetic test data.
- Document the rule that raw tenant files are read-only.
- Add import audit metadata fields before building adapters.

Acceptance criteria:

- Real local source snapshots do not appear in `git status`.
- Test fixtures contain no real names, addresses, phone numbers, emails, or
  sensitive operational details.
- Import code writes derived output only to controlled workspace/database
  locations.

## Phase 3: Define The Executable Import Contract

Status: Done.

Goal: turn the normalized data contract into concrete files the importer can
validate and load.

Implemented:

- Expanded `docs/NORMALIZED_DATA_CONTRACT.md` with exact standardized CSV v1
  files, column names, field ownership, types, allowed values, list formats,
  date/time rules, ID rules, validation severities, optional-field behavior, and
  unknown compatibility semantics.
- Added a complete synthetic fixture folder under
  `fixtures/standardized_csv_v1/`.
- Added contract fixture tests that verify required files, headers, enum values,
  offset-aware date-times, and cross-table references before importer work
  starts.

Recommended v1 tables:

- `workers.csv`
- `service_users.csv`
- `sessions.csv`
- `availability.csv`
- `unavailability.csv`
- `compatibility.csv`
- `family_requests.csv`
- `pair_history.csv`

Tasks:

- Define exact column names, types, required fields, and allowed values.
- Decide how lists are represented in CSV, for example pipe-separated tags.
- Decide how dates and times are represented, including timezone rules.
- Define stable IDs for workers, service users, sessions, and import runs.
- Define severity levels for validation warnings.
- Add sample synthetic CSV fixtures.
- Document how missing optional fields are handled.
- Document how unknown compatibility should be represented.

Acceptance criteria:

- A developer can create valid synthetic CSVs by reading the contract.
- The contract maps cleanly to `PlanningState`.
- Each field has an owner: required, optional, derived, or future.
- Data-quality warnings are specified before parser code is written.

## Phase 4: Build The Standard Importer

Status: Done.

Goal: load the standardized contract into a validated `PlanningState`.

Implemented:

- Added `booking_recommender.importer` for standardized CSV v1 loading.
- Parses normalized workers, service users, sessions, availability,
  unavailability, compatibility, family requests, pair history, manifest audit,
  and source references into domain objects.
- Emits a structured `ImportReport` with deterministic row counts, warnings,
  errors, table names, row numbers, fields, and source refs.
- Rejects invalid imports with `StandardizedImportError`.
- Adds `import-standardized-csv` / `python -m booking_recommender.import_cli`
  summary CLI.
- Adds unit tests for valid fixture loading, invalid references/time windows,
  invalid assignments/compatibility, and CLI output.
- First end-to-end allocation CLI exists for standardized CSV imports, proposed
  greedy plans, readable reports, recommendation details, debug candidates, and
  derived output artifacts.

Tasks:

- Create an importer module under `booking_recommender`.
- Parse standardized tables into domain objects.
- Validate missing IDs, duplicate IDs, bad dates, invalid time ranges, and
  unknown references.
- Validate session start/end ordering.
- Validate worker availability and unavailability windows.
- Validate assigned workers on sessions.
- Validate compatibility records.
- Emit an import report with row counts, warnings, errors, and source refs.
- Add unit tests using synthetic fixtures.
- Add a CLI command that loads a standardized import folder and prints a summary.

Acceptance criteria:

- Valid fixture data loads into `PlanningState`.
- Invalid fixture data fails with clear validation errors.
- Warnings are structured, not only free text.
- Import output is deterministic.
- No tenant-specific workbook logic exists in the standardized importer.

## Phase 5: Build The First End-To-End Allocation CLI

Status: Done.

Goal: prove the full allocation workflow before building a UI.

Implemented:

- Added `allocate-sessions` / `python -m booking_recommender.allocation_cli`.
- Loads a standardized CSV v1 folder without mutating source files.
- Generates a greedy proposed allocation plan for open sessions.
- Prints a staff-readable report with allocated sessions, unfilled sessions,
  blocked candidates, scores, reasons, warnings, and import warnings.
- Preserves per-decision candidate recommendations from the full-plan planner.
- Supports `--include-infeasible` for hard-failure debugging.
- Supports `--compare-single-session` for comparing one-session ranking with the
  full greedy plan.
- Saves derived `allocation_report.json`, `planning_snapshot.json`,
  `proposed_plan.json`, `recommendation_details.json`, and
  `allocation_report.txt` artifacts to an output folder.
- Added tests for fixture allocation behavior, readable CLI output, derived
  artifact creation, debug candidates, and source-file immutability.

Tasks:

- Add a CLI entrypoint such as `allocate-sessions`.
- Load a standardized import folder.
- Generate a proposed allocation plan for all open sessions.
- Rank candidate alternatives for each proposed, unfilled, or problematic
  allocation.
- Output a readable plan report with assigned workers, unfilled sessions, scores,
  hard failures, warnings, and reasons.
- Add an option to include infeasible candidates for debugging.
- Add an option to compare one-session ranking with full-plan allocation.
- Save a planning snapshot, proposed plan, and recommendation details to a
  derived output folder.

Acceptance criteria:

- One command can produce a full proposed allocation plan from synthetic fixture
  data.
- Staff-readable reasons appear beside every proposed allocation.
- The report separates allocated sessions, unfilled sessions, and blocked
  candidates.
- The command never mutates source files.

## Phase 6: Build Tenant-Specific Adapters

Status: Partly done.

Goal: convert real tenant source files into the standardized contract.

Implemented:

- Added `booking_recommender.adapters.workbook` for workbook-shaped source exports.
- Reads source files as read-only inputs and writes derived standardized CSV v1
  outputs to a caller-provided output folder.
- Supports synthetic CSV exports for staff, service users, sessions,
  availability, matching, date requests, and ad hocs.
- Supports simple `.xlsx` extraction for observed booking workbook sheets:
  `Bookings`, `Childs`, `Support Workers`, `Availability`, and `Matching`.
- Emits structured adapter warnings/errors for missing sources, missing workbook
  sheets, unresolved names, duplicate IDs, and incomplete rows.
- Writes `adapter_report.json` and `import_manifest.json` with source refs.
- Adds `workbook-adapter` / `python -m booking_recommender.adapters.workbook` CLI with
  optional standardized-import validation.
- Adds synthetic workbook adapter fixtures under `fixtures/workbook_adapter_csv/`.
- Adds adapter tests for source immutability, standardized importer
  compatibility, CLI validation, and missing required sources.

Still pending:

- Direct parsing for raw date-request emails/documents.
- Tenant-local alias configuration and fuzzy review workflow beyond normalized
  exact-name/ID matching.
- More complete workbook-shape coverage as additional anonymized source shapes
  are captured.

Initial adapter targets:

- Monthly booking workbook.
- Child/service-user sheet.
- Support-worker sheet.
- Availability sheet.
- Matching sheet.
- Date-request emails and documents.
- Alias/name-resolution configuration.

Tasks:

- Create adapter modules outside the tenant-agnostic importer.
- Read source files as read-only inputs.
- Convert each source into standardized CSV/JSON tables.
- Emit adapter-specific warnings for ambiguous names and incomplete rows.
- Keep raw source refs in audit metadata.
- Add anonymized fixtures that mimic the source shapes.
- Add adapter tests using anonymized or synthetic files.

Acceptance criteria:

- Real workbook structure can be converted without touching source files.
- Missing tabs, renamed columns, and blank sheets produce clear warnings.
- Ambiguous names require review instead of silent matching.
- Adapter output can be loaded by the standardized importer.

## Phase 7: Add Persistence And Audit

Status: Pending.

Goal: make recommendations reproducible and reviewable.

Recommended stack:

- FastAPI backend.
- Postgres database.
- SQLAlchemy or SQLModel.
- Background worker for imports and travel jobs.
- Object storage for original uploads in production.

Core records:

- Tenant.
- Import run.
- Source file metadata.
- Normalized worker, service-user, session, availability, compatibility, and
  request records.
- Planning snapshot.
- Recommendation.
- Recommendation component.
- Human decision.
- Override reason.
- Outcome feedback.

Tasks:

- Design database schema.
- Store import audit metadata.
- Store immutable planning snapshots.
- Store recommendation inputs and outputs.
- Store accept/reject/override decisions.
- Add migration tooling.
- Add seed data for development.

Acceptance criteria:

- A recommendation can be traced back to its import run and planning snapshot.
- Human decisions are stored separately from generated recommendations.
- Override reasons are required when a non-top candidate is selected.
- Data can be queried by session, worker, service user, and import run.

## Phase 8: Build The Review UI

Status: Pending.

Goal: support bookings staff in reviewing and approving a proposed allocation
plan.

Core views:

- Proposed allocation plan.
- Unfilled sessions queue.
- Partially allocated sessions queue.
- High-risk or warning sessions queue.
- Session detail.
- Ranked candidate list.
- Ranked alternatives for each proposed allocation.
- Candidate hard failures and warnings.
- Score component breakdown.
- Worker day/route view.
- Data-quality warnings.
- Accept/reject/override workflow.
- Decision audit history.

Tasks:

- Build a compact operational dashboard.
- Show whole-plan status: allocated, partially allocated, unfilled, warning, and
  approved counts.
- Show candidate reasons without hiding warnings.
- Add filters for date, service user, worker, status, and priority.
- Add an override reason form.
- Add warnings for stale profiles, missing travel data, or ambiguous imports.
- Add review states: draft, reviewed, accepted, rejected, needs information.

Acceptance criteria:

- Staff can review one session without reading raw source files.
- Staff can review the whole proposed plan and drill into one session.
- Staff can see why each allocation was chosen and why alternatives were blocked.
- Accepting a recommendation records a decision audit entry.
- Rejecting or overriding a recommendation requires a reason.

## Phase 9: Add Real Travel-Time Support

Status: Pending.

Goal: make travel a reliable hard constraint and scoring signal.

Tasks:

- Define a travel provider interface.
- Add postcode/geocode normalization.
- Add cached fallback estimates.
- Add provider-backed estimates for drive, public transport, walk, cycle, and
  taxi where available.
- Include departure time, day of week, home location, previous session location,
  next session location, and route buffers.
- Store confidence and warnings with estimates.
- Cache estimates by tenant, origin, destination, mode, and time bucket.
- Add tests for cache keys, fallback behavior, and provider failures.

Acceptance criteria:

- Travel failures block infeasible candidates.
- Travel warnings are visible in recommendations.
- Provider outages degrade to warnings or cached estimates, not silent bad data.
- Drive and public-transport estimates are not treated as interchangeable.

## Phase 10: Upgrade The Planner

Status: Later.

Goal: move from candidate ranking to better whole-day plans.

Algorithm progression:

- Current: single-session candidate ranking.
- Next: greedy all-open-session allocation as the first full-plan generator.
- Then: repair plan after illness or family change.
- Then: beam search over session-order decisions.
- Later: mixed-integer optimization for daily allocation.
- Much later: learning policy only after enough clean outcome data exists.

Tasks:

- Add objective metrics for coverage, continuity, travel burden, fairness, and
  robustness.
- Add day-level route feasibility.
- Add reserve-capacity logic.
- Add repair planning for cancellations and sickness.
- Compare greedy vs beam search on synthetic scenarios.
- Add planner evaluation reports.

Acceptance criteria:

- Planner can explain tradeoffs at day level.
- Planner can show why one plan beats another.
- Staff can still override individual decisions.
- More advanced planners improve measurable outcomes over greedy baselines.

## Phase 11: Add Feedback And Learning

Status: Later.

Goal: improve scoring from real outcomes without losing explainability.

Signals:

- Completed session.
- Cancellation.
- Family feedback.
- Worker feedback.
- Incident.
- Lateness or travel issue.
- Accepted recommendation.
- Rejected recommendation.
- Override reason.
- Stale profile warning.

Tasks:

- Store outcome records.
- Add outcome import adapters.
- Summarize outcomes by worker/service-user pair.
- Feed safe outcome signals into `PairHistory`.
- Add reports for acceptance rate and override reasons.
- Review fairness risks before adding reliability penalties.
- Keep scoring components visible.

Acceptance criteria:

- Outcomes can update pair history.
- Scoring changes remain explainable.
- Sensitive absence or health-related signals are not used unfairly.
- Staff can see when feedback affected a future recommendation.

## Phase 12: Prepare SaaS Production

Status: Later.

Goal: make the system safe to run for multiple tenants.

Tasks:

- Add tenant isolation.
- Add role-based access control.
- Add authentication.
- Add encryption and backup policies.
- Add audit-log retention policies.
- Add data export and deletion workflows.
- Add observability for imports, recommendations, travel calls, and UI actions.
- Add production deployment scripts.
- Add staging environment.
- Add privacy and data-protection review.
- Add incident response process.

Acceptance criteria:

- Tenant data cannot cross boundaries.
- All sensitive actions are audited.
- Backups can be restored.
- Imports and recommendation jobs are observable.
- The system has documented operational ownership.

## Immediate Next Sprint

Recommended first sprint:

- Add `RecommenderSystem/local-data/` to `.gitignore`.
- Add `pyproject.toml` and a working test command.
- Create `core/python/tests`.
- Add tests for scoring and transitions.
- Fix stale worker allocation/hour state after unavailability.
- Prevent duplicate assignment state corruption.
- Enforce travel modes and driver route constraints.
- Add the first synthetic standardized CSV fixture.

Sprint outcome:

- The core is safer.
- The repo is less likely to leak local source data.
- The next developer can run tests and begin the importer confidently.

## Definition Of Finish

The project is finished enough for operational use when:

- Staff can import source data.
- The system validates that data and reports quality issues.
- The system generates a proposed allocation plan for all sessions in the
  planning period.
- Staff can view allocated, partially allocated, unfilled, and warning sessions.
- Staff can see why each allocation was chosen, with ranked alternatives,
  reasons, and warnings.
- Travel and hard constraints block unsafe recommendations.
- Staff can approve the full plan or accept, reject, or override individual
  allocations.
- Every decision is audited.
- Outcomes can be fed back into future planning.
- Source data remains protected.
- Recommendations are reproducible from stored snapshots.

The project is finished enough for SaaS when the above is multi-tenant,
permissioned, monitored, backed up, and covered by data-protection processes.
