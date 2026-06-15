"""Travel estimation interfaces and implementations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from typing import Protocol

from .models import Location, TravelEstimate, TravelMode, Worker


class TravelEstimator(Protocol):
    def estimate(
        self,
        worker: Worker,
        origin: Location,
        destination: Location,
        departure_time: datetime,
    ) -> TravelEstimate:
        """Return the best usable estimate for the worker."""


class Geocoder(Protocol):
    def geocode(self, location: Location) -> Location:
        """Return a location with latitude/longitude when possible."""


@dataclass(frozen=True)
class HeuristicTravelEstimator:
    """Deterministic fallback estimator.

    This is not a substitute for a maps provider. It exists so scoring, tests,
    and imports can run before provider credentials are configured.
    """

    default_same_area_minutes: float = 25.0
    default_different_area_minutes: float = 55.0

    def estimate(
        self,
        worker: Worker,
        origin: Location,
        destination: Location,
        departure_time: datetime,
    ) -> TravelEstimate:
        mode = self._select_mode(worker)
        distance_km = self._distance_km(origin, destination)

        if distance_km is not None:
            minutes = self._minutes_from_distance(distance_km, mode)
            confidence = 0.55
            warnings = ("postcode/geocode fallback estimate",)
        elif origin.area and destination.area and origin.area == destination.area:
            minutes = self.default_same_area_minutes
            confidence = 0.35
            warnings = ("area-only fallback estimate",)
        else:
            minutes = self.default_different_area_minutes
            confidence = 0.25
            warnings = ("low-confidence fallback estimate",)

        return TravelEstimate(
            mode=mode,
            minutes=minutes,
            distance_km=distance_km,
            confidence=confidence,
            provider="heuristic",
            warnings=warnings,
        )

    def _select_mode(self, worker: Worker) -> TravelMode:
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
        ):
            if mode in worker.travel_modes:
                return mode
        return TravelMode.PUBLIC_TRANSPORT

    def _minutes_from_distance(self, distance_km: float, mode: TravelMode) -> float:
        speed_by_mode = {
            TravelMode.DRIVE: 28.0,
            TravelMode.TAXI: 28.0,
            TravelMode.PUBLIC_TRANSPORT: 16.0,
            TravelMode.BUS: 14.0,
            TravelMode.TRAM: 18.0,
            TravelMode.TRAIN: 22.0,
            TravelMode.CYCLE: 14.0,
            TravelMode.WALK: 4.5,
        }
        speed = speed_by_mode.get(mode, 14.0)
        fixed_minutes = 8.0 if mode in {TravelMode.DRIVE, TravelMode.TAXI} else 15.0
        return fixed_minutes + (distance_km / speed * 60.0)

    def _distance_km(self, origin: Location, destination: Location) -> float | None:
        if (
            origin.latitude is None
            or origin.longitude is None
            or destination.latitude is None
            or destination.longitude is None
        ):
            return None

        lat1 = radians(origin.latitude)
        lon1 = radians(origin.longitude)
        lat2 = radians(destination.latitude)
        lon2 = radians(destination.longitude)
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        return 6371.0 * 2 * asin(sqrt(a))


@dataclass(frozen=True)
class PostcodesIoGeocoder:
    """Free UK postcode geocoder backed by postcodes.io.

    This is appropriate for UK postcode-level prototyping. It is not rooftop
    precision and does not replace a commercial address database when a tenant
    needs property-level accuracy.
    """

    base_url: str = "https://api.postcodes.io/postcodes"
    timeout_seconds: float = 8.0
    user_agent: str = "staff-session-recommender/0.1"

    def geocode(self, location: Location) -> Location:
        if location.latitude is not None and location.longitude is not None:
            return location
        if not location.postcode:
            return location

        postcode = quote(location.postcode.strip())
        url = f"{self.base_url}/{postcode}"
        data = _get_json(url, self.timeout_seconds, self.user_agent)
        result = data.get("result") or {}
        latitude = result.get("latitude")
        longitude = result.get("longitude")
        if latitude is None or longitude is None:
            return location

        area = location.area or _postcode_area(location.postcode)
        return Location(
            label=location.label,
            postcode=location.postcode,
            latitude=float(latitude),
            longitude=float(longitude),
            area=area,
        )


@dataclass(frozen=True)
class CachedGeocoder:
    """Geocoder wrapper with a small JSON cache.

    The cache keeps prototype runs from repeatedly calling a postcode provider
    for the same staff and service-user postcodes.
    """

    provider: Geocoder = PostcodesIoGeocoder()
    cache_path: Path | None = None

    def geocode(self, location: Location) -> Location:
        if location.latitude is not None and location.longitude is not None:
            return location
        if not location.postcode:
            return location

        key = _normalise_postcode(location.postcode)
        cached = self._read_cache(key)
        if cached:
            return Location(
                label=location.label,
                postcode=location.postcode,
                latitude=cached.latitude,
                longitude=cached.longitude,
                area=location.area or cached.area,
            )

        geocoded = self.provider.geocode(location)
        if geocoded.latitude is not None and geocoded.longitude is not None:
            self._write_cache(key, geocoded)
        return geocoded

    def _read_cache(self, key: str) -> Location | None:
        if not self.cache_path or not self.cache_path.exists():
            return None
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            row = data.get(key)
            if not row:
                return None
            return Location(
                label="cached postcode",
                postcode=key,
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                area=row.get("area") or _postcode_area(key),
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _write_cache(self, key: str, location: Location) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        data[key] = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "area": location.area or _postcode_area(key),
        }
        self.cache_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


@dataclass(frozen=True)
class GeocodedHeuristicTravelEstimator:
    """Postcode-geocoded estimator for all modes.

    This is a practical middle step between the flat area-only heuristic and a
    paid routing provider. It geocodes UK postcodes, then uses mode-specific
    speeds over straight-line distance with confidence warnings.
    """

    geocoder: Geocoder = CachedGeocoder()
    fallback: HeuristicTravelEstimator = HeuristicTravelEstimator()

    def estimate(
        self,
        worker: Worker,
        origin: Location,
        destination: Location,
        departure_time: datetime,
    ) -> TravelEstimate:
        try:
            geocoded_origin = self.geocoder.geocode(origin)
            geocoded_destination = self.geocoder.geocode(destination)
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
            fallback = self.fallback.estimate(worker, origin, destination, departure_time)
            return _with_warning(fallback, f"postcode geocoding failed; used heuristic fallback: {exc}")

        estimate = self.fallback.estimate(worker, geocoded_origin, geocoded_destination, departure_time)
        if estimate.distance_km is None:
            return _with_warning(estimate, "postcode geocoding incomplete; used area fallback")

        return TravelEstimate(
            mode=estimate.mode,
            minutes=estimate.minutes,
            distance_km=estimate.distance_km,
            cost=estimate.cost,
            confidence=0.6,
            provider="geocoded_heuristic",
            warnings=("postcode-centroid distance estimate; verify for real allocation decisions",),
        )


@dataclass(frozen=True)
class OSRMTravelEstimator:
    """OSRM-backed route estimator with heuristic fallback.

    The default endpoint uses the public OSRM demo server. That is useful for
    development, but production SaaS should use a paid provider or self-hosted
    OSRM/Valhalla/GraphHopper with rate limits, caching, and attribution.
    """

    geocoder: Geocoder = PostcodesIoGeocoder()
    fallback: HeuristicTravelEstimator = HeuristicTravelEstimator()
    base_url: str = "https://router.project-osrm.org/route/v1"
    timeout_seconds: float = 10.0
    user_agent: str = "staff-session-recommender/0.1"
    cache_path: Path | None = None

    def estimate(
        self,
        worker: Worker,
        origin: Location,
        destination: Location,
        departure_time: datetime,
    ) -> TravelEstimate:
        mode = self._select_mode(worker)
        if mode not in {TravelMode.DRIVE, TravelMode.TAXI, TravelMode.CYCLE, TravelMode.WALK}:
            fallback = self.fallback.estimate(worker, origin, destination, departure_time)
            return TravelEstimate(
                mode=fallback.mode,
                minutes=fallback.minutes,
                distance_km=fallback.distance_km,
                cost=fallback.cost,
                confidence=min(fallback.confidence, 0.35),
                provider=fallback.provider,
                warnings=fallback.warnings
                + ("public-transport routing provider not configured; used heuristic fallback",),
            )

        origin = self.geocoder.geocode(origin)
        destination = self.geocoder.geocode(destination)
        if (
            origin.latitude is None
            or origin.longitude is None
            or destination.latitude is None
            or destination.longitude is None
        ):
            fallback = self.fallback.estimate(worker, origin, destination, departure_time)
            return _with_warning(fallback, "geocoding incomplete; used heuristic fallback")

        cache_key = self._cache_key(mode, origin, destination)
        cached = self._read_cache(cache_key)
        if cached:
            return cached

        profile = self._osrm_profile(mode)
        coords = f"{origin.longitude},{origin.latitude};{destination.longitude},{destination.latitude}"
        query = urlencode({"overview": "false", "alternatives": "false", "steps": "false"})
        url = f"{self.base_url}/{profile}/{coords}?{query}"

        try:
            data = _get_json(url, self.timeout_seconds, self.user_agent)
            route = (data.get("routes") or [None])[0]
            if not route:
                raise ValueError("OSRM returned no routes")
            estimate = TravelEstimate(
                mode=mode,
                minutes=float(route["duration"]) / 60.0,
                distance_km=float(route["distance"]) / 1000.0,
                confidence=0.75,
                provider="osrm",
                warnings=(
                    "uses public OSRM/demo-style routing; not suitable as a paid SaaS dependency",
                ),
            )
            self._write_cache(cache_key, estimate)
            return estimate
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
            fallback = self.fallback.estimate(worker, origin, destination, departure_time)
            return _with_warning(fallback, f"OSRM lookup failed; used heuristic fallback: {exc}")

    def _select_mode(self, worker: Worker) -> TravelMode:
        if worker.has_car and TravelMode.DRIVE in worker.travel_modes:
            return TravelMode.DRIVE
        for mode in (
            TravelMode.CYCLE,
            TravelMode.WALK,
            TravelMode.TAXI,
            TravelMode.PUBLIC_TRANSPORT,
            TravelMode.BUS,
            TravelMode.TRAM,
            TravelMode.TRAIN,
        ):
            if mode in worker.travel_modes:
                return mode
        return TravelMode.PUBLIC_TRANSPORT

    def _osrm_profile(self, mode: TravelMode) -> str:
        if mode == TravelMode.WALK:
            return "foot"
        if mode == TravelMode.CYCLE:
            return "bike"
        return "driving"

    def _cache_key(self, mode: TravelMode, origin: Location, destination: Location) -> str:
        return "|".join(
            [
                mode.value,
                f"{origin.latitude:.6f}",
                f"{origin.longitude:.6f}",
                f"{destination.latitude:.6f}",
                f"{destination.longitude:.6f}",
            ]
        )

    def _read_cache(self, key: str) -> TravelEstimate | None:
        if not self.cache_path or not self.cache_path.exists():
            return None
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            row = data.get(key)
            if not row:
                return None
            return TravelEstimate(
                mode=TravelMode(row["mode"]),
                minutes=float(row["minutes"]),
                distance_km=float(row["distance_km"]) if row.get("distance_km") is not None else None,
                cost=float(row["cost"]) if row.get("cost") is not None else None,
                confidence=float(row["confidence"]),
                provider=row["provider"],
                warnings=tuple(row.get("warnings") or ()),
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _write_cache(self, key: str, estimate: TravelEstimate) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        data[key] = {
            "mode": estimate.mode.value,
            "minutes": estimate.minutes,
            "distance_km": estimate.distance_km,
            "cost": estimate.cost,
            "confidence": estimate.confidence,
            "provider": estimate.provider,
            "warnings": list(estimate.warnings),
        }
        self.cache_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _get_json(url: str, timeout_seconds: float, user_agent: str) -> dict:
    request = Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    with urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _with_warning(estimate: TravelEstimate, warning: str) -> TravelEstimate:
    return TravelEstimate(
        mode=estimate.mode,
        minutes=estimate.minutes,
        distance_km=estimate.distance_km,
        cost=estimate.cost,
        confidence=min(estimate.confidence, 0.35),
        provider=estimate.provider,
        warnings=estimate.warnings + (warning,),
    )


def _postcode_area(postcode: str) -> str:
    outward = postcode.strip().upper().split()[0]
    letters = []
    for char in outward:
        if char.isalpha():
            letters.append(char)
        else:
            break
    return "".join(letters) or outward


def _normalise_postcode(postcode: str) -> str:
    return "".join(postcode.upper().split())
