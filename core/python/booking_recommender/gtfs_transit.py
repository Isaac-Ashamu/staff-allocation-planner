"""Scheduled public-transport (bus/tram/train) estimates from a static GTFS feed.

Ported and adapted from an earlier, unmerged prototype (branch
``recommender-worker-id-sync``, June 2026) into the current provider
architecture. The original handled its own geocoding and its own fallback
chaining; here that is unnecessary because ``CachedTravelEstimator`` already
geocodes before calling any provider, and ``_provider_estimate_chain`` already
falls through to the next provider on a ``TravelProviderError`` -- so this
provider only needs to answer or refuse.

Coverage is deliberately narrow: direct scheduled legs, and journeys with at
most one transfer, using walking access and egress. There is no live
disruption data and no multi-transfer search. Good enough to tell "no bus/tram
gets you there in a sane time" from "here is roughly how long it takes";
not a passenger-facing journey planner.

Feed source: TfGM's published GTFS static feed (https://tfgm.com, open data).
Accepts either an unzipped directory of the standard GTFS text files (stops.txt,
trips.txt, calendar.txt, calendar_dates.txt, stop_times.txt) or the original
.zip, since feeds are sometimes distributed one way and unpacked the other.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from functools import cached_property, lru_cache
from pathlib import Path
from zipfile import ZipFile

from .models import Location, TravelMode
from .travel import TravelConfig, TravelEstimate, TravelProviderError, TravelRunStats, straight_line_distance_meters

TRANSIT_MODES = frozenset({TravelMode.PUBLIC_TRANSPORT, TravelMode.BUS, TravelMode.TRAM, TravelMode.TRAIN})


@dataclass(frozen=True)
class GtfsStop:
    id: str
    name: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class GtfsStopTime:
    stop_id: str
    arrival_seconds: int
    departure_seconds: int
    sequence: int


@dataclass(frozen=True)
class GtfsTransitLeg:
    origin_stop_id: str
    origin_stop_name: str
    destination_stop_id: str
    destination_stop_name: str
    trip_id: str
    route_id: str
    departure_seconds: int
    arrival_seconds: int
    walk_to_stop_minutes: float
    walk_from_stop_minutes: float
    wait_minutes: float
    transfer_stop_id: str | None = None
    transfer_stop_name: str | None = None
    second_trip_id: str | None = None
    second_route_id: str | None = None
    transfer_wait_minutes: float = 0.0


def _read_gtfs_rows(path: Path, name: str):
    """Yield dict rows from ``name`` inside a GTFS feed at ``path`` (dir or .zip)."""
    if path.is_dir():
        file_path = path / name
        if not file_path.exists():
            raise KeyError(name)
        with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
            yield from csv.DictReader(handle)
        return
    with ZipFile(path) as archive:
        with archive.open(name) as raw:
            text = raw.read().decode("utf-8-sig")
    yield from csv.DictReader(text.splitlines())


def _parse_gtfs_date(value: str) -> date | None:
    value = value.strip()
    if len(value) != 8 or not value.isdigit():
        return None
    return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))


def _parse_gtfs_time(value: str) -> int | None:
    """GTFS times can exceed 24:00:00 for trips past midnight; kept as raw seconds."""
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(p) for p in parts)
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


@dataclass(frozen=True)
class GtfsFeed:
    """Minimal GTFS static feed index for scheduled direct/one-transfer transit legs."""

    path: Path

    @cached_property
    def stops(self) -> dict[str, GtfsStop]:
        stops: dict[str, GtfsStop] = {}
        for row in _read_gtfs_rows(self.path, "stops.txt"):
            stop_id = (row.get("stop_id") or "").strip()
            lat = (row.get("stop_lat") or "").strip()
            lon = (row.get("stop_lon") or "").strip()
            if not stop_id or not lat or not lon:
                continue
            try:
                stops[stop_id] = GtfsStop(
                    id=stop_id, name=(row.get("stop_name") or "").strip() or stop_id,
                    latitude=float(lat), longitude=float(lon),
                )
            except ValueError:
                continue
        return stops

    @cached_property
    def trips(self) -> dict[str, tuple[str, str]]:
        return {
            row["trip_id"].strip(): ((row.get("route_id") or "").strip(), (row.get("service_id") or "").strip())
            for row in _read_gtfs_rows(self.path, "trips.txt")
            if row.get("trip_id")
        }

    @cached_property
    def service_calendar(self) -> dict[str, dict[str, str]]:
        try:
            rows = _read_gtfs_rows(self.path, "calendar.txt")
        except KeyError:
            return {}
        return {row["service_id"].strip(): row for row in rows if row.get("service_id")}

    @cached_property
    def calendar_dates(self) -> dict[date, dict[str, int]]:
        try:
            rows = _read_gtfs_rows(self.path, "calendar_dates.txt")
        except KeyError:
            return {}
        exceptions: dict[date, dict[str, int]] = {}
        for row in rows:
            service_id = (row.get("service_id") or "").strip()
            day = _parse_gtfs_date(row.get("date") or "")
            exception_type = (row.get("exception_type") or "").strip()
            if not service_id or day is None or exception_type not in {"1", "2"}:
                continue
            exceptions.setdefault(day, {})[service_id] = int(exception_type)
        return exceptions

    @cached_property
    def stop_times_by_trip(self) -> dict[str, tuple[GtfsStopTime, ...]]:
        stop_times: dict[str, list[GtfsStopTime]] = {}
        for row in _read_gtfs_rows(self.path, "stop_times.txt"):
            trip_id = (row.get("trip_id") or "").strip()
            stop_id = (row.get("stop_id") or "").strip()
            arrival = _parse_gtfs_time(row.get("arrival_time") or "")
            departure = _parse_gtfs_time(row.get("departure_time") or "")
            sequence = (row.get("stop_sequence") or "").strip()
            if not trip_id or not stop_id or arrival is None or departure is None or not sequence:
                continue
            try:
                stop_times.setdefault(trip_id, []).append(
                    GtfsStopTime(stop_id=stop_id, arrival_seconds=arrival, departure_seconds=departure, sequence=int(sequence))
                )
            except ValueError:
                continue
        return {trip_id: tuple(sorted(times, key=lambda t: t.sequence)) for trip_id, times in stop_times.items()}

    @cached_property
    def stop_visits_by_stop(self) -> dict[str, tuple[tuple[str, int, GtfsStopTime], ...]]:
        visits: dict[str, list[tuple[str, int, GtfsStopTime]]] = {}
        for trip_id, stop_times in self.stop_times_by_trip.items():
            for index, stop_time in enumerate(stop_times):
                visits.setdefault(stop_time.stop_id, []).append((trip_id, index, stop_time))
        return {
            stop_id: tuple(sorted(items, key=lambda item: item[2].departure_seconds))
            for stop_id, items in visits.items()
        }

    def active_service_ids(self, day: date) -> set[str]:
        weekday = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")[day.weekday()]
        active: set[str] = set()
        day_token = day.strftime("%Y%m%d")
        for service_id, row in self.service_calendar.items():
            if row.get(weekday) != "1":
                continue
            if row.get("start_date", "") <= day_token <= row.get("end_date", ""):
                active.add(service_id)
        for service_id, exception_type in self.calendar_dates.get(day, {}).items():
            if exception_type == 1:
                active.add(service_id)
            elif exception_type == 2:
                active.discard(service_id)
        return active

    @lru_cache(maxsize=20000)  # noqa: B019 -- feed is process-lifetime, not per-request
    def nearest_stops(self, latitude: float, longitude: float, max_distance_km: float, limit: int) -> tuple[tuple[GtfsStop, float], ...]:
        here = Location(label="lookup", latitude=latitude, longitude=longitude)
        candidates = []
        for stop in self.stops.values():
            meters = straight_line_distance_meters(here, Location(label=stop.name, latitude=stop.latitude, longitude=stop.longitude))
            if meters is None:
                continue
            km = meters / 1000.0
            if km <= max_distance_km:
                candidates.append((stop, km))
        return tuple(sorted(candidates, key=lambda item: item[1])[:limit])


class GtfsTransitProvider:
    """Scheduled bus/tram/train estimates for PUBLIC_TRANSPORT/BUS/TRAM/TRAIN modes.

    Refuses (raises ``TravelProviderError``) for any other mode, when no feed
    is configured, when either end lacks coordinates, or when no direct or
    one-transfer journey is found within the wait/transfer budgets -- the
    provider chain then falls through to postcode-distance or fallback.
    """

    name = "gtfs_transit"

    def __init__(
        self,
        *,
        gtfs_path: str | Path | None = None,
        config: TravelConfig | None = None,
        stats: TravelRunStats | None = None,
        max_walk_to_stop_km: float = 1.2,
        max_walk_from_stop_km: float = 1.2,
        max_candidate_stops: int = 8,
        walk_speed_kmh: float = 4.5,
        max_wait_minutes: float = 45.0,
        max_transfers: int = 1,
        min_transfer_minutes: float = 3.0,
        max_transfer_wait_minutes: float = 25.0,
    ) -> None:
        self.gtfs_path = Path(gtfs_path) if gtfs_path else None
        self.config = config or TravelConfig()
        self.stats = stats
        self.max_walk_to_stop_km = max_walk_to_stop_km
        self.max_walk_from_stop_km = max_walk_from_stop_km
        self.max_candidate_stops = max_candidate_stops
        self.walk_speed_kmh = walk_speed_kmh
        self.max_wait_minutes = max_wait_minutes
        self.max_transfers = max_transfers
        self.min_transfer_minutes = min_transfer_minutes
        self.max_transfer_wait_minutes = max_transfer_wait_minutes

    @cached_property
    def feed(self) -> GtfsFeed | None:
        if not self.gtfs_path or not self.gtfs_path.exists():
            return None
        return GtfsFeed(self.gtfs_path)

    def estimate(self, *, origin: Location, destination: Location, mode: TravelMode, departure_time: datetime) -> TravelEstimate:
        if mode not in TRANSIT_MODES:
            raise TravelProviderError(f"GTFS transit does not serve mode {mode.value}")
        feed = self.feed
        if feed is None:
            raise TravelProviderError("GTFS transit feed is not configured")
        if origin.latitude is None or origin.longitude is None or destination.latitude is None or destination.longitude is None:
            raise TravelProviderError("GTFS transit requires coordinates")

        leg = self._best_leg(feed, origin, destination, departure_time)
        if leg is None:
            raise TravelProviderError("no direct or one-transfer scheduled GTFS journey found")

        in_vehicle_minutes = (leg.arrival_seconds - leg.departure_seconds) / 60.0
        total_minutes = leg.walk_to_stop_minutes + leg.wait_minutes + in_vehicle_minutes + leg.walk_from_stop_minutes
        if leg.second_trip_id:
            warnings = (
                "scheduled GTFS journey with one transfer; no live disruption data",
                f"via {leg.origin_stop_name}, transfer at {leg.transfer_stop_name}, then {leg.destination_stop_name}",
            )
            confidence = "medium"
        else:
            warnings = (
                "scheduled GTFS direct journey; no live disruption data",
                f"via {leg.origin_stop_name} to {leg.destination_stop_name}",
            )
            confidence = "high"
        return TravelEstimate(
            origin=origin, destination=destination, mode=mode, departure_time=departure_time,
            duration_minutes=round(total_minutes, 1), distance_meters=None,
            provider=self.name, confidence=confidence, warnings=warnings,
        )

    def _best_leg(self, feed: GtfsFeed, origin: Location, destination: Location, departure_time: datetime) -> GtfsTransitLeg | None:
        origin_stops = feed.nearest_stops(round(origin.latitude, 6), round(origin.longitude, 6), self.max_walk_to_stop_km, self.max_candidate_stops)
        destination_stops = feed.nearest_stops(round(destination.latitude, 6), round(destination.longitude, 6), self.max_walk_from_stop_km, self.max_candidate_stops)
        if not origin_stops or not destination_stops:
            return None

        destination_stop_ids = {stop.id: walk_km for stop, walk_km in destination_stops}
        active_services = feed.active_service_ids(departure_time.date())
        if not active_services:
            return None

        departure_seconds = departure_time.hour * 3600 + departure_time.minute * 60 + departure_time.second
        best: tuple[float, GtfsTransitLeg] | None = None

        for origin_stop, origin_walk_km in origin_stops:
            earliest_board = departure_seconds + int(self._walk_minutes(origin_walk_km) * 60)
            latest_board = earliest_board + int(self.max_wait_minutes * 60)
            for trip_id, origin_index, origin_stop_time in feed.stop_visits_by_stop.get(origin_stop.id, ()):
                if origin_stop_time.departure_seconds < earliest_board:
                    continue
                if origin_stop_time.departure_seconds > latest_board:
                    break
                trip = feed.trips.get(trip_id)
                if not trip:
                    continue
                route_id, service_id = trip
                if service_id not in active_services:
                    continue
                stop_times = feed.stop_times_by_trip[trip_id]

                direct_found = False
                for destination_stop_time in stop_times[origin_index + 1 :]:
                    if destination_stop_time.stop_id not in destination_stop_ids:
                        continue
                    destination_stop = feed.stops[destination_stop_time.stop_id]
                    leg = GtfsTransitLeg(
                        origin_stop_id=origin_stop.id, origin_stop_name=origin_stop.name,
                        destination_stop_id=destination_stop.id, destination_stop_name=destination_stop.name,
                        trip_id=trip_id, route_id=route_id,
                        departure_seconds=origin_stop_time.departure_seconds, arrival_seconds=destination_stop_time.arrival_seconds,
                        walk_to_stop_minutes=self._walk_minutes(origin_walk_km),
                        walk_from_stop_minutes=self._walk_minutes(destination_stop_ids[destination_stop_time.stop_id]),
                        wait_minutes=(origin_stop_time.departure_seconds - earliest_board) / 60.0,
                    )
                    total = leg.walk_to_stop_minutes + leg.wait_minutes + (leg.arrival_seconds - leg.departure_seconds) / 60.0 + leg.walk_from_stop_minutes
                    if best is None or total < best[0]:
                        best = (total, leg)
                    direct_found = True
                    break

                if direct_found or self.max_transfers < 1:
                    continue

                for transfer_stop_time in stop_times[origin_index + 1 :]:
                    transfer_stop = feed.stops.get(transfer_stop_time.stop_id)
                    if transfer_stop is None:
                        continue
                    second_leg = self._best_second_leg(
                        feed=feed, transfer_stop=transfer_stop, transfer_arrival_seconds=transfer_stop_time.arrival_seconds,
                        destination_stop_ids=destination_stop_ids, active_services=active_services, first_trip_id=trip_id,
                    )
                    if second_leg is None:
                        continue
                    second_trip_id, second_route_id, second_origin_stop_time, final_stop_time = second_leg
                    destination_stop = feed.stops[final_stop_time.stop_id]
                    leg = GtfsTransitLeg(
                        origin_stop_id=origin_stop.id, origin_stop_name=origin_stop.name,
                        destination_stop_id=destination_stop.id, destination_stop_name=destination_stop.name,
                        trip_id=trip_id, route_id=route_id,
                        departure_seconds=origin_stop_time.departure_seconds, arrival_seconds=final_stop_time.arrival_seconds,
                        walk_to_stop_minutes=self._walk_minutes(origin_walk_km),
                        walk_from_stop_minutes=self._walk_minutes(destination_stop_ids[final_stop_time.stop_id]),
                        wait_minutes=(origin_stop_time.departure_seconds - earliest_board) / 60.0,
                        transfer_stop_id=transfer_stop.id, transfer_stop_name=transfer_stop.name,
                        second_trip_id=second_trip_id, second_route_id=second_route_id,
                        transfer_wait_minutes=(second_origin_stop_time.departure_seconds - transfer_stop_time.arrival_seconds) / 60.0,
                    )
                    total = (
                        leg.walk_to_stop_minutes + leg.wait_minutes
                        + (transfer_stop_time.arrival_seconds - origin_stop_time.departure_seconds) / 60.0
                        + leg.transfer_wait_minutes
                        + (final_stop_time.arrival_seconds - second_origin_stop_time.departure_seconds) / 60.0
                        + leg.walk_from_stop_minutes
                    )
                    if best is None or total < best[0]:
                        best = (total, leg)

        return best[1] if best else None

    def _best_second_leg(self, *, feed: GtfsFeed, transfer_stop: GtfsStop, transfer_arrival_seconds: int,
                          destination_stop_ids: dict[str, float], active_services: set[str], first_trip_id: str):
        earliest = transfer_arrival_seconds + int(self.min_transfer_minutes * 60)
        latest = earliest + int(self.max_transfer_wait_minutes * 60)
        best = None
        for second_trip_id, second_index, second_stop_time in feed.stop_visits_by_stop.get(transfer_stop.id, ()):
            if second_trip_id == first_trip_id:
                continue
            if second_stop_time.departure_seconds < earliest:
                continue
            if second_stop_time.departure_seconds > latest:
                break
            second_trip = feed.trips.get(second_trip_id)
            if not second_trip:
                continue
            second_route_id, second_service_id = second_trip
            if second_service_id not in active_services:
                continue
            for final_stop_time in feed.stop_times_by_trip[second_trip_id][second_index + 1 :]:
                if final_stop_time.stop_id not in destination_stop_ids:
                    continue
                total_seconds = final_stop_time.arrival_seconds - transfer_arrival_seconds
                candidate = (second_trip_id, second_route_id, second_stop_time, final_stop_time)
                if best is None or total_seconds < best[0]:
                    best = (total_seconds, candidate)
                break
        return best[1] if best else None

    def _walk_minutes(self, distance_km: float) -> float:
        return distance_km / self.walk_speed_kmh * 60.0
