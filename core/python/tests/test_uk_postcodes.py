"""Self-hosted UK postcode geocoding: Code-Point Open import and local lookup.

Uses a tiny synthetic Code-Point Open-shaped CSV (a handful of made-up postcodes
with real GB-range Eastings/Northings), not the real 1.7M-row national dataset.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from booking_recommender import build_live_travel_estimator
from booking_recommender.uk_postcodes import LocalPostcodeGeocoder, build_postcode_database

# Real Code-Point Open Eastings/Northings for M1 1AE, used only as a stable
# ground-truth coordinate pair -- not a real worker/service-user address.
_SYNTHETIC_ROWS = [
    ("M1 1AE", "384756", "398553"),
    ("M1 1AF", "384359", "398229"),
]


class UkPostcodesTests(unittest.TestCase):
    def _build_db(self, tmp_dir: Path) -> Path:
        source_dir = tmp_dir / "source"
        source_dir.mkdir()
        (source_dir / "m.csv").write_text(
            "\n".join(f'"{pc}",10,{e},{n},"E92000001"' for pc, e, n in _SYNTHETIC_ROWS) + "\n"
        )
        db_path = tmp_dir / "postcodes.sqlite"
        count = build_postcode_database(source_dir, db_path)
        self.assertEqual(count, len(_SYNTHETIC_ROWS))
        return db_path

    def test_build_from_directory_and_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._build_db(Path(tmp))
            geocoder = LocalPostcodeGeocoder(db_path)
            try:
                location = geocoder.resolve("m1 1ae")
                self.assertIsNotNone(location)
                self.assertAlmostEqual(location.latitude, 53.483487, delta=0.001)
                self.assertAlmostEqual(location.longitude, -2.231182, delta=0.001)
                self.assertEqual(location.area, "M1")
                self.assertEqual(location.postcode, "M11AE")
            finally:
                geocoder.close()

    def test_unknown_postcode_resolves_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._build_db(Path(tmp))
            geocoder = LocalPostcodeGeocoder(db_path)
            try:
                self.assertIsNone(geocoder.resolve("ZZ99 9ZZ"))
                self.assertIsNone(geocoder.resolve(""))
            finally:
                geocoder.close()

    def test_prefetch_returns_all_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._build_db(Path(tmp))
            geocoder = LocalPostcodeGeocoder(db_path)
            try:
                results = geocoder.prefetch(["M1 1AE", "M1 1AF", "ZZ99 9ZZ"])
                self.assertEqual(set(results), {"M11AE", "M11AF", "ZZ999ZZ"})
                self.assertIsNotNone(results["M11AE"])
                self.assertIsNone(results["ZZ999ZZ"])
            finally:
                geocoder.close()

    def test_build_from_zip_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = tmp_path / "codepo_gb.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr(
                    "Data/CSV/m.csv",
                    "\n".join(f'"{pc}",10,{e},{n},"E92000001"' for pc, e, n in _SYNTHETIC_ROWS) + "\n",
                )
                archive.writestr("Doc/licence.txt", "Ordnance Survey Data Licence\n")
            db_path = tmp_path / "postcodes.sqlite"
            count = build_postcode_database(zip_path, db_path)
            self.assertEqual(count, len(_SYNTHETIC_ROWS))
            geocoder = LocalPostcodeGeocoder(db_path)
            try:
                self.assertIsNotNone(geocoder.resolve("M1 1AE"))
            finally:
                geocoder.close()

    def test_build_live_travel_estimator_prefers_local_postcode_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._build_db(Path(tmp))
            with mock.patch.dict(os.environ, {"UK_POSTCODE_DB_PATH": str(db_path)}, clear=True):
                estimator = build_live_travel_estimator(tenant_id="test-tenant")
            self.assertIsInstance(estimator.geocoder, LocalPostcodeGeocoder)


if __name__ == "__main__":
    unittest.main()
