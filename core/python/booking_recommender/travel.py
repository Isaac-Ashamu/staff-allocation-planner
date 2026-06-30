"""Travel estimation providers, normalization, and caching."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Protocol

from .models import Location, TravelEstimate, TravelMode, Worker


class TravelProvider(Protocol):
    """Mode-specific travel provider interface.

    Production implementations can wrap APIs such as Google Maps, HERE, OSRM,
    or a public-transport provider. Implementations should return estimates for
    the requested mode only.
    """

    name: str

    def estimate(
        self,
        *,
        origin: Location,
        destination: Location,
        mode: TravelMode,
        departure_time: datetime,
    ) -> TravelEstimate:
        ...


class TravelProviderError(RuntimeError):
    """Raised when a provider cannot safely return an estimate."""


class TravelEstimateCache(Protocol):
    def get(self, key: tuple[str, str, str, str, str, str]) -> TravelEstimate | None:
        ...

    def set(self, key: tuple[str, str, str, str, str, str], estimate: TravelEstimate) -> None:
        ...


class PostcodeGeocoder(Protocol):
    """Tenant-managed postcode/geocode lookup interface."""

    name: str

    def resolve(self, postcode: str) -> Location | None:
        ...


@dataclass(frozen=True)
class TravelLocationIssue:
    severity: str
    code: str
    message: str
    context: str


@dataclass(frozen=True)
class TravelConfig:
    travel_provider_order: tuple[str, ...] = ("cache", "postcode_distance", "fallback")
    enable_external_travel: bool = False
    ors_api_key_env: str = "ORS_API_KEY"
    osrm_base_url_env: str = "OSRM_BASE_URL"
    google_maps_api_key_env: str = "GOOGLE_MAPS_API_KEY"
    enable_google_routes: bool = False
    max_external_route_calls_per_run: int = 100
    max_matrix_elements_per_run: int = 625
    route_timeout_seconds: float = 5.0
    route_retries: int = 1
    fallback_on_provider_failure: bool = True
    time_bucket_minutes: int = 30


@dataclass
class TravelRunStats:
    estimates_from_cache: int = 0
    estimates_from_fallback: int = 0
    estimates_from_postcode_distance: int = 0
    estimates_from_ors: int = 0
    estimates_from_osrm: int = 0
    estimates_from_google: int = 0
    provider_failures: int = 0
    external_route_calls: int = 0
    matrix_elements: int = 0

    def record(self, estimate: TravelEstimate) -> None:
        if estimate.cached:
            self.estimates_from_cache += 1
        if estimate.provider == "fallback":
            self.estimates_from_fallback += 1
        elif estimate.provider == "postcode_distance":
            self.estimates_from_postcode_distance += 1
        elif estimate.provider == "openrouteservice":
            self.estimates_from_ors += 1
        elif estimate.provider == "osrm":
            self.estimates_from_osrm += 1
        elif estimate.provider == "google_routes":
            self.estimates_from_google += 1

    def to_dict(self) -> dict:
        return {
            "estimates_from_cache": self.estimates_from_cache,
            "estimates_from_fallback": self.estimates_from_fallback,
            "estimates_from_postcode_distance": self.estimates_from_postcode_distance,
            "estimates_from_ors": self.estimates_from_ors,
            "estimates_from_osrm": self.estimates_from_osrm,
            "estimates_from_google": self.estimates_from_google,
            "provider_failures": self.provider_failures,
            "external_route_calls": self.external_route_calls,
            "matrix_elements": self.matrix_elements,
        }


class InMemoryTravelEstimateCache:
    """Small deterministic cache for local planning and tests."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str, str, str, str], TravelEstimate] = {}

    def get(self, key: tuple[str, str, str, str, str, str]) -> TravelEstimate | None:
        return self._items.get(key)

    def set(self, key: tuple[str, str, str, str, str, str], estimate: TravelEstimate) -> None:
        self._items[key] = estimate

    def __len__(self) -> int:
        return len(self._items)


class SQLiteTravelEstimateCache:
    """Persistent local cache for development and production-like tests."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def get(self, key: tuple[str, str, str, str, str, str]) -> TravelEstimate | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT estimate_json
                FROM travel_estimate_cache
                WHERE tenant_id = ?
                  AND origin_key = ?
                  AND destination_key = ?
                  AND mode = ?
                  AND day_type = ?
                  AND departure_bucket = ?
                """,
                key,
            ).fetchone()
        if row is None:
            return None
        return estimate_from_dict(json.loads(row["estimate_json"]))

    def set(self, key: tuple[str, str, str, str, str, str], estimate: TravelEstimate) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO travel_estimate_cache (
                    tenant_id, origin_key, destination_key, mode, day_type, departure_bucket,
                    estimate_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, origin_key, destination_key, mode, day_type, departure_bucket)
                DO UPDATE SET
                    estimate_json = excluded.estimate_json,
                    updated_at = excluded.updated_at
                """,
                (*key, json.dumps(estimate_to_dict(estimate), sort_keys=True), datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS travel_estimate_cache (
                    tenant_id TEXT NOT NULL,
                    origin_key TEXT NOT NULL,
                    destination_key TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    day_type TEXT NOT NULL,
                    departure_bucket TEXT NOT NULL,
                    estimate_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, origin_key, destination_key, mode, day_type, departure_bucket)
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection


class StaticPostcodeGeocoder:
    """Reviewed local postcode lookup for tests and tenant-managed config."""

    name = "static_postcode_geocoder"

    def __init__(self, locations: Mapping[str, Location]) -> None:
        self.locations = {normalize_postcode(key): value for key, value in locations.items()}

    def resolve(self, postcode: str) -> Location | None:
        return self.locations.get(normalize_postcode(postcode))


class ModeSpecificTravelProvider:
    """Route estimates to separate providers for drive, transit, walk, cycle, or taxi."""

    name = "mode_specific"

    def __init__(self, providers: Mapping[TravelMode, TravelProvider]) -> None:
        self.providers = dict(providers)

    def estimate(
        self,
        *,
        origin: Location,
        destination: Location,
        mode: TravelMode,
        departure_time: datetime,
    ) -> TravelEstimate:
        provider = self.providers.get(mode)
        if provider is None:
            raise TravelProviderError(f"no travel provider configured for mode {mode.value}")
        return provider.estimate(
            origin=origin,
            destination=destination,
            mode=mode,
            departure_time=departure_time,
        )


class FallbackTravelProvider:
    """Final deterministic fallback with no network or API dependency."""

    name = "fallback"

    def estimate(
        self,
        *,
        origin: Location,
        destination: Location,
        mode: TravelMode,
        departure_time: datetime,
    ) -> TravelEstimate:
        return fallback_estimate(
            origin=origin,
            destination=destination,
            mode=mode,
            departure_time=departure_time,
            provider=self.name,
        )


class PostcodeDistanceProvider:
    """Approximate route estimates from coordinates or normalized UK postcode areas."""

    name = "postcode_distance"

    def estimate(
        self,
        *,
        origin: Location,
        destination: Location,
        mode: TravelMode,
        departure_time: datetime,
    ) -> TravelEstimate:
        distance_meters = straight_line_distance_meters(origin, destination)
        confidence = "medium" if distance_meters is not None else "low"
        warnings = ["approximate postcode-distance travel estimate; not live routing"]

        if distance_meters is None:
            distance_meters = postcode_area_distance_meters(origin, destination)
            warnings.append("postcode area distance used because exact coordinates are unavailable")

        duration = _duration_from_distance(distance_meters, mode, route_factor=1.35)
        return TravelEstimate(
            origin=origin,
            destination=destination,
            mode=mode,
            departure_time=departure_time,
            duration_minutes=round(duration, 1),
            distance_meters=round(distance_meters, 1),
            provider=self.name,
            confidence=confidence,
            warnings=tuple(warnings),
        )


class OpenRouteServiceProvider:
    """OpenRouteService routing provider, disabled unless configured with an API key."""

    name = "openrouteservice"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        config: TravelConfig | None = None,
        base_url: str = "https://api.openrouteservice.org",
        stats: TravelRunStats | None = None,
    ) -> None:
        self.config = config or TravelConfig()
        self.api_key = api_key if api_key is not None else os.environ.get(self.config.ors_api_key_env, "")
        self.base_url = base_url.rstrip("/")
        self.stats = stats

    def estimate(
        self,
        *,
        origin: Location,
        destination: Location,
        mode: TravelMode,
        departure_time: datetime,
    ) -> TravelEstimate:
        if not self.config.enable_external_travel or not self.api_key:
            raise TravelProviderError("OpenRouteService is not configured")
        if not _has_coordinates(origin) or not _has_coordinates(destination):
            raise TravelProviderError("OpenRouteService requires coordinates")
        profile = _ors_profile(mode)
        if profile is None:
            raise TravelProviderError(f"OpenRouteService does not support mode {mode.value}")
        payload = {
            "locations": [
                [origin.longitude, origin.latitude],
                [destination.longitude, destination.latitude],
            ],
            "metrics": ["duration", "distance"],
        }
        data = self._request_json(f"/v2/matrix/{profile}", payload)
        durations = data.get("durations") or []
        distances = data.get("distances") or []
        duration_seconds = durations[0][1] if durations and len(durations[0]) > 1 else None
        distance_meters = distances[0][1] if distances and len(distances[0]) > 1 else None
        if duration_seconds is None:
            raise TravelProviderError("OpenRouteService returned no duration")
        return TravelEstimate(
            origin=origin,
            destination=destination,
            mode=mode,
            departure_time=departure_time,
            duration_minutes=round(float(duration_seconds) / 60.0, 1),
            distance_meters=float(distance_meters) if distance_meters is not None else None,
            provider=self.name,
            confidence="high",
            warnings=(),
        )

    def matrix(
        self,
        *,
        origins: Iterable[Location],
        destinations: Iterable[Location],
        mode: TravelMode,
        departure_time: datetime,
    ) -> tuple[TravelEstimate, ...]:
        origin_list = tuple(origins)
        destination_list = tuple(destinations)
        element_count = len(origin_list) * len(destination_list)
        if element_count > self.config.max_matrix_elements_per_run:
            raise TravelProviderError("OpenRouteService matrix element limit exceeded")
        return tuple(
            self.estimate(origin=origin, destination=destination, mode=mode, departure_time=departure_time)
            for origin in origin_list
            for destination in destination_list
        )

    def _request_json(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        if self.stats:
            if self.stats.external_route_calls >= self.config.max_external_route_calls_per_run:
                raise TravelProviderError("external route call limit exceeded")
            self.stats.external_route_calls += 1
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        return _request_json_with_retries(request, config=self.config)


class OSRMProvider:
    """Self-hosted OSRM provider for driving estimates."""

    name = "osrm"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        config: TravelConfig | None = None,
        stats: TravelRunStats | None = None,
    ) -> None:
        self.config = config or TravelConfig()
        self.base_url = (base_url if base_url is not None else os.environ.get(self.config.osrm_base_url_env, "")).rstrip("/")
        self.stats = stats

    def estimate(
        self,
        *,
        origin: Location,
        destination: Location,
        mode: TravelMode,
        departure_time: datetime,
    ) -> TravelEstimate:
        if not self.config.enable_external_travel or not self.base_url:
            raise TravelProviderError("OSRM is not configured")
        if mode != TravelMode.DRIVE:
            raise TravelProviderError("OSRM provider only supports drive mode")
        if not _has_coordinates(origin) or not _has_coordinates(destination):
            raise TravelProviderError("OSRM requires coordinates")
        if self.stats:
            if self.stats.external_route_calls >= self.config.max_external_route_calls_per_run:
                raise TravelProviderError("external route call limit exceeded")
            self.stats.external_route_calls += 1
        coords = f"{origin.longitude},{origin.latitude};{destination.longitude},{destination.latitude}"
        query = urllib.parse.urlencode({"overview": "false", "alternatives": "false"})
        request = urllib.request.Request(f"{self.base_url}/route/v1/driving/{coords}?{query}")
        data = _request_json_with_retries(request, config=self.config)
        routes = data.get("routes") or []
        if not routes:
            raise TravelProviderError("OSRM returned no route")
        route = routes[0]
        return TravelEstimate(
            origin=origin,
            destination=destination,
            mode=mode,
            departure_time=departure_time,
            duration_minutes=round(float(route["duration"]) / 60.0, 1),
            distance_meters=float(route["distance"]),
            provider=self.name,
            confidence="high",
            warnings=(),
        )


class GoogleRoutesProvider:
    """Optional Google Routes provider stub with explicit opt-in and cost controls."""

    name = "google_routes"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        config: TravelConfig | None = None,
        stats: TravelRunStats | None = None,
    ) -> None:
        self.config = config or TravelConfig()
        self.api_key = api_key if api_key is not None else os.environ.get(self.config.google_maps_api_key_env, "")
        self.stats = stats

    def estimate(
        self,
        *,
        origin: Location,
        destination: Location,
        mode: TravelMode,
        departure_time: datetime,
    ) -> TravelEstimate:
        if not self.config.enable_external_travel or not self.config.enable_google_routes:
            raise TravelProviderError("Google Routes is disabled by configuration")
        if not self.api_key:
            raise TravelProviderError("Google Routes API key is not configured")
        raise TravelProviderError("Google Routes provider is a guarded later-stage integration")


class PublicTransportProvider:
    """Public-transport provider interface designed for future GTFS/TXC data."""

    name = "public_transport"

    def __init__(self, *, configured: bool = False) -> None:
        self.configured = configured

    def estimate(
        self,
        *,
        origin: Location,
        destination: Location,
        mode: TravelMode,
        departure_time: datetime,
    ) -> TravelEstimate:
        if mode not in {TravelMode.PUBLIC_TRANSPORT, TravelMode.BUS, TravelMode.TRAM, TravelMode.TRAIN}:
            raise TravelProviderError(f"public transport provider cannot estimate {mode.value}")
        if not self.configured:
            raise TravelProviderError("GTFS/public transport provider is not configured")
        raise TravelProviderError("GTFS/public transport routing is not implemented yet")


class FallbackTravelEstimator:
    """Deterministic postcode/geocode fallback used when no provider is available."""

    def __init__(self, *, tenant_id: str = "local", geocoder: PostcodeGeocoder | None = None) -> None:
        self.tenant_id = tenant_id
        self.geocoder = geocoder
        self.provider = FallbackTravelProvider()

    def estimate(self, *, worker: Worker, origin: Location, destination: Location, departure_time) -> TravelEstimate:
        mode = preferred_mode(worker)
        origin, destination, warnings = _resolved_route_locations(
            origin,
            destination,
            geocoder=self.geocoder,
        )
        estimate = self.provider.estimate(
            origin=origin,
            destination=destination,
            mode=mode,
            departure_time=departure_time,
        )
        return _with_warnings(estimate, warnings)


class CachedTravelEstimator:
    """Provider-backed travel estimator with mode-aware fallback estimates."""

    def __init__(
        self,
        *,
        tenant_id: str,
        provider: TravelProvider | None = None,
        providers: Mapping[str, TravelProvider] | None = None,
        cache: TravelEstimateCache | None = None,
        geocoder: PostcodeGeocoder | None = None,
        config: TravelConfig | None = None,
        stats: TravelRunStats | None = None,
        time_bucket_minutes: int = 30,
    ) -> None:
        self.tenant_id = tenant_id
        self.config = config or TravelConfig(time_bucket_minutes=time_bucket_minutes)
        self.stats = stats or TravelRunStats()
        self.providers = build_default_travel_providers(config=self.config, stats=self.stats)
        if providers:
            self.providers.update(providers)
        if provider is not None:
            self.providers[provider.name] = provider
            if provider.name not in self.config.travel_provider_order:
                ordered = ("cache", provider.name) + tuple(
                    item for item in self.config.travel_provider_order if item != "cache"
                )
                self.config = replace(self.config, travel_provider_order=ordered)
        self.cache = cache if cache is not None else InMemoryTravelEstimateCache()
        self.geocoder = geocoder
        self.time_bucket_minutes = max(1, self.config.time_bucket_minutes)

    def estimate(self, *, worker: Worker, origin: Location, destination: Location, departure_time) -> TravelEstimate:
        mode = preferred_mode(worker)
        origin, destination, warnings = _resolved_route_locations(
            origin,
            destination,
            geocoder=self.geocoder,
        )
        key = travel_cache_key(
            tenant_id=self.tenant_id,
            origin=origin,
            destination=destination,
            mode=mode,
            departure_time=departure_time,
            bucket_minutes=self.time_bucket_minutes,
        )
        cached = self.cache.get(key)
        if cached is not None:
            estimate = _with_warnings(_mark_cached(cached), (*warnings, "travel estimate served from cache"))
            self.stats.record(estimate)
            return estimate

        estimate = self._provider_estimate_chain(
            origin=origin,
            destination=destination,
            mode=mode,
            departure_time=departure_time,
        )
        estimate = _with_warnings(estimate, warnings)
        self.cache.set(key, estimate)
        self.stats.record(estimate)
        return estimate

    def _provider_estimate_chain(
        self,
        *,
        origin: Location,
        destination: Location,
        mode: TravelMode,
        departure_time,
    ) -> TravelEstimate:
        failures: list[str] = []
        for provider_name in self.config.travel_provider_order:
            if provider_name == "cache":
                continue
            provider = self.providers.get(provider_name)
            if provider is None:
                continue
            try:
                estimate = provider.estimate(
                    origin=origin,
                    destination=destination,
                    mode=mode,
                    departure_time=departure_time,
                )
            except Exception as exc:
                self.stats.provider_failures += 1
                failures.append(f"travel provider {provider_name} failed: {exc}")
                if not self.config.fallback_on_provider_failure:
                    raise
                continue
            if estimate.mode != mode:
                self.stats.provider_failures += 1
                failures.append(f"travel provider {provider_name} returned {estimate.mode.value} for requested {mode.value}")
                if not self.config.fallback_on_provider_failure:
                    raise TravelProviderError(f"travel provider {provider_name} returned wrong mode")
                continue
            return _with_warnings(estimate, tuple(failures))

        fallback = FallbackTravelProvider().estimate(
            origin=origin,
            destination=destination,
            mode=mode,
            departure_time=departure_time,
        )
        return _with_warnings(fallback, tuple(failures))


def build_default_travel_providers(
    *,
    config: TravelConfig | None = None,
    stats: TravelRunStats | None = None,
) -> dict[str, TravelProvider]:
    resolved = config or TravelConfig()
    providers: dict[str, TravelProvider] = {
        "postcode_distance": PostcodeDistanceProvider(),
        "fallback": FallbackTravelProvider(),
        "public_transport": PublicTransportProvider(configured=False),
    }
    providers["openrouteservice"] = OpenRouteServiceProvider(config=resolved, stats=stats)
    providers["osrm"] = OSRMProvider(config=resolved, stats=stats)
    providers["google_routes"] = GoogleRoutesProvider(config=resolved, stats=stats)
    return providers


def fallback_estimate(
    *,
    origin: Location,
    destination: Location,
    mode: TravelMode,
    departure_time: datetime,
    provider: str,
    warnings: tuple[str, ...] = (),
) -> TravelEstimate:
    minutes = 20.0
    estimate_warnings = list(warnings)
    distance_meters = straight_line_distance_meters(origin, destination)
    confidence = "medium" if distance_meters is not None or (origin.area and destination.area) else "low"
    estimate_warnings.append("fallback travel estimate used; not live routing")

    if origin.area and destination.area:
        minutes = 12.0 if normalize_area(origin.area) == normalize_area(destination.area) else 35.0
    elif _has_coordinates(origin) and _has_coordinates(destination):
        distance_meters = distance_meters if distance_meters is not None else 5000.0
        minutes = _duration_from_distance(distance_meters, mode, route_factor=1.5)
    else:
        minutes = 30.0
        estimate_warnings.append("fallback travel estimate used because location data is incomplete")

    if mode == TravelMode.WALK:
        minutes *= 1.5
    elif mode in {TravelMode.BUS, TravelMode.PUBLIC_TRANSPORT, TravelMode.TRAM, TravelMode.TRAIN}:
        minutes *= 1.25
    elif mode == TravelMode.CYCLE:
        minutes *= 0.85
    elif mode == TravelMode.TAXI:
        minutes *= 1.05

    return TravelEstimate(
        origin=origin,
        destination=destination,
        mode=mode,
        departure_time=departure_time,
        duration_minutes=round(minutes, 1),
        distance_meters=round(distance_meters, 1) if distance_meters is not None else None,
        confidence=confidence,
        provider=provider,
        warnings=tuple(estimate_warnings),
    )


def resolve_location_for_travel(
    location: Location,
    *,
    geocoder: PostcodeGeocoder | None = None,
    context: str = "location",
) -> tuple[Location, tuple[TravelLocationIssue, ...]]:
    """Normalize/geocode a location without mutating source records."""

    issues = list(travel_location_issues(location, context=context))
    normalized_postcode = normalize_postcode(location.postcode)
    if not normalized_postcode or _has_coordinates(location):
        return with_postcode_area(location), tuple(issues)

    if geocoder is None:
        issues.append(
            TravelLocationIssue(
                severity="warning",
                code="postcode_not_geocoded",
                message=f"{context} has a postcode but no geocoder was configured",
                context=context,
            )
        )
        return with_postcode_area(location), tuple(issues)

    resolved = geocoder.resolve(normalized_postcode)
    if resolved is None:
        issues.append(
            TravelLocationIssue(
                severity="warning",
                code="postcode_not_found",
                message=f"{context} postcode could not be resolved by {geocoder.name}",
                context=context,
            )
        )
        return with_postcode_area(location), tuple(issues)

    return (
        Location(
            label=location.label or resolved.label,
            postcode=normalized_postcode,
            latitude=location.latitude if location.latitude is not None else resolved.latitude,
            longitude=location.longitude if location.longitude is not None else resolved.longitude,
            area=location.area or resolved.area or postcode_area(normalized_postcode),
        ),
        tuple(issues),
    )


def travel_location_issues(location: Location, *, context: str = "location") -> tuple[TravelLocationIssue, ...]:
    issues: list[TravelLocationIssue] = []
    if not any((location.postcode, _has_coordinates(location), location.area)):
        issues.append(
            TravelLocationIssue(
                severity="warning",
                code="missing_travel_location",
                message=f"{context} has no postcode, coordinates, or area for travel estimation",
                context=context,
            )
        )
    if (location.latitude is None) != (location.longitude is None):
        issues.append(
            TravelLocationIssue(
                severity="warning",
                code="partial_coordinates",
                message=f"{context} has only one coordinate value",
                context=context,
            )
        )
    return tuple(issues)


def with_postcode_area(location: Location) -> Location:
    if location.area or not location.postcode:
        return location
    return replace(location, postcode=normalize_postcode(location.postcode), area=postcode_area(location.postcode))


def travel_cache_key(
    *,
    tenant_id: str,
    origin: Location,
    destination: Location,
    mode: TravelMode,
    departure_time: datetime,
    bucket_minutes: int = 30,
) -> tuple[str, str, str, str, str, str]:
    return (
        tenant_id,
        location_cache_key(origin),
        location_cache_key(destination),
        mode.value,
        day_type(departure_time),
        time_bucket(departure_time, bucket_minutes=bucket_minutes),
    )


def location_cache_key(location: Location) -> str:
    postcode = normalize_postcode(location.postcode)
    if postcode:
        return f"postcode:{postcode}"
    if _has_coordinates(location):
        return f"geo:{round(location.latitude or 0.0, 4):.4f},{round(location.longitude or 0.0, 4):.4f}"
    area = normalize_area(location.area)
    if area:
        return f"area:{area}"
    return "label:" + " ".join(location.label.strip().lower().split())


def normalize_postcode(value: str | None) -> str:
    if not value:
        return ""
    return "".join(value.upper().split())


def postcode_area(value: str | None) -> str:
    normalized = normalize_postcode(value)
    if not normalized:
        return ""
    # UK outward codes are the non-space prefix before the final three characters.
    return normalized[:-3] if len(normalized) > 3 else normalized


def normalize_area(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.upper().split())


def time_bucket(value: datetime, *, bucket_minutes: int = 30) -> str:
    bucket = max(1, bucket_minutes)
    minute = (value.minute // bucket) * bucket
    bucketed = value.replace(minute=minute, second=0, microsecond=0)
    return bucketed.isoformat()


def day_type(value: datetime) -> str:
    return "weekend" if value.weekday() >= 5 else "weekday"


def preferred_mode(worker: Worker) -> TravelMode:
    if worker.has_car and TravelMode.DRIVE in worker.travel_modes:
        return TravelMode.DRIVE
    for mode in (
        TravelMode.PUBLIC_TRANSPORT,
        TravelMode.BUS,
        TravelMode.TRAM,
        TravelMode.TRAIN,
        TravelMode.CYCLE,
        TravelMode.WALK,
        TravelMode.TAXI,
        TravelMode.DRIVE,
    ):
        if mode in worker.travel_modes:
            return mode
    return TravelMode.WALK


def straight_line_distance_meters(origin: Location, destination: Location) -> float | None:
    if not _has_coordinates(origin) or not _has_coordinates(destination):
        return None
    radius_meters = 6_371_000.0
    lat1 = math.radians(origin.latitude or 0.0)
    lat2 = math.radians(destination.latitude or 0.0)
    delta_lat = math.radians((destination.latitude or 0.0) - (origin.latitude or 0.0))
    delta_lon = math.radians((destination.longitude or 0.0) - (origin.longitude or 0.0))
    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return radius_meters * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def postcode_area_distance_meters(origin: Location, destination: Location) -> float:
    left = normalize_area(origin.area) or postcode_area(origin.postcode)
    right = normalize_area(destination.area) or postcode_area(destination.postcode)
    if left and right and left == right:
        return 3_000.0
    if left and right and left[:1] == right[:1]:
        return 12_000.0
    return 25_000.0


def _duration_from_distance(distance_meters: float, mode: TravelMode, *, route_factor: float) -> float:
    speed_kph = {
        TravelMode.DRIVE: 32.0,
        TravelMode.TAXI: 30.0,
        TravelMode.CYCLE: 14.0,
        TravelMode.WALK: 4.8,
        TravelMode.PUBLIC_TRANSPORT: 18.0,
        TravelMode.BUS: 16.0,
        TravelMode.TRAM: 20.0,
        TravelMode.TRAIN: 30.0,
    }.get(mode, 14.0)
    route_km = (distance_meters / 1000.0) * route_factor
    return max(5.0, (route_km / speed_kph) * 60.0)


def estimate_to_dict(estimate: TravelEstimate) -> dict:
    return {
        "origin": location_to_dict(estimate.origin) if estimate.origin else None,
        "destination": location_to_dict(estimate.destination) if estimate.destination else None,
        "mode": estimate.mode.value,
        "departure_time": estimate.departure_time.isoformat() if estimate.departure_time else None,
        "duration_minutes": estimate.duration_minutes,
        "minutes": estimate.minutes,
        "distance_meters": estimate.distance_meters,
        "distance_km": estimate.distance_km,
        "cost": estimate.cost,
        "confidence": estimate.confidence,
        "provider": estimate.provider,
        "warnings": list(estimate.warnings),
        "cached": estimate.cached,
    }


def estimate_from_dict(data: Mapping[str, object]) -> TravelEstimate:
    departure_time = data.get("departure_time")
    return TravelEstimate(
        origin=location_from_dict(data.get("origin")) if data.get("origin") else None,
        destination=location_from_dict(data.get("destination")) if data.get("destination") else None,
        mode=TravelMode(str(data["mode"])),
        departure_time=datetime.fromisoformat(str(departure_time)) if departure_time else None,
        duration_minutes=float(data.get("duration_minutes", data.get("minutes"))),
        distance_meters=float(data["distance_meters"]) if data.get("distance_meters") is not None else None,
        distance_km=float(data["distance_km"]) if data.get("distance_km") is not None and data.get("distance_meters") is None else None,
        cost=float(data["cost"]) if data.get("cost") is not None else None,
        confidence=str(data.get("confidence", "low")),
        provider=str(data.get("provider", "unknown")),
        warnings=tuple(str(item) for item in data.get("warnings", ())),
        cached=bool(data.get("cached", False)),
    )


def location_to_dict(location: Location) -> dict:
    return {
        "label": location.label,
        "postcode": location.postcode,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "area": location.area,
    }


def location_from_dict(data) -> Location:
    return Location(
        label=str(data.get("label") or ""),
        postcode=data.get("postcode"),
        latitude=float(data["latitude"]) if data.get("latitude") is not None else None,
        longitude=float(data["longitude"]) if data.get("longitude") is not None else None,
        area=data.get("area"),
    )


def _has_coordinates(location: Location) -> bool:
    return location.latitude is not None and location.longitude is not None


def _ors_profile(mode: TravelMode) -> str | None:
    return {
        TravelMode.DRIVE: "driving-car",
        TravelMode.WALK: "foot-walking",
        TravelMode.CYCLE: "cycling-regular",
    }.get(mode)


def _request_json_with_retries(request: urllib.request.Request, *, config: TravelConfig) -> Mapping[str, object]:
    last_error: Exception | None = None
    for attempt in range(config.route_retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=config.route_timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < config.route_retries:
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.0
                time.sleep(min(delay, 2.0))
                last_error = exc
                continue
            raise TravelProviderError(f"routing provider HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < config.route_retries:
                time.sleep(0.2 * (attempt + 1))
                continue
            break
    raise TravelProviderError(str(last_error) if last_error else "routing provider request failed")


def _resolved_route_locations(
    origin: Location,
    destination: Location,
    *,
    geocoder: PostcodeGeocoder | None,
) -> tuple[Location, Location, tuple[str, ...]]:
    resolved_origin, origin_issues = resolve_location_for_travel(origin, geocoder=geocoder, context="travel origin")
    resolved_destination, destination_issues = resolve_location_for_travel(
        destination,
        geocoder=geocoder,
        context="travel destination",
    )
    warnings = tuple(issue.message for issue in (*origin_issues, *destination_issues))
    return resolved_origin, resolved_destination, warnings


def _with_warning(estimate: TravelEstimate, warning: str) -> TravelEstimate:
    if warning in estimate.warnings:
        return estimate
    return replace(estimate, warnings=estimate.warnings + (warning,))


def _with_warnings(estimate: TravelEstimate, warnings: tuple[str, ...]) -> TravelEstimate:
    updated = estimate
    for warning in warnings:
        updated = _with_warning(updated, warning)
    return updated


def _mark_cached(estimate: TravelEstimate) -> TravelEstimate:
    return replace(estimate, cached=True)
