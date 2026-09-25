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

1. geocode every worker home and session postcode -- self-hosted from a local
   Ordnance Survey Code-Point Open database if `UK_POSTCODE_DB_PATH` is set
   (see "Self-hosted geocoding" below), otherwise through postcodes.io (free,
   no key; bulk requests of up to 100 postcodes, cached), and
2. route with self-hosted OSRM if `OSRM_BASE_URL` is set, or OpenRouteService
   if `ORS_API_KEY` is set (free tier: https://openrouteservice.org/dev/#/signup).
   When both are set, OSRM is tried first for drive-mode requests -- it has no
   request quota, while ORS's free tier does -- and ORS is used for whatever
   OSRM can't serve (it only ever handles `drive`).

Without routing credentials the run still geocodes and falls back to
coordinate-based postcode distance, which is already far better than the
20-minute default. Provider failures degrade to the next provider and are
recorded in the travel stats printed at the end of the run.

### Self-hosted OSRM

`local-data/osrm-gb/` (drive), `local-data/osrm-gb-bicycle/` (cycle), and
`local-data/osrm-gb-foot/` (walk) each hold a pre-built OSRM Great Britain
extract for one profile -- one running `osrm-routed` process serves exactly
one profile, so cycle and walk are separate instances/ports from drive, not a
flag on the same one. Built with `osrm-extract`/`osrm-partition`/
`osrm-customize` (MLD algorithm) against Geofabrik's
`great-britain-latest.osm.pbf`, using the matching `car.lua`/`bicycle.lua`/
`foot.lua` profile bundled with the `osrm-backend` Homebrew formula; all three
extracts can share one downloaded `.osm.pbf` (symlink it into each profile's
directory).

Use `scripts/start-osrm.sh [drive|cycle|walk]` / `scripts/stop-osrm.sh
[drive|cycle|walk]` to run one (default: `drive`) -- none survive
logout/reboot on their own, so re-run after either. Each prints the matching
env var to export: `OSRM_BASE_URL` (drive), `OSRM_CYCLE_BASE_URL` (cycle), or
`OSRM_WALK_BASE_URL` (walk). Configuring only some of the three is fine --
`live_travel_config_from_env` enables whichever are set, and any mode without
a self-hosted instance falls through to ORS (if configured) or postcode
distance, exactly like before this existed.

Note: `osrm-routed`'s argument parser mishandles an absolute `--base` path
that contains spaces, so the start script `cd`s into the data directory and
passes a relative filename -- keep that pattern if you invoke `osrm-routed`
by hand from a path like this repo's (`.../AI projects/RecommenderSystem/...`).

### Self-hosted geocoding

Set `UK_POSTCODE_DB_PATH` to a SQLite database built by
`import-uk-postcodes` (or `python -m booking_recommender.import_uk_postcodes_cli`)
from Ordnance Survey Code-Point Open, and every planning run resolves postcodes
from it instead of calling postcodes.io -- no network call, no rate limit, and
it's the one geocoding step every run needs (unlike routing, which only needs
live credentials for the worker/session pairs actually being scored). Coverage
is Great Britain only, not Northern Ireland.

```sh
curl -L -o codepo_gb.zip "https://api.os.uk/downloads/v1/products/CodePointOpen/downloads?area=GB&format=CSV&redirect"
import-uk-postcodes codepo_gb.zip local-data/cache/uk_postcodes.sqlite
export UK_POSTCODE_DB_PATH="$(pwd)/local-data/cache/uk_postcodes.sqlite"
```

Code-Point Open publishes OSGB36 National Grid Eastings/Northings, not WGS84
lat/lon; `booking_recommender.osgb36` converts each row (validated against
postcodes.io to within a few metres across GB -- see `tests/test_osgb36.py`).
Code-Point Open is free under the Open Government Licence, no account needed;
contains OS data (c) Crown copyright and database right.

Set `GTFS_FEED_PATH` to a directory of standard GTFS files (or a `.zip`) to
add scheduled bus/tram/train estimates for PUBLIC_TRANSPORT/BUS/TRAM/TRAIN
workers -- direct journeys and one-transfer journeys with walking access and
egress, no live disruption data. TfGM's feed is free, open data
(https://tfgm.com). Loading a full feed (tens of millions of stop-time rows)
costs a few seconds once per run, then every later query is instant. A GTFS
feed only covers the service dates it was published for -- test against a
date the feed's `calendar.txt`/`calendar_dates.txt` actually serves, not an
arbitrary past date.

```powershell
$env:ORS_API_KEY = "..."
allocate-sessions .\standardized --live-travel --travel-cache-db .\cache\travel.sqlite
```

`--travel-cache-db` keeps both travel estimates (keyed by tenant, origin,
destination, mode, day type and half-hour bucket) and geocodes between runs, so
a monthly cycle re-routes only new worker/session pairs. Real postcodes never
need to enter the repository: the cache file lives with the tenant data.

## Persistence backend

`allocate-sessions --audit-db`, `review-ui --audit-db`, and `recommender-db
init`/`seed-dev` accept either a SQLite file path (the zero-setup default for
tests and local dev) or a `postgresql://...` DSN, self-hosted or managed, with
no other code changes -- same schema, same query helpers, same override-reason
audit-trail rules. Requires the optional `psycopg[binary]` dependency:

```sh
pip install '.[postgres]'
allocate-sessions ./standardized --audit-db postgresql:///booking_recommender
```

## Handing this off / Docker

`docker-compose.yml` and `Dockerfile` package the software layer (the Python
package, Postgres, self-hosted OSRM) for a fresh machine. Python dependencies
are pinned in `requirements-lock.txt` (regenerate with `uv pip compile
pyproject.toml --all-extras -o requirements-lock.txt`). The large geographic
datasets (OSRM extract, UK postcode DB, GTFS feed) aren't part of the image --
see [Deployment](./docs/DEPLOYMENT.md) for the full walkthrough.

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
