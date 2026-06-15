# Staff Session Recommender

Deployable SaaS concept for shortlisting support workers for requested or
unfilled family sessions.

The current PowerShell script is only a local data-inspection prototype. It was
used because this Windows shell could not run Python and Excel COM was
unavailable. The SaaS product should not be implemented in PowerShell. The core
service should be a portable backend, for example Python/FastAPI or
TypeScript/NestJS, with a database, background jobs, mapping/travel-time
providers, and an explainable recommendation engine.

The recommender should be rules-first, optimization-aware, and explainable. It
should help bookings staff see good candidates and the reason for each
suggestion, not make final allocation decisions without review.

## Data Sources Reviewed

Primary monthly booking workbook:

- `C:\path\to\source-data\booking-import.xlsx`
- Sheets found: `Bookings`, `Childs`, `Support Workers`, `Availability`, `Matching`
- Useful fields:
  - Booking date/start/finish
  - Child ID/name
  - 2:1 requirement
  - Lead and second support worker IDs
  - Staff availability/unavailability
  - Explicit child-worker appropriateness
  - Staff car flag and comments

Family requests and ad hocs:

- `data\July 2026 Ad hocs.xlsx`
- Sheets found: `Phase 1`, `Phase 2`
- Useful fields:
  - Specific day/time preferences
  - Specific requested support workers
  - Session frequency notes
  - Draft/booked-in status

Date request folder:

- `C:\path\to\source-data\Date requests`
- Contains subfolders:
  - `Added to system with sw`
  - `Added to system without sw`
- These `.msg` files should be parsed later for exact email/body requests. The
  current prototype uses the structured ad hoc workbook first.

Staff and service-user geography:

- `data\Staff and SU names and postcode.xlsx`
- Sheets found: `STAFF`, `SU`
- Useful fields:
  - Staff postcode and travel mode
  - Service-user postcode and support level
  - Languages spoken

Availability reports:

- `data\availability report\*.pdf`
- Useful fields:
  - Staff daily unavailable flag
  - Staff all-day availability
  - Staff partial availability windows
  - Year-to-date availability coverage

Reliability and risk signals:

- `data\Support Worker Illness Record.xlsx`
- Useful fields:
  - Illness/cancellation history
  - Cover group tracker
  - Date and reason for cancellations

Historical session outcomes:

- `data\MASTER provider feedback (Responses) - MASTER Form responses 1.csv`
- `data\Incident report form (Responses) - Form Responses 1.csv`
- Useful fields:
  - Activities that worked for a service user
  - Repeated issues/incidents
  - Profile freshness warnings
  - Support worker/service user pair history

Profiles and supported-connection signals:

- `data\profiles\ingested\all-profile-index.json`
- `data\profiles\ingested\current\*.connection-profile.json`
- `SupportedConnections\data\canonical\current-profiles.*`
- Useful fields:
  - Support level and 2:1 indicators
  - Interests and suggested activities
  - Communication supports
  - Triggers, dislikes, risks, and safeguarding notes

## SaaS Product Direction

The product should be company-agnostic. Tenant-specific concepts such as
support worker, service user, family request, approval rule, and booking system
import should be represented as configurable schemas and normalized into a
shared planning model.

Implementation map:

- [Upgrade Roadmap](./docs/UPGRADE_ROADMAP.md)
- [Sequential Decision Model](./docs/SEQUENTIAL_DECISION_MODEL.md)
- [Normalized Data Contract](./docs/NORMALIZED_DATA_CONTRACT.md)
- [Travel Providers](./docs/TRAVEL_PROVIDERS.md)
- SaaS core package: `core/python/booking_recommender`
- Local spreadsheet prototype: `scripts/Export-Recommendations.ps1`

Core deployable components:

- Multi-tenant web app for bookings teams.
- Import adapters for spreadsheets, booking systems, calendars, email requests,
  and HR/staff records.
- Normalized operational database for workers, service users, sessions,
  availability, constraints, skills, geography, and audit logs.
- Recommendation service that scores candidate actions and can run batch
  allocation simulations.
- Travel-time service using postcode/geocode data, driving/public transport
  modes, and time-of-day estimates.
- Human review workflow with reasons, warnings, overrides, and decision audit.
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

## Current Prototype Scoring Model

Hard filters:

- Do not suggest a worker already allocated to the same slot.
- Do not suggest a worker marked unavailable for the booking date/time.
- Do not suggest a worker explicitly marked inappropriate for the child.
- Do not suggest a worker who cannot meet travel or driver requirements once
  proper travel-time data is available.

Soft scoring:

- Explicit appropriate match in the `Matching` sheet.
- Family/ad hoc request mentions the worker by name.
- Worker has a car where the booking appears to need car support.
- Staff and service-user postcode areas match.
- Staff has comments/experience in the monthly support-worker sheet.
- Estimated travel time and route practicality once maps data is integrated.
- Previous successful sessions, continuity, family preference, and worker load
  balance.

The output includes score reasons so staff can audit every recommendation.

## Run

Export ranked recommendations through the Python SaaS core:

```powershell
python .\RecommenderSystem\core\python\export_workbook_recommendations.py
```

Default output:

```text
RecommenderSystem\output\july-2026-recommendations-python.csv
```

Export a provisional greedy allocation plan:

```powershell
python .\RecommenderSystem\core\python\plan_workbook_allocations.py
```

Default output:

```text
RecommenderSystem\output\july-2026-greedy-plan.csv
```

Both commands default to cached postcode-geocoded heuristic travel. This avoids
bulk route calls while separating candidates by approximate postcode distance
instead of only same-area/different-area buckets. For a fully offline run:

```powershell
python .\RecommenderSystem\core\python\export_workbook_recommendations.py --travel-provider heuristic
```

For a small live-route experiment:

```powershell
python .\RecommenderSystem\core\python\export_workbook_recommendations.py --travel-provider osrm --top-n 1
```

Install Python parser dependencies if needed:

```powershell
python -m pip install -r .\RecommenderSystem\core\python\requirements.txt
```

Run the older local PowerShell spreadsheet prototype:

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\RecommenderSystem\scripts\Export-Recommendations.ps1
```

Legacy output:

```text
RecommenderSystem\output\july-2026-recommendations.csv
```

Run the SaaS-core demo once Python is available:

```powershell
python .\RecommenderSystem\core\python\demo_scenario.py
```

Run the initial core tests once Python is available:

```powershell
$env:PYTHONPATH = ".\RecommenderSystem\core\python"
python -m unittest discover .\RecommenderSystem\core\python\tests
```

## Next Build Steps

- Convert spreadsheet import code into Python import adapters that populate
  `PlanningState`.
- Parse `.msg` date request files into structured requested dates/times/workers.
- Normalize staff and service-user names/postcodes into stable IDs.
- Replace heuristic travel estimates with a provider-backed travel-time service.
- Add reliability features from illness/cover records, with careful weighting so
  health-related absence is not used unfairly.
- Add feedback-derived pair history: previous successful sessions, incidents,
  family comments, and profile-outdated warnings.
- Add a review UI that shows ranked candidates, conflicts, reasons, and an audit
  trail for human decisions.
