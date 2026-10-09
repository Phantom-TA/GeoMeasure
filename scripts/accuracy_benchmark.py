"""Compare ways of measuring area against the exact ellipsoidal answer.

    python scripts/accuracy_benchmark.py            # prints a markdown table

The test shape is a 0.01 x 0.01 degree cell (about 1 km^2 near the equator) whose exact
area on the WGS84 ellipsoid has a closed form, so no library is trusted as the reference.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyproj import CRS, Transformer
from shapely.geometry import LineString, box

from app.geo.crs import CrsContext
from app.geo.geometry import transform
from app.geo.measure import MeasureOptions, measure_geometry
from app.geo.types import Strategy

A, F = 6378137.0, 1 / 298.257223563
B, E = A * (1 - F), math.sqrt(2 * F - F * F)


def exact_cell_area(lat1: float, lat2: float, dlon: float) -> float:
    def q(lat: float) -> float:
        s = math.sin(math.radians(lat))
        return s / (1 - E * E * s * s) + math.log((1 + E * s) / (1 - E * s)) / (2 * E)

    return B * B * math.radians(dlon) / 2 * abs(q(lat2) - q(lat1))


WGS84 = CrsContext.build(CRS.from_epsg(4326))
TO_MERCATOR = Transformer.from_crs(4326, 3857, always_xy=True)


def area_errors(lat: float, lon: float = 10.0) -> dict[str, float]:
    cell = box(lon, lat, lon + 0.01, lat + 0.01)
    truth = exact_cell_area(lat, lat + 0.01, 0.01)
    naive = cell.area * 111_320.0**2  # "1 degree = 111.32 km" applied to both axes
    mercator = transform(cell, TO_MERCATOR).area
    utm = measure_geometry(cell, WGS84, MeasureOptions(strategy=Strategy.UTM)).area_m2
    local = measure_geometry(cell, WGS84).area_m2
    assert utm is not None and local is not None
    return {
        name: (value - truth) / truth * 100
        for name, value in (
            ("naive", naive),
            ("mercator", mercator),
            ("utm", utm),
            ("local", local),
        )
    }


def fmt(pct: float) -> str:
    if abs(pct) >= 1:
        return f"{pct:+.1f} %"
    if abs(pct) >= 0.001:
        return f"{pct:+.3f} %"
    return f"{pct:+.1e} %"


def main() -> None:
    print("Area error vs exact ellipsoidal area, 0.01 deg x 0.01 deg cell at lon 10 deg\n")
    print(
        "| Latitude | Degrees x 111.32 km | Web Mercator (EPSG:3857) | UTM zone | "
        "This API (local equal-area) |"
    )
    print("|---:|---:|---:|---:|---:|")
    for lat in (0, 30, 45, 60, 75, 85):
        e = area_errors(lat)
        print(
            f"| {lat} deg | {fmt(e['naive'])} | {fmt(e['mercator'])} | {fmt(e['utm'])} | "
            f"{fmt(e['local'])} |"
        )

    line = LineString([(70, 10), (80, 10), (80, 20)])  # 2,200 km, mostly far from its centre
    m = measure_geometry(line, WGS84)
    assert m.length_m is not None and m.geodesic_length_m is not None
    dev = abs(m.length_m - m.geodesic_length_m) / m.geodesic_length_m * 100
    print(
        f"\n2,200 km line: projected {m.length_m:,.1f} m vs geodesic "
        f"{m.geodesic_length_m:,.1f} m ({fmt(dev)})"
    )


if __name__ == "__main__":
    main()
