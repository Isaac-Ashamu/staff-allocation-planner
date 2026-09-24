import math
import unittest

from booking_recommender.osgb36 import en_to_wgs84


class Osgb36ConversionTests(unittest.TestCase):
    """Ground truth taken from postcodes.io (WGS84) and OS Code-Point Open (OSGB36
    Eastings/Northings) for the same real postcodes, spread across Great Britain."""

    def test_matches_postcodes_io_within_a_few_metres(self):
        cases = [
            ("M1 1AE", 384756, 398553, 53.483487, -2.231182),
            ("EH1 1YZ", 325597, 673676, 55.950328, -3.193018),
            ("CF10 1EP", 318200, 175860, 51.475764, -3.179217),
            ("IV2 3JQ", 267425, 845587, 57.481062, -4.212806),
        ]
        for name, easting, northing, expected_lat, expected_lon in cases:
            with self.subTest(postcode=name):
                lat, lon = en_to_wgs84(easting, northing)
                distance_metres = _approx_distance_metres(lat, lon, expected_lat, expected_lon)
                self.assertLess(distance_metres, 5.0)


def _approx_distance_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    mean_lat_cos = math.cos(math.radians((lat1 + lat2) / 2))
    dlat_m = (lat1 - lat2) * 111_320
    dlon_m = (lon1 - lon2) * 111_320 * mean_lat_cos
    return math.hypot(dlat_m, dlon_m)


if __name__ == "__main__":
    unittest.main()
