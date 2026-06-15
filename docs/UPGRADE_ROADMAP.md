# Upgrade Roadmap

This roadmap moves the project from a local spreadsheet prototype to a
deployable, tenant-agnostic SaaS recommender.

## Phase 0: Protect Source Data

Status: implemented as project rule.

- Treat shared drives, booking exports, profile folders, and HR files as read-only
  source systems.
- Write all derived outputs into the application workspace or cloud database.
- Keep every import run auditable: source path, timestamp, row count, warnings,
  and generated state snapshot ID.

## Phase 1: Domain Model and Scoring Core

Status: implemented in `core/python/booking_recommender`.

Goal: define the normalized planning language independent of provider spreadsheets.

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

Implemented services:

- Travel estimation interface.
- Heuristic travel estimator by mode.
- Hard-constraint checker.
- Explainable action scorer.
- Single-session ranking planner.

## Phase 2: Import Adapters

Status: initial provider workbook adapters implemented.

Build adapters that map company-specific files into the shared domain model.

Adapters:

- Monthly booking workbook adapter.
- Staff and service-user postcode workbook adapter.
- Ad hoc/date request workbook adapter.
- Email `.msg` date request adapter. Pending.
- Availability PDF adapter. Implemented for current report layout.
- Calendar adapter. Pending.
- Feedback/incidents adapter.
- Profile JSON adapter.

Outputs:

- Normalized `PlanningState`.
- Data-quality report.
- Name-resolution warnings.
- Import audit record.

## Phase 3: Travel-Time Service

Status: prototype provider implemented.

The current core has a deterministic heuristic estimator and an OSRM/Postcodes.io
prototype estimator. SaaS deployment still needs provider-backed estimates with
quotas, monitoring, caching, attribution, and tenant configuration.

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

## Phase 4: Sequential Planner

Status: single-session ranking implemented, full sequential planning pending.

Upgrade from candidate ranking to plan optimization:

- Rank actions for one unfilled role.
- Allocate all roles for one session.
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

Status: design pending.

Recommended stack:

- Python/FastAPI or TypeScript/NestJS API.
- Postgres for normalized data and audit logs.
- Redis/queue for import and travel-estimation jobs.
- Object storage for original uploaded files.
- Background workers for scoring, batch planning, and map calls.

Core API endpoints:

- `POST /imports`
- `GET /planning-states/{id}`
- `POST /recommendations/session/{session_id}`
- `POST /recommendations/day`
- `POST /decisions/{recommendation_id}/accept`
- `POST /decisions/{recommendation_id}/reject`
- `GET /audit/{session_id}`

## Phase 6: Review UI

Status: pending.

The UI should support bookings staff, not hide complexity.

Views:

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
