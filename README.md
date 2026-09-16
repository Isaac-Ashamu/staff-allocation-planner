# Staff Session Recommender

Rebuild foundation for a tenant-agnostic support-worker allocation planner.

The recommender should be rules-first, optimization-aware, and explainable. It
should generate a complete proposed allocation plan for the planning period,
show the reasons and warnings behind each allocation, and let bookings staff
review, approve, adjust, or override the plan before it becomes final.

## Rebuild Scope

The active rebuild starts from a standardized import contract, not from raw
local spreadsheets, generated CSV outputs, or hard-coded file paths.

Current foundation:

- Tenant-agnostic domain models for workers, service users, sessions,
  compatibility, family requests, pair history, and planning state.
- Explainable scoring and planning modules retained as a starting point.
- Sequential-decision documentation for the intended allocation workflow.

Deferred until the standardized importer works:

- Raw company-specific spreadsheet/email/PDF adapters.
- Public-transport travel provider (drive/cycle/walk routing is now wired in;
  see "Live travel" below).
- Review dashboard or web UI.
- Feedback, incident, illness, and profile ingestion.

## SaaS Product Direction

The product should be company-agnostic. Tenant-specific concepts such as
support worker, service user, family request, approval rule, and booking system
import should be represented as configurable schemas and normalized into a
shared planning model.

Implementation map:

- [Build Plan](./docs/BUILD_PLAN.md)
- [Upgrade Roadmap](./docs/UPGRADE_ROADMAP.md)
- [Sequential Decision Model](./docs/SEQUENTIAL_DECISION_MODEL.md)
- [Normalized Data Contract](./docs/NORMALIZED_DATA_CONTRACT.md)
- SaaS core package: `core/python/booking_recommender`

Core deployable components:

- Multi-tenant web app for bookings teams.
- Import adapters for spreadsheets, booking systems, calendars, email requests,
  and HR/staff records.
- Normalized operational database for workers, service users, sessions,
  availability, constraints, skills, geography, and audit logs.
- Allocation service that scores candidate actions, generates full proposed
  plans, and reports unfilled or high-risk sessions.
- Travel-time service using postcode/geocode data, driving/public transport
  modes, and time-of-day estimates.
- Human review workflow for proposed plans, exceptions, warnings, overrides, and
  decision audit.
- Feedback loop from completed sessions, cancellations, incidents, and family
  comments.

## Sequential Decision Model

The booking problem is a sequential decision problem, not a single static
matching table. See [Sequential Decision Model](./docs/SEQUENTIAL_DECISION_MODEL.md)
for the formal state, action, decision, exogenous information, transition, and
objective definitions.

## Travel-Time Model

Travel must be a first-class constraint and score feature.

Worker travel variables:

- `worker.travel_modes`: allowed modes such as `drive`, `bus`, `tram`, `train`,
  `cycle`, `walk`, `taxi`.
- `worker.has_car`: whether the worker can drive to sessions.
- `worker.willing_areas`: configured areas/postcode sectors the worker can
  travel to.
- `worker.home_location`: postcode or geocode.
- `worker.current_location_at_t`: home, previous session location, college,
  work, or custom temporary location.

Session travel variables:

- `session.location`: service-user home, venue, school, activity location, or
  family pickup/drop-off point.
- `session.requires_driver`: hard requirement where family/SU needs car access.
- `session.travel_buffer_before_minutes`
- `session.travel_buffer_after_minutes`

Travel estimates:

- `travel_time(worker, from_location, to_location, mode, departure_time)`
- `travel_cost(worker, from_location, to_location, mode, departure_time)`
- `travel_feasible`: whether arrival before session start is possible after the
  previous commitment.

Driving and public transport should be estimated separately. A worker who drives
may be excellent for spread-out sessions, while a bus-only worker may be better
for dense Manchester routes or sessions near their home/college.

## Live travel

Planning runs are offline and deterministic by default. Add `--live-travel` to
`allocate-sessions` or `review-ui` to:

1. geocode every worker home and session postcode through postcodes.io
   (free, no key; bulk requests of up to 100 postcodes, cached), and
2. route with OpenRouteService if `ORS_API_KEY` is set (free tier:
   https://openrouteservice.org/dev/#/signup), or self-hosted OSRM if
   `OSRM_BASE_URL` is set.

Without routing credentials the run still geocodes and falls back to
coordinate-based postcode distance, which is already far better than the
20-minute default. Provider failures degrade to the next provider and are
recorded in the travel stats printed at the end of the run.

```powershell
$env:ORS_API_KEY = "..."
allocate-sessions .\standardized --live-travel --travel-cache-db .\cache\travel.sqlite
```

`--travel-cache-db` keeps both travel estimates (keyed by tenant, origin,
destination, mode, day type and half-hour bucket) and geocodes between runs, so
a monthly cycle re-routes only new worker/session pairs. Real postcodes never
need to enter the repository: the cache file lives with the tenant data.

## Current Scoring Direction

Hard filters:

- Do not suggest a worker already allocated to the same slot.
- Do not suggest a worker marked unavailable for the booking date/time.
- Do not suggest a worker explicitly marked inappropriate for the child.
- Do not suggest a worker who cannot meet travel or driver requirements once
  proper travel-time data is available.

Soft scoring:

- Explicit appropriate compatibility.
- Family/ad hoc request mentions the worker by name.
- Worker has a car where the booking appears to need car support.
- Staff and service-user postcode areas match.
- Estimated travel time and route practicality once maps data is integrated.
- Previous successful sessions, continuity, family preference, and worker load
  balance.

The output includes score reasons so staff can audit every recommendation.

## Rebuild Status

The previous local runner scripts have been removed from the active rebuild
path. The rebuild should start from a clean standardized import format instead
of hard-coded local paths and generated output CSVs.

Run the initial core tests once Python is available:

```powershell
$env:PYTHONPATH = ".\RecommenderSystem\core\python"
python -m unittest discover .\RecommenderSystem\core\python\tests
```

## Next Build Steps

- Define the v1 standardized CSV contract.
- Build a standardized CSV importer that populates `PlanningState`.
- Add validation warnings for missing IDs, invalid dates, duplicate sessions,
  missing workers, and ambiguous names.
- Normalize staff and service-user names/postcodes into stable IDs.
- Add worker identity sync for ID/name/alias cases.
- Add focused tests using small fixture CSVs.
- Add a review UI that shows the proposed allocation plan, unfilled sessions,
  ranked alternatives, conflicts, reasons, warnings, and an audit trail for
  human decisions.
