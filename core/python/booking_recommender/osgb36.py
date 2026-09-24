"""OSGB36 National Grid (Eastings/Northings) -> WGS84 latitude/longitude.

Ordnance Survey Code-Point Open publishes postcode locations as National Grid
Eastings/Northings on the OSGB36 datum (Airy 1830 ellipsoid), not WGS84
lat/lon. This module implements the standard published conversion -- Redfearn
series inverse Transverse Mercator projection to get OSGB36 lat/lon, then a
seven-parameter Helmert datum transform to WGS84 -- so postcode coordinates
can be self-hosted without an external geocoding API. See Ordnance Survey's
"A guide to coordinate systems in Great Britain" (Annexe B/C) for the
reference algorithm and published Helmert parameters.
"""

from __future__ import annotations

import math

# Airy 1830 ellipsoid (OSGB36).
_AIRY_A = 6377563.396
_AIRY_B = 6356256.909
_F0 = 0.9996012717  # NatGrid scale factor on central meridian
_LAT0 = math.radians(49.0)
_LON0 = math.radians(-2.0)
_N0 = -100000.0
_E0 = 400000.0
_AIRY_E2 = 1 - (_AIRY_B**2) / (_AIRY_A**2)
_AIRY_N = (_AIRY_A - _AIRY_B) / (_AIRY_A + _AIRY_B)

# WGS84 ellipsoid.
_WGS84_A = 6378137.000
_WGS84_B = 6356752.3141

# Published OSGB36 -> WGS84 Helmert transform parameters (OS guide Annexe A).
_HELMERT_TX = 446.448
_HELMERT_TY = -125.157
_HELMERT_TZ = 542.060
_HELMERT_S = -20.4894 / 1_000_000.0  # scale, ppm -> ratio
_HELMERT_RX = math.radians(0.1502 / 3600.0)
_HELMERT_RY = math.radians(0.2470 / 3600.0)
_HELMERT_RZ = math.radians(0.8421 / 3600.0)


def en_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """Convert OSGB36 National Grid Eastings/Northings to WGS84 (lat, lon) degrees."""

    lat_osgb36, lon_osgb36 = _grid_to_osgb36_lat_lon(easting, northing)
    x, y, z = _lat_lon_to_cartesian(lat_osgb36, lon_osgb36, 0.0, _AIRY_A, _AIRY_E2)
    x2, y2, z2 = _helmert_transform(x, y, z)
    wgs84_e2 = 1 - (_WGS84_B**2) / (_WGS84_A**2)
    lat, lon, _ = _cartesian_to_lat_lon(x2, y2, z2, _WGS84_A, wgs84_e2)
    return math.degrees(lat), math.degrees(lon)


def _grid_to_osgb36_lat_lon(easting: float, northing: float) -> tuple[float, float]:
    lat = _LAT0
    m = 0.0
    while True:
        lat = (northing - _N0 - m) / (_AIRY_A * _F0) + lat
        m = _meridional_arc(lat)
        if abs(northing - _N0 - m) < 0.00001:
            break

    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    nu = _AIRY_A * _F0 * (1 - _AIRY_E2 * sin_lat**2) ** -0.5
    rho = _AIRY_A * _F0 * (1 - _AIRY_E2) * (1 - _AIRY_E2 * sin_lat**2) ** -1.5
    eta2 = nu / rho - 1

    tan_lat = math.tan(lat)
    tan2 = tan_lat**2
    tan4 = tan2**2
    tan6 = tan4 * tan2
    sec_lat = 1 / cos_lat
    nu3 = nu**3
    nu5 = nu**5
    nu7 = nu**7

    vii = tan_lat / (2 * rho * nu)
    viii = tan_lat / (24 * rho * nu3) * (5 + 3 * tan2 + eta2 - 9 * tan2 * eta2)
    ix = tan_lat / (720 * rho * nu5) * (61 + 90 * tan2 + 45 * tan4)
    x = sec_lat / nu
    xi = sec_lat / (6 * nu3) * (nu / rho + 2 * tan2)
    xii = sec_lat / (120 * nu5) * (5 + 28 * tan2 + 24 * tan4)
    xiia = sec_lat / (5040 * nu7) * (61 + 662 * tan2 + 1320 * tan4 + 720 * tan6)

    de = easting - _E0
    lat_result = lat - vii * de**2 + viii * de**4 - ix * de**6
    lon_result = _LON0 + x * de - xi * de**3 + xii * de**5 - xiia * de**7
    return lat_result, lon_result


def _meridional_arc(lat: float) -> float:
    n = _AIRY_N
    delta = lat - _LAT0
    total = lat + _LAT0
    return (
        _AIRY_B
        * _F0
        * (
            (1 + n + 1.25 * n**2 + 1.25 * n**3) * delta
            - (3 * n + 3 * n**2 + 2.625 * n**3) * math.sin(delta) * math.cos(total)
            + (1.875 * n**2 + 1.875 * n**3) * math.sin(2 * delta) * math.cos(2 * total)
            - (35 / 24) * n**3 * math.sin(3 * delta) * math.cos(3 * total)
        )
    )


def _lat_lon_to_cartesian(lat: float, lon: float, height: float, a: float, e2: float) -> tuple[float, float, float]:
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    nu = a / math.sqrt(1 - e2 * sin_lat**2)
    x = (nu + height) * cos_lat * math.cos(lon)
    y = (nu + height) * cos_lat * math.sin(lon)
    z = ((1 - e2) * nu + height) * sin_lat
    return x, y, z


def _cartesian_to_lat_lon(x: float, y: float, z: float, a: float, e2: float) -> tuple[float, float, float]:
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(10):
        sin_lat = math.sin(lat)
        nu = a / math.sqrt(1 - e2 * sin_lat**2)
        height = p / math.cos(lat) - nu
        new_lat = math.atan2(z, p * (1 - e2 * nu / (nu + height)))
        if abs(new_lat - lat) < 1e-12:
            lat = new_lat
            break
        lat = new_lat
    lon = math.atan2(y, x)
    return lat, lon, height


def _helmert_transform(x: float, y: float, z: float) -> tuple[float, float, float]:
    s = 1 + _HELMERT_S
    rx, ry, rz = _HELMERT_RX, _HELMERT_RY, _HELMERT_RZ
    new_x = _HELMERT_TX + s * (x - rz * y + ry * z)
    new_y = _HELMERT_TY + s * (rz * x + y - rx * z)
    new_z = _HELMERT_TZ + s * (-ry * x + rx * y + z)
    return new_x, new_y, new_z
