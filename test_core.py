"""Default unittest discovery bridge for the core package tests."""

from __future__ import annotations

from pathlib import Path


def load_tests(loader, tests, pattern):
    root = Path(__file__).resolve().parent
    return loader.discover(str(root / "core" / "python" / "tests"), pattern or "test*.py")
