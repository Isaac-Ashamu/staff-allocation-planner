"""Command-line wrapper for building the local UK postcode geocode database."""

from __future__ import annotations

from .uk_postcodes import main


if __name__ == "__main__":
    raise SystemExit(main())
