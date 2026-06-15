# Travel Providers

The recommender supports multiple travel estimators behind one interface.

## Implemented

### `HeuristicTravelEstimator`

Local deterministic fallback.

- No network.
- No API key.
- Uses lat/long straight-line distance when available.
- Uses postcode-area fallback when lat/long is missing.
- Good for tests and offline development.
- Not good enough for real booking decisions.

### `GeocodedHeuristicTravelEstimator`

Cached postcode-distance estimator for all modes.

- Uses `PostcodesIoGeocoder` through `CachedGeocoder`.
- Stores postcode lookups in `output/travel-cache.json` by default.
- Estimates drive, taxi, public transport, bus, tram, train, cycle, and walk
  using mode-specific speeds over postcode-centroid straight-line distance.
- Better than area-only fallback because workers in the same postcode area no
  longer all receive the same travel time.
- Still not a true route or timetable. It should be treated as a ranking aid,
  not final operational proof that a journey is feasible.

### `PostcodesIoGeocoder`

UK postcode geocoding.

- Uses `https://api.postcodes.io`.
- No API key.
- Postcode-level, not property-level.
- Useful for UK prototypes and low-cost pilots.

### `OSRMTravelEstimator`

Route-time estimator using OSRM plus `PostcodesIoGeocoder`.

- Uses `https://router.project-osrm.org` by default.
- Supports driving, taxi, cycling, and walking style routes.
- Does not provide true public transport timetables.
- Falls back to `HeuristicTravelEstimator` when geocoding/routing fails.
- Adds warnings because the public OSRM endpoint is not a SaaS-grade dependency.

## Current Prototype Default

Recommendation and greedy-plan exports now default to:

```powershell
--travel-provider geocoded
```

This uses cached postcode geocoding without calling OSRM for every candidate.
For fully offline runs:

```powershell
--travel-provider heuristic
```

For small live-route experiments:

```powershell
--travel-provider osrm --top-n 1
```

The scoring layer also uses known worker allocations as travel context:

- If a worker has a previous allocated session before the candidate session,
  travel is estimated from that previous session location.
- If a worker has a later allocated session after the candidate session, the
  candidate is rejected when there is not enough time to reach the later
  session.
- If neither exists, travel is estimated from the worker home location.

## Are They Free?

Postcodes.io is presented as a free UK postcode lookup/geocoding tool and is
open-source/MIT licensed. It is suitable for prototype postcode geocoding.

OSRM is open-source. The public demo server is free to query lightly, but it is
best-effort, has usage expectations, gives no guarantees, and access can be
withdrawn. It should not be the dependency for a paid SaaS product unless you
self-host OSRM or use a commercial routing provider.

OpenRouteService has a free plan, but requires an account/API key and has quota
limits. It may be useful for development or lower-volume pilots, but a SaaS
needs tenant-aware quotas and billing assumptions.

Google Maps, Azure Maps, HERE, and similar providers are generally paid beyond
free trial or small quota levels. They are better candidates for production
where reliability, SLAs, transit coverage, and support matter.

## Production Recommendation

For a deployable SaaS:

- Use Postcodes.io or another geocoder only as a pluggable provider.
- Cache geocodes and route estimates.
- Add tenant-level provider configuration.
- Use paid/provider-backed travel estimates for production.
- Consider self-hosted OSRM/Valhalla for driving/cycling/walking if cost is a
  concern.
- Use a transit-aware provider for bus/tram/train. Do not infer real bus travel
  from driving routes.
