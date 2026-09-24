"""Command-line helpers for local persistence and audit storage."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

from .persistence import initialize_database, seed_development_data

_DATABASE_HELP = "SQLite file path, or a postgresql://... DSN for the optional Postgres backend."


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the recommender audit database.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create or migrate the audit database.")
    init_parser.add_argument("database", help=_DATABASE_HELP)

    seed_parser = subparsers.add_parser("seed-dev", help="Create a synthetic development tenant.")
    seed_parser.add_argument("database", help=_DATABASE_HELP)
    seed_parser.add_argument("--tenant-id", default="dev-tenant", help="Tenant ID to create.")

    args = parser.parse_args(list(argv) if argv is not None else None)
    database = args.database
    if not database.startswith("postgresql://") and not database.startswith("postgres://"):
        database = Path(database)
        database.parent.mkdir(parents=True, exist_ok=True)

    if args.command == "init":
        initialize_database(database)
        print(f"Initialized audit database at {database}")
        return 0
    if args.command == "seed-dev":
        seed_development_data(database, tenant_id=args.tenant_id)
        print(f"Seeded development tenant {args.tenant_id} in {database}")
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
