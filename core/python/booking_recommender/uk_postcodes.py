"""Self-hosted UK postcode geocoding from Ordnance Survey Code-Point Open.

Code-Point Open (free, Open Government Licence, no API key) publishes every GB
postcode's location as OSGB36 National Grid Eastings/Northings, not WGS84
lat/lon, so ``osgb36.en_to_wgs84`` converts each row before it goes into the
local lookup database built by this module. Once built, ``LocalPostcodeGeocoder``
resolves postcodes entirely offline: no postcodes.io calls, no rate limits, no
network dependency at all for geocoding. Code-Point Open covers Great Britain
only, not Northern Ireland.

Download the source data from the OS Data Hub, no account required:
https://api.os.uk/downloads/v1/products/CodePointOpen/downloads?area=GB&format=CSV&redirect

Contains OS data (c) Crown copyright and database right. Code-Point Open is
licensed under the Open Government Licence:
https://www.ordnancesurvey.co.uk/opendata/licence
"""

from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import zipfile
from pathlib import Path
from typing import Iterable

from .models import Location
from .osgb36 import en_to_wgs84


def normalize_postcode(value: str | None) -> str:
    if not value:
        return ""
    return "".join(value.upper().split())


def postcode_area(value: str | None) -> str:
    normalized = normalize_postcode(value)
    if not normalized:
        return ""
    return normalized[:-3] if len(normalized) > 3 else normalized


def build_postcode_database(source: str | Path, db_path: str | Path) -> int:
    """Load a Code-Point Open ``codepo_gb.zip`` (or its extracted directory) into a
    local SQLite lookup table. Returns the number of postcodes written."""

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("DROP TABLE IF EXISTS uk_postcodes")
        connection.execute(
            """
            CREATE TABLE uk_postcodes (
                postcode TEXT PRIMARY KEY,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                area TEXT NOT NULL
            )
            """
        )
        count = 0
        batch: list[tuple[str, float, float, str]] = []
        for postcode, easting, northing in _read_code_point_open_rows(source):
            key = normalize_postcode(postcode)
            if not key:
                continue
            lat, lon = en_to_wgs84(easting, northing)
            batch.append((key, lat, lon, postcode_area(key)))
            count += 1
            if len(batch) >= 5000:
                connection.executemany(
                    "INSERT OR REPLACE INTO uk_postcodes (postcode, latitude, longitude, area) VALUES (?, ?, ?, ?)",
                    batch,
                )
                batch.clear()
        if batch:
            connection.executemany(
                "INSERT OR REPLACE INTO uk_postcodes (postcode, latitude, longitude, area) VALUES (?, ?, ?, ?)",
                batch,
            )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_uk_postcodes_area ON uk_postcodes(area)")
        connection.commit()
        return count
    finally:
        connection.close()


def _read_code_point_open_rows(source: str | Path) -> Iterable[tuple[str, float, float]]:
    """Yield (postcode, easting, northing) from a Code-Point Open zip or directory.

    Code-Point Open has no header row; columns are Postcode, Positional_quality
    _indicator, Eastings, Northings, Country_code, ... (see Doc/Code-Point_Open
    _Column_Headers.csv in the download).
    """

    path = Path(source)
    if path.is_dir():
        for csv_path in sorted(path.rglob("*.csv")):
            with csv_path.open(newline="", encoding="utf-8-sig") as handle:
                yield from _parse_code_point_open_csv(handle)
        return

    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if not name.startswith("Data/") or not name.lower().endswith(".csv"):
                continue
            with archive.open(name) as raw:
                text_stream = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                yield from _parse_code_point_open_csv(text_stream)


def _parse_code_point_open_csv(handle) -> Iterable[tuple[str, float, float]]:
    for row in csv.reader(handle):
        if len(row) < 4:
            continue
        postcode, _quality, easting, northing = row[0], row[1], row[2], row[3]
        try:
            yield postcode, float(easting), float(northing)
        except ValueError:
            continue


class LocalPostcodeGeocoder:
    """Offline ``PostcodeGeocoder`` backed by a database built with
    ``build_postcode_database``. Implements the same interface as
    ``PostcodesIoGeocoder`` (``resolve``/``prefetch``) so it's a drop-in
    replacement wherever a live-travel run needs postcode coordinates."""

    name = "local_postcodes"

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row

    def resolve(self, postcode: str) -> Location | None:
        key = normalize_postcode(postcode)
        if not key:
            return None
        row = self._connection.execute(
            "SELECT latitude, longitude, area FROM uk_postcodes WHERE postcode = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return Location(label=key, postcode=key, latitude=row["latitude"], longitude=row["longitude"], area=row["area"])

    def prefetch(self, postcodes: Iterable[str]) -> dict[str, Location | None]:
        """Present for interface parity with ``PostcodesIoGeocoder``; lookups are
        already a single indexed local query, so there is nothing to batch."""
        return {normalize_postcode(pc): self.resolve(pc) for pc in postcodes if normalize_postcode(pc)}

    def close(self) -> None:
        self._connection.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a local UK postcode geocode database from Code-Point Open.")
    parser.add_argument("source", help="Path to codepo_gb.zip, or its extracted directory.")
    parser.add_argument("db_path", help="SQLite file to write (overwritten if it already has a uk_postcodes table).")
    args = parser.parse_args(list(argv) if argv is not None else None)

    count = build_postcode_database(args.source, args.db_path)
    print(f"Wrote {count} postcodes to {args.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
