"""Geometry preparation: repair, decomposition, antimeridian handling, densification."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TypeVar

import numpy as np
import shapely
from pyproj import Geod, Transformer
from shapely.geometry import LinearRing, LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry, BaseMultipartGeometry

from app.geo.types import Issue

Coords = np.ndarray


def has_nonfinite(geom: BaseGeometry) -> bool:
    return not bool(np.isfinite(shapely.get_coordinates(geom)).all())


def repair(geom: BaseGeometry) -> tuple[BaseGeometry, Issue | None]:
    if shapely.is_valid(geom):
        return geom, None
    reason = shapely.is_valid_reason(geom)
    polygonal = isinstance(geom, Polygon | MultiPolygon)
    method = "structure" if polygonal else "linework"
    fixed = shapely.make_valid(geom, method=method, keep_collapsed=not polygonal)
    return fixed, Issue("geometry_repaired", f"Invalid geometry repaired ({reason}).")


def split_parts(geom: BaseGeometry) -> tuple[list[Polygon], list[LineString]]:
    """Flatten any geometry into its polygon and line parts; points are dropped."""
    polygons: list[Polygon] = []
    lines: list[LineString] = []

    def visit(g: BaseGeometry) -> None:
        if g.is_empty:
            return
        if isinstance(g, Polygon):
            polygons.append(g)
        elif isinstance(g, LinearRing):
            lines.append(LineString(g.coords))
        elif isinstance(g, LineString):
            lines.append(g)
        elif isinstance(g, BaseMultipartGeometry):
            for part in g.geoms:
                visit(part)

    visit(geom)
    return polygons, lines


G = TypeVar("G", BaseGeometry, np.ndarray)


def transform(geom: G, transformer: Transformer) -> G:
    """Transform one geometry, or a whole array of them in a single pyproj call."""

    def fn(xy: Coords) -> Coords:
        x, y = transformer.transform(xy[:, 0], xy[:, 1])
        return np.column_stack([x, y])

    result: G = shapely.transform(geom, fn)
    return result


def _map_rings(geom: Polygon | LineString, fn: Callable[[Coords], Coords]) -> Polygon | LineString:
    if isinstance(geom, Polygon):
        shell = fn(np.asarray(geom.exterior.coords))
        holes = [fn(np.asarray(r.coords)) for r in geom.interiors]
        return Polygon(shell, holes)
    return LineString(fn(np.asarray(geom.coords)))


def _unwrap(coords: Coords) -> Coords:
    if len(coords) < 2 or np.abs(np.diff(coords[:, 0])).max() <= 180.0:
        return coords
    out = coords.copy()
    out[:, 0] = np.unwrap(coords[:, 0], period=360.0)
    return out


def unwrap_antimeridian(geom: Polygon | LineString) -> tuple[Polygon | LineString, bool]:
    """Make longitudes continuous so a shape crossing +/-180 isn't read as spanning the globe.

    A ring stored as 179.9 -> -179.9 becomes 179.9 -> 180.1. Holes are shifted by whole
    turns so they stay next to their exterior ring.
    """
    if isinstance(geom, Polygon):
        rings = [np.asarray(geom.exterior.coords)] + [np.asarray(r.coords) for r in geom.interiors]
    else:
        rings = [np.asarray(geom.coords)]
    if all(len(r) < 2 or np.abs(np.diff(r[:, 0])).max() <= 180.0 for r in rings):
        return geom, False

    unwrapped = [_unwrap(r) for r in rings]
    ref = unwrapped[0][:, 0].mean()
    for ring in unwrapped[1:]:
        ring[:, 0] -= 360.0 * round((ring[:, 0].mean() - ref) / 360.0)
    if isinstance(geom, Polygon):
        return Polygon(unwrapped[0], unwrapped[1:]), True
    return LineString(unwrapped[0]), True


# No degree of latitude or longitude is longer than this on the WGS84 ellipsoid, so
# MAX_M_PER_DEG * hypot(dlon, dlat) bounds any distance inside a lon/lat box.
MAX_M_PER_DEG = 111_700.0


def max_extent_m(bounds: tuple[float, float, float, float]) -> float:
    minx, miny, maxx, maxy = bounds
    return MAX_M_PER_DEG * math.hypot(maxx - minx, maxy - miny)


def densify_geodesic(
    geom: Polygon | LineString, geod: Geod, max_segment_m: float
) -> Polygon | LineString:
    """Insert points along long edges so they follow the geodesic, not a projected straight line."""
    if max_extent_m(geom.bounds) <= max_segment_m:
        return geom

    def densify(coords: Coords) -> Coords:
        if len(coords) < 2:
            return coords
        lon, lat = coords[:, 0], coords[:, 1]
        _, _, dist = geod.inv(lon[:-1], lat[:-1], lon[1:], lat[1:])
        if float(np.max(dist)) <= max_segment_m:
            return coords
        out: list[tuple[float, float]] = []
        for i, d in enumerate(dist):
            out.append((float(lon[i]), float(lat[i])))
            if d > max_segment_m:
                n = math.ceil(d / max_segment_m) - 1
                out.extend(geod.npts(lon[i], lat[i], lon[i + 1], lat[i + 1], n))
        out.append((float(lon[-1]), float(lat[-1])))
        dense = np.asarray(out)
        # npts returns longitudes in [-180, 180]; restore continuity with unwrapped input
        dense[:, 0] = np.unwrap(dense[:, 0], period=360.0)
        return dense

    return _map_rings(geom, densify)
