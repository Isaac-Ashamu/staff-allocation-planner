# Upgrade Roadmap

This roadmap moves the project from the current foundation to a deployable,
tenant-agnostic SaaS recommender.

## Phase 0: Protect Source Data

Status: implemented as project rule.

- Treat tenant source files and operational systems as read-only source systems.
- Write all derived outputs into the application workspace or cloud database.
- Keep every import run auditable: source path, timestamp, row count, warnings,
  and generated state snapshot ID.

## Phase 1: Domain Model and Scoring Core

Status: implemented in `core/python/booking_recommender`.

Goal: define the normalized planning language independent of tenant-specific
source files.

Implemented objects:

- `TenantRules`
- `Location`
- `Worker`
- `ServiceUser`
- `Session`
- `AvailabilityWindow`
- `Allocation`
- `Compatibility`
- `PairHistory`
- `FamilyRequest`
- `PlanningState`
- `AssignAction`
- `Recommendation`

Implemented or retained core services:

- Hard-constraint checker.
- Explainable action scorer.
- Single-session ranking planner.
- Greedy full-plan allocation baseline.

## Phase 2: Import Adapters

Status: deferred until the core foundation is stable.

Build adapters that map company-specific files into the standardized import
contract. Raw adapters should not bypass the standardized contract.

Adapters:

- Monthly booking workbook adapter.
- Staff and service-user postcode workbook adapter.
- Ad hoc/date request workbook adapter.
- Email/date request adapter.
- Availability report adapter.
- Calendar adapter. Pending.
- Feedback/incidents adapter.
- Profile JSON adapter.

Outputs:

- Normalized `PlanningState`.
- Data-quality report.
- Name-resolution warnings.
- Import audit record.

## Phase 3: Travel-Time Service

Status: wired end to end (September 2026). `allocate-sessions --live-travel`
and `review-ui --live-travel` geocode every worker home and session postcode
through postcodes.io (free, keyless, bulk lookups of 100) and route through
OpenRouteService when `ORS_API_KEY` is set or self-hosted OSRM when
`OSRM_BASE_URL` is set. Provider order is cache -> live providers ->
postcode distance -> fallback, so an outage degrades to an approximation
rather than failing the run. `--travel-cache-db` persists both the travel
estimates and the geocodes between monthly runs. Off by default: without the
flag nothing calls an external service.

Status update (September 2026): a real GTFS static-feed provider now exists
(`GtfsTransitProvider`), ported from an earlier unmerged prototype and adapted
to the current provider-chain architecture. Direct and one-transfer journeys,
walking access/egress, real service-calendar resolution (weekday pattern plus
calendar_dates.txt exceptions). Enabled via `GTFS_FEED_PATH`; tested against
TfGM's real feed at 53% pair coverage across a 70-postcode Manchester sample
(the other 47% correctly found no same-day scheduled connection within the
conservative one-transfer/45-minute-wait budget, and fall through to
postcode-distance). No live disruption data, no multi-transfer search --
those remain open.

Status update (September 2026): geocoding can now also be fully self-hosted.
Setting `UK_POSTCODE_DB_PATH` to a database built by `import-uk-postcodes`
from Ordnance Survey Code-Point Open (`booking_recommender.uk_postcodes`,
free, Open Government Licence, GB coverage only) resolves postcodes locally
instead of calling postcodes.io -- no network call, no rate limit. Code-Point
Open publishes OSGB36 National Grid Eastings/Northings; `booking_recommender.
osgb36` converts to WGS84 (validated against postcodes.io to within a few
metres across GB). Provider order now also tries self-hosted OSRM before the
metered ORS free tier for drive-mode requests when both are configured, since
OSRM only ever serves `drive` and has no request quota.

Remaining: multi-transfer journeys, live disruption/delay data, and Google
Routes if a tenant wants it over OpenRouteService.

Provider options:

- Google Maps Distance Matrix.
- Azure Maps.
- HERE.
- OpenRouteService plus public transport provider.
- Self-hosted OSRM/Valhalla/GraphHopper for drive/cycle/walk.
- Cached postcode centroid fallback.

Required behavior:

- Estimate driving, public transport, cycling, walking, and taxi separately.
- Use departure time, day of week, previous session location, and buffers.
- Cache estimates by tenant, origin, destination, mode, and time bucket.
- Surface uncertainty when only postcode-level estimates are available.

Implemented foundation:

- Mode-specific provider interface.
- Postcode, area, coordinate, and time-bucket normalization.
- Tenant/mode/location/day-type/time-bucket cache keys.
- In-memory and SQLite travel caches.
- Offline fallback and postcode-distance providers.
- Guarded OpenRouteService and OSRM providers, disabled unless configured.
- postcodes.io geocoder with in-memory and SQLite geocode caches (misses cached
  too, so a mistyped postcode costs one request, not one per run).
- `live_travel_config_from_env` / `build_live_travel_estimator` assembly and the
  `--live-travel` / `--travel-cache-db` CLI flags; postcodes are bulk-prefetched
  before planning starts.
- Google Routes later-stage provider stub, disabled by default with explicit
  opt-in and cost-control configuration.
- GTFS-ready public transport provider stub for future TfGM/GTFS/TXC schedule
  ingestion.
- Deterministic warning-backed fallback estimates.
- Provider failure and provider mode-mismatch warnings in recommendations.

## Phase 4: Sequential Planner

Status: single-session ranking and greedy full-plan baseline implemented; stronger
sequential planning pending.

Upgrade from candidate ranking and greedy full-plan generation to stronger plan
optimization:

- Rank actions for one unfilled role.
- Allocate all roles for one session.
- Generate a proposed allocation plan for all open sessions in the planning
  period.
- Allocate one day with worker route constraints.
- Repair plans after exogenous events such as illness or family changes.
- Simulate likely disruptions and preserve reserve capacity.

Candidate algorithms:

- Greedy explainable baseline.
- Beam search over session-order decisions.
- Mixed-integer optimization for daily batch allocation.
- Reinforcement-learning style policy later, only after enough clean outcome
  data exists.

## Phase 5: SaaS Backend

Status: design pending. Postgres itself is no longer pending, though: every
persistence entry point accepts a `postgresql://...` DSN in place of a SQLite
path (self-hosted or managed) with the same schema and API -- see Phase 7 in
`docs/BUILD_PLAN.md`. What's still pending here is the API/service layer
around it.

Recommended stack:

- Python/FastAPI or TypeScript/NestJS API.
- Postgres for normalized data and audit logs.
- Redis/queue for import and travel-estimation jobs.
- Object storage for original uploaded files.
- Background workers for scoring, batch planning, and map calls.

Core API endpoints:

- `POST /imports`
- `GET /planning-states/{id}`
- `POST /plans`
- `GET /plans/{plan_id}`
- `POST /plans/{plan_id}/approve`
- `POST /recommendations/session/{session_id}`
- `POST /recommendations/day`
- `POST /decisions/{recommendation_id}/accept`
- `POST /decisions/{recommendation_id}/reject`
- `GET /audit/{session_id}`

## Phase 6: Review UI

Status: pending.

The UI should support bookings staff, not hide complexity.

Views:

- Proposed allocation plan.
- Unallocated sessions queue.
- Session detail with family request, risks, and profile summary.
- Ranked worker candidates with reasons and warnings.
- Worker route/day view.
- Conflict and data-quality warnings.
- Override capture.
- Outcome feedback after session completion.

## Phase 7: Learning Loop

Status: pending.

Use completed-session outcomes to improve scores.

Signals:

- Session completed/cancelled.
- Family feedback.
- Worker feedback.
- Incidents/issues.
- Profile-outdated warnings.
- Travel estimates vs actual lateness/cancellations.

Guardrails:

- Keep scoring explainable.
- Do not let sensitive health-related absence become an unfair penalty.
- Require human review for high-risk or low-confidence recommendations.
- Monitor recommendation acceptance, override reasons, and outcome quality by
  tenant.
