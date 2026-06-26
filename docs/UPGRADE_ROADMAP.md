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

Status: deferred until the core foundation is stable.

Travel should come back after the standardized importer and allocation planner
are stable.

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
