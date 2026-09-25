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
- Local SQLite persistence and recommendation audit storage exist.
- Local review UI exists for proposed-plan review and decision capture.
- Travel provider/cache foundation exists with postcode/geocode normalization,
  deterministic fallback estimates, provider-outage warnings, and mode-aware
  cache keys.

Partly done:

- Hard constraints exist and Phase 1 guardrails are covered by tests; broader
  production validation will continue as importer and UI surfaces are added.
- Travel is represented through a provider/cache interface; production external
  map and public-transport adapters are still pending.
- Exogenous events exist for worker unavailability, family requests, and stale
  service-user profiles; Phase 1 repair behavior is covered by tests.
- Local source-data examples exist; the first adapter path is available, but
  broader raw email/document adapters are still pending.

Not done:

- Data-quality validation report.
- Broader tenant-specific email/document adapters.
- Production Postgres persistence.
- API service.
- Production authenticated review UI.
- Production external map/public-transport workbook adapters.
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

Status: Done.

Goal: convert real tenant source files into the standardized contract.

Implemented:

- Added `booking_recommender.adapters.workbook` for workbook-shaped source exports.
- Reads source files as read-only inputs and writes derived standardized CSV v1
  outputs to a caller-provided output folder.
- Supports synthetic CSV exports for staff, service users, sessions,
  availability, matching, date requests, and ad hocs.
- Supports simple `.xlsx` extraction for observed booking workbook sheets:
  `Bookings`, `Childs`, `Support Workers`, `Availability`, and `Matching`.
- Supports observed July booking workbook columns such as `booking_date`,
  `booking_start_time`, `booking_finish_time`, `lead_sw_id`, `2nd_sw_id`,
  `2to1_required`, and `car`.
- Resolves messy `lead_sw_id` / `2nd_sw_id` import-sheet cells that include
  notes plus embedded SW names by matching against the staff source of truth.
- Reads Excel cell comments/notes on `lead_sw_id` / `2nd_sw_id` cells and uses
  them as SW-name fallbacks when visible cell values are opaque numeric IDs.
- Creates warning-backed placeholder workers from import-sheet SW IDs and cell
  notes when a booked SW is not present in the staff source, preserving the
  assignment while keeping the data-quality gap visible.
- Expands recurring import-sheet bookings into concrete weekly standardized
  sessions within the booking month; booking-shaped rows default to weekly
  recurrence unless explicitly marked as non-recurring.
- Expands explicitly recurring ad hoc request windows for supported frequencies
  such as weekly, fortnightly, and daily; ad hoc rows remain one-off when
  recurrence is absent because request wording can be more nuanced.
- Supports workbook-style staff/service-user ID and postcode workbook headings such
  as `sw_id`, `support_worker_id`, `su_id`, and service-user/support-worker
  postcode variants.
- Supports ignored tenant-local alias config at
  `RecommenderSystem/config/aliases.local.json` for worker aliases,
  service-user aliases, and local service-user records needed to correct source
  typos without mutating raw workbooks.
- Supports multiple booking workbooks in one source folder, including separate
  July import-sheet, staff/SU names-and-postcodes, and ad hoc request workbooks.
- Supports headerless workbook-style SU postcode sheets where the first two columns
  form the service-user display name and a later column contains postcode.
- Supports ad hoc workbook columns such as `client_name`, `specific_day`,
  `specific_time`, `sw_requested`, `sessions_notes`, and `notes_for_july` when
  they can be parsed deterministically.
- Discovers raw date-request `.eml`, `.docx`, and `.txt` files under date
  request folders, captures parseable notes and simple structured hints, and
  emits low-confidence warnings where manual review is still required.
- Discovers `.msg` date-request files and emits manual-review warnings rather
  than silently dropping or pretending to parse unsupported Outlook binary
  content.
- Parses text-extractable availability-report PDFs into standardized
  availability windows using `pypdf`; all-day rows become `08:00-23:00`
  bookable windows and explicit free-from/to pairs become separate availability
  windows.
- Excludes availability-report staff rows marked `(R)` because those workers
  were still in the recruitment pipeline when the source data was collected.
- Keeps availability-report PDF fallback warnings when the PDF cannot be read,
  no deterministic rows are found, or extracted worker names cannot be resolved
  against the staff source of truth.
- Emits structured adapter warnings/errors for missing sources, missing workbook
  sheets, unresolved names, duplicate IDs, and incomplete rows.
- Writes `adapter_report.json` and `import_manifest.json` with source refs.
- Adds `workbook-adapter` / `python -m booking_recommender.adapters.workbook` CLI with
  optional standardized-import validation.
- Adds synthetic workbook adapter fixtures under `fixtures/workbook_adapter_csv/`.
- Adds adapter tests for source immutability, standardized importer
  compatibility, CLI validation, missing required sources, July booking column
  variants, raw email request ingestion, and `.msg` manual-review warnings.

Operational follow-up:

- Tenant-local alias configuration now exists for reviewed exact corrections;
  broader fuzzy review workflow remains a later product feature beyond
  normalized exact-name/ID matching.
- Additional anonymized source shapes should be added as fixtures when new source
  workbook/report variants are captured.
- Binary Outlook `.msg` content still requires manual export to `.eml`, `.docx`,
  `.txt`, or a reviewed structured CSV until a dedicated parser dependency is
  approved.
- Availability-report PDF parsing is now supported for the observed
  text-extractable availability reports. Rows whose extracted names do not resolve
  against staff records still require alias/staff-source review.

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

Status: Done.

Goal: make recommendations reproducible and reviewable.

Recommended stack:

- FastAPI backend.
- Postgres database.
- SQLAlchemy or SQLModel.
- Background worker for imports and travel jobs.
- Object storage for original uploads in production.

Implemented:

- Added local relational persistence in `booking_recommender.persistence` using
  SQLite for development and tests, with a schema that maps to the planned
  Postgres backend.
- Added schema bootstrap/migration tracking through `schema_migrations`.
- Persists tenants, import runs, source file metadata, immutable planning
  snapshots, normalized records, recommendations, recommendation components,
  human decisions, override-reason catalogue rows, and outcome feedback rows.
- Persists allocation CLI runs with `--audit-db`, linking recommendations to
  import run, planning snapshot, session, worker, and service user.
- Adds query helpers for recommendations by session, worker, service user, and
  import run.
- Stores human decisions separately from generated recommendations.
- Requires override reasons when a non-top candidate is accepted or an override
  decision is recorded.
- Adds `recommender-db init` and `recommender-db seed-dev` commands for local
  database setup with synthetic development tenant data only.
- Adds tests for schema initialization, fixture allocation persistence,
  idempotent immutable snapshots, query paths, CLI audit persistence, and
  override-reason enforcement.
- Status update (September 2026): every persistence entry point (`allocate-sessions
  --audit-db`, `review-ui --audit-db`, `recommender-db init`/`seed-dev`, and the
  `booking_recommender.persistence` functions directly) now also accepts a
  `postgresql://...` DSN in place of a SQLite file path, selecting a Postgres
  backend with the same schema, same public API, and the same override-reason/
  audit-trail guarantees -- no other code changes needed. SQLite stays the
  zero-setup default for tests and local dev. Requires the optional
  `psycopg[binary]` dependency (`pip install 'staff-session-recommender[postgres]'`);
  tests for the Postgres path (`tests/test_persistence_postgres.py`) skip
  automatically when psycopg isn't installed or no local server answers.

Production follow-up:

- Self-hosted or managed Postgres now works as a persistence backend (see
  above); still pending is the FastAPI service layer itself, and production
  migration tooling (the current bootstrap is idempotent `CREATE TABLE IF NOT
  EXISTS`, not a versioned migration chain).
- Status update (September 2026): the software layer (Python package,
  Postgres, self-hosted OSRM) can now be packaged for a fresh machine via
  `docker-compose.yml`/`Dockerfile`, with Python dependencies pinned in
  `requirements-lock.txt` -- see `docs/DEPLOYMENT.md`. This is a local/
  self-hosted handoff, not the SaaS deployment target below: no auth, no
  tenant isolation, no TLS, no CI/CD.
- Store original uploads in object storage and persist object metadata instead
  of local source paths.
- Add API authentication/authorization and tenant isolation around persistence
  access.
- Add review UI flows that write `human_decisions` and later
  `outcome_feedback`.

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

- Design database schema. Done.
- Store import audit metadata. Done.
- Store immutable planning snapshots. Done.
- Store recommendation inputs and outputs. Done.
- Store accept/reject/override decisions. Done.
- Add migration tooling. Done for local SQLite schema bootstrap; production
  migration tooling remains a backend follow-up.
- Add seed data for development. Done for synthetic development tenant setup.

Acceptance criteria:

- A recommendation can be traced back to its import run and planning snapshot.
  Done.
- Human decisions are stored separately from generated recommendations. Done.
- Override reasons are required when a non-top candidate is selected. Done.
- Data can be queried by session, worker, service user, and import run. Done.

## Phase 8: Build The Review UI

Status: Done.

Goal: support bookings staff in reviewing and approving a proposed allocation
plan.

Implemented:

- Added dependency-light local review UI in `booking_recommender.review_ui`.
- Added `review-ui` / `python -m booking_recommender.review_ui` command.
- Loads standardized CSV v1 input, generates the proposed allocation plan, and
  persists recommendations to the existing SQLite audit database.
- Shows a compact operational dashboard with whole-plan counts for allocated,
  partially allocated, unfilled, warning, and approved sessions.
- Shows queues and filters for date, service user, worker, status/review state,
  priority, and page size.
- Shows session detail with assigned workers, remaining coverage, driver
  requirement, location, stale-profile/missing-travel/import warnings, ranked
  candidates, alternatives, hard failures, candidate warnings, score component
  breakdown, and fallback travel estimate details.
- Shows a worker day/route view built from proposed and existing allocations.
- Records accept, reject, and override decisions through the persistence audit
  layer; reject and override decisions require a reason, while non-top accepts
  continue to require override reasons through persistence.
- Derives review states for draft, accepted, rejected, needs information, and
  reviewed sessions.
- Paginates the session queue so real planning runs can be reviewed without
  rendering every session on the first page.
- Added focused review UI tests for dashboard counts, filters, rendered review
  evidence, and decision audit writes.

Production follow-up:

- Replace the local stdlib server with the planned authenticated multi-tenant
  backend/UI when the SaaS API layer is introduced.
- Add authenticated users, tenant isolation, object-storage upload flow,
  richer decision-state transitions, and concurrent reviewer handling.

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

Status: Done.

Goal: make travel a reliable hard constraint and scoring signal.

Implemented:

- Added `booking_recommender.travel` with a mode-specific travel provider
  interface and explicit provider failure error type.
- Added provider configuration with offline defaults:
  `travel_provider_order=["cache", "postcode_distance", "fallback"]`,
  `enable_external_travel=false`, external API env-var names, route timeouts,
  retry count, matrix/call caps, Google opt-in, and provider-failure fallback.
- Added postcode, area, coordinate, and time-bucket normalization helpers for
  stable cache identity.
- Added in-memory and SQLite estimate caches keyed by tenant, normalized origin,
  normalized destination, travel mode, day type, and departure-time bucket.
- Moved fallback travel estimates into the shared travel layer so CLI, review
  UI, planner tests, and future providers use the same semantics.
- Added `CachedTravelEstimator`, which uses provider estimates when available,
  serves cached estimates deterministically, and degrades to warning-backed
  fallback estimates when providers fail or return the wrong mode.
- Added `FallbackTravelProvider`, `PostcodeDistanceProvider`,
  `OpenRouteServiceProvider`, `OSRMProvider`, guarded `GoogleRoutesProvider`
  stub, and GTFS-ready `PublicTransportProvider` stub.
- External providers are not default paths. ORS and OSRM require explicit
  external-travel config plus env/config credentials; Google additionally
  requires `enable_google_routes=true`.
- Preserved hard travel constraints in scoring for travel mode, driver-required
  sessions, pre-session travel feasibility, next-session travel feasibility,
  route buffers, and max planned travel time.
- Surfaced warnings from both primary and next-session route estimates in
  recommendations.
- Allocation reports now include travel-provider counts, provider-failure
  counts, travel-infeasible candidate counts, and a warning when all accepted
  assignments use fallback travel.
- Added tests for cache keys, postcode/time-bucket normalization, cache reuse,
  provider contracts, fallback behavior, postcode-distance estimates, provider
  failures, provider mode mismatches, disabled external providers, Google opt-in
  guardrails, public-transport fallback behavior, report warnings, and
  slow-route hard failures.

Production follow-up:

- Configure and validate real ORS/OSRM endpoints in the deployment environment
  before enabling external travel for a tenant.
- Implement the full Google Routes API integration only if explicitly approved
  for a tenant with cost controls in place.
- Load tenant/region public-transport GTFS/TXC datasets, such as TfGM sources
  for Greater Manchester, behind the `PublicTransportProvider`.

Tasks:

- Define a travel provider interface. Done.
- Add postcode/geocode normalization. Done.
- Add cached fallback estimates. Done with in-memory and SQLite cache support.
- Add provider-backed estimates for drive, public transport, walk, cycle, and
  taxi where available. Done for offline fallback/postcode estimates, guarded
  ORS/OSRM integrations, Google opt-in stub, and GTFS-ready public transport
  stub.
- Include departure time, day of week, home location, previous session location,
  next session location, and route buffers. Done through estimator inputs,
  chronological origin selection, next-session checks, day-type cache keys, and
  time buckets.
- Store confidence and warnings with estimates. Done.
- Cache estimates by tenant, origin, destination, mode, and time bucket. Done.
- Add tests for cache keys, fallback behavior, and provider failures. Done.

Acceptance criteria:

- Travel failures block infeasible candidates. Done.
- Travel warnings are visible in recommendations. Done.
- Provider outages degrade to warnings or cached estimates, not silent bad data.
  Done.
- Drive and public-transport estimates are not treated as interchangeable. Done.

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
