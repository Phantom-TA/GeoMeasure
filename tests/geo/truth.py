"""Closed-form reference values on the WGS84 ellipsoid, independent of pyproj."""

import math

A = 6378137.0
F = 1 / 298.257223563
B = A * (1 - F)
E = math.sqrt(2 * F - F * F)

EQUATOR_DEGREE_M = A * math.pi / 180  # 111319.4907932...


def _q(lat_deg: float) -> float:
    s = math.sin(math.radians(lat_deg))
    return s / (1 - E * E * s * s) + math.log((1 + E * s) / (1 - E * s)) / (2 * E)


def cell_area(lat1: float, lat2: float, dlon: float) -> float:
    """Exact area of the region bounded by two parallels and two meridians."""
    return B * B * math.radians(dlon) / 2 * abs(_q(lat2) - _q(lat1))
