# Handing this off to another machine

This walks a new machine from a clean clone to a running stack: Postgres,
self-hosted OSRM, self-hosted postcode geocoding, and the review UI, all via
Docker Compose (`docker-compose.yml`, `Dockerfile`).

Software (the compose file) and data (large geographic downloads, built once
per machine) are handled separately. The data never gets baked into an image
or committed to git -- it lives under `local-data/docker/`, which the existing
`local-data/` gitignore rule already covers.

## Prerequisites

- Docker Desktop (or another Docker Engine + Compose v2).
- This repo cloned locally. **On Windows, clone it inside your WSL2 distro's
  filesystem** (e.g. `~/RecommenderSystem`), not under `C:\Users\...` --
  see the performance note near the end of this doc for why.

## 1. Software layer

```sh
docker compose build
docker compose up -d postgres
```

`postgres` is the only service that starts by default -- `osrm` is behind a
profile (nothing to serve until its dataset exists, step 2), and `review-ui`
needs the data from steps 2-4 mounted in before it's useful.

Optional `.env` file in the repo root for anything you don't want the
defaults for:

```
POSTGRES_PASSWORD=choose-a-real-password
ORS_API_KEY=your-openrouteservice-key
```

## 2. Self-hosted OSRM (driving routes)

Download a region extract from Geofabrik (pick the region you need; this
project has been using Great Britain):

```sh
mkdir -p local-data/docker/osrm
curl -L -o local-data/docker/osrm/region.osm.pbf \
  https://download.geofabrik.de/europe/great-britain-latest.osm.pbf
```

Build the routing dataset (extract -> partition -> customize is the standard
OSRM pipeline; `car.lua` is bundled in the image at `/opt/car.lua`). This is
CPU/disk-heavy for a country-sized extract -- expect it to take a while and
use several GB of disk for a GB-sized region:

```sh
docker compose --profile routing run --rm osrm osrm-extract -p /opt/car.lua /data/region.osm.pbf
docker compose --profile routing run --rm osrm osrm-partition /data/region.osrm
docker compose --profile routing run --rm osrm osrm-customize /data/region.osrm
```

Then start it as a service:

```sh
docker compose --profile routing up -d osrm
```

`OSRM_BASE_URL=http://osrm:5000` is already wired into the `app` and
`review-ui` services in `docker-compose.yml`.

## 3. Self-hosted geocoding (UK postcodes)

Ordnance Survey Code-Point Open, free, no account needed (Great Britain
coverage only, not Northern Ireland):

```sh
mkdir -p local-data/docker/downloads local-data/docker/postcodes
curl -L -o local-data/docker/downloads/codepo_gb.zip \
  "https://api.os.uk/downloads/v1/products/CodePointOpen/downloads?area=GB&format=CSV&redirect"
docker compose --profile tools run --rm app \
  import-uk-postcodes /data/downloads/codepo_gb.zip /data/postcodes/uk_postcodes.sqlite
```

`UK_POSTCODE_DB_PATH=/data/postcodes/uk_postcodes.sqlite` is already wired in.

## 4. Public transport (GTFS, optional)

Download a GTFS feed for the tenant's region (e.g. TfGM's for Greater
Manchester, free open data at https://tfgm.com) and unzip it into
`local-data/docker/gtfs/` so it contains `stops.txt`, `stop_times.txt`, etc.
directly (not nested in a subfolder). `GTFS_FEED_PATH=/data/gtfs` is already
wired in. Skip this step if the tenant doesn't need public-transport
estimates -- the run still works without it.

## 5. Tenant data

Put a standardized CSV v1 folder (see `docs/NORMALIZED_DATA_CONTRACT.md`) at
`local-data/docker/standardized/` -- real tenant data (never committed) or the
synthetic fixture under `fixtures/standardized_csv_v1/` for a first smoke
test. Then:

```sh
# One-off allocation run
docker compose --profile tools run --rm app \
  allocate-sessions /data/standardized --live-travel --output-dir /data/allocation_outputs

# Persistent review UI at http://localhost:8787
docker compose up -d review-ui
```

## Everyday commands

```sh
docker compose run --rm --profile tools app <any console script> ...   # allocate-sessions, workbook-adapter, recommender-db, import-uk-postcodes, import-standardized-csv
docker compose logs -f review-ui
docker compose down            # stop everything, keep data volumes
docker compose down -v         # stop everything, also delete Postgres's volume
```

## A performance note for macOS and Windows

On a Linux host, containers are native processes -- no meaningful overhead
versus running the same binaries directly. Postgres's data lives in a named
Docker volume specifically so it's never affected by any of this, on any OS;
the caveats below are about the OSRM service's bind-mounted dataset directory
(`local-data/docker/osrm`), which OSRM memory-maps multi-GB index files out of
on every query.

**macOS**: Docker Desktop runs containers inside a Linux VM, and a
bind-mounted host directory crosses that VM boundary (via VirtioFS) on every
page fault -- noticeably slower than the native `osrm-routed` this project
also runs directly on the host (see `scripts/start-osrm.sh`).

**Windows**: Docker Desktop's WSL2 backend also runs containers in a Linux VM,
but where the *repo* lives changes everything. Clone it inside the WSL2
filesystem (e.g. `~/RecommenderSystem` from inside your WSL2 distro) and bind
mounts are genuine native Linux filesystem access -- no boundary at all, since
containers and that filesystem share the same kernel. Clone it on the Windows
side instead (`C:\Users\...`, seen from WSL2 as `/mnt/c/...`) and every bind
mount crosses a 9P-protocol bridge that is often *worse* than the macOS case.
This one setup choice matters more than anything else on Windows.

If OSRM routing feels slow under Docker, one of these boundaries is why; it's
not a problem with the dataset or the query itself.

## What this doesn't cover

Per `docs/BUILD_PLAN.md` Phase 12 ("Prepare SaaS Production"), this is a
single-tenant local/self-hosted stack, not a production deployment: no
authentication, no tenant isolation, no TLS, no backup automation, no CI/CD.
It gets a collaborator (or a fresh machine) to the same working state this
project reached locally -- nothing more.
