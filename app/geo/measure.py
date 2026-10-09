"""Per-feature measurement: a projected value plus an independent geodesic cross-check."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import cast

import numpy as np
import shapely
from pyproj.exceptions import ProjError
from shapely.errors import GEOSException
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient

from app.geo.crs import CrsContext, CrsKind, local_projection, utm_projection
from app.geo.geometry import (
    densify_geodesic,
    has_nonfinite,
    max_extent_m,
    repair,
    split_parts,
    transform,
    unwrap_antimeridian,
)
from app.geo.types import Issue, Measurement, MeasureStatus, Method, Strategy


@dataclass(frozen=True)
class MeasureOptions:
    strategy: Strategy = Strategy.AUTO
    deviation_warn_pct: float = 0.5
    densify_max_segment_m: float = 10_000.0
    # Lines are measured in chunks, each in an equidistant projection centred on it, so no
    # vertex is far from its projection centre (AEQD distortion grows with distance^2).
    length_chunk_m: float = 100_000.0


@dataclass
class _Values:
    area: float | None = None
    perimeter: float | None = None
    length: float | None = None


class _MeasureError(Exception):
    def __init__(self, issue: Issue) -> None:
        self.issue = issue


def measure_geometry(
    geom: BaseGeometry | None,
    ctx: CrsContext | None,
    options: MeasureOptions | None = None,
) -> Measurement:
    options = options or MeasureOptions()
    if geom is None:
        return _fail(MeasureStatus.SKIPPED, "no_geometry", "Feature has no geometry.")
    if geom.is_empty:
        return _fail(MeasureStatus.SKIPPED, "empty_geometry", "Feature geometry is empty.")
    try:
        return _measure(geom, ctx, options)
    except _MeasureError as exc:
        return Measurement(MeasureStatus.ERROR, issues=[exc.issue])
    except (ProjError, GEOSException, ValueError) as exc:
        return _fail(MeasureStatus.ERROR, "measurement_failed", f"Measurement failed: {exc}")


def _fail(status: MeasureStatus, code: str, message: str) -> Measurement:
    return Measurement(status, issues=[Issue(code, message)])


def _measure(geom: BaseGeometry, ctx: CrsContext | None, options: MeasureOptions) -> Measurement:
    issues: list[Issue] = []
    geom = shapely.force_2d(geom)
    if has_nonfinite(geom):
        raise _MeasureError(Issue("non_finite_coordinates", "Geometry has NaN/inf coordinates."))

    geom, repaired = repair(geom)
    if repaired:
        issues.append(repaired)

    polygons, lines = split_parts(geom)
    if not polygons and not lines:
        issues.append(Issue("not_measurable", f"{geom.geom_type} has no area or length."))
        return Measurement(MeasureStatus.NOT_APPLICABLE, issues=issues)
    if ctx is None:
        raise _MeasureError(Issue("crs_unknown", "Cannot measure without a known CRS."))

    ll_polygons, ll_lines, unwrapped = _to_lonlat(polygons, lines, ctx)
    if unwrapped:
        issues.append(Issue("antimeridian_normalized", "Geometry crosses the antimeridian."))

    geodesic = _geodesic(ll_polygons, ll_lines, ctx)
    method, method_issues = _pick_method(options.strategy, ctx, ll_polygons, ll_lines)
    issues.extend(method_issues)
    projected, projections = _projected(
        method, polygons, lines, ll_polygons, ll_lines, ctx, options
    )
    deviation = _deviation(projected, geodesic)

    if (
        method is Method.SOURCE
        and options.strategy is Strategy.AUTO
        and deviation is not None
        and deviation > options.deviation_warn_pct
    ):
        issues.append(
            Issue(
                "source_crs_fallback",
                f"{ctx.label} differed from geodesic by {deviation:.3g}%; re-measured locally.",
            )
        )
        method = Method.LOCAL
        projected, projections = _projected(
            method, polygons, lines, ll_polygons, ll_lines, ctx, options
        )
        deviation = _deviation(projected, geodesic)

    if deviation is not None and deviation > options.deviation_warn_pct:
        issues.append(
            Issue(
                "high_deviation",
                f"Projected and geodesic results differ by {deviation:.3g}%; "
                "prefer the geodesic value.",
            )
        )

    return Measurement(
        MeasureStatus.OK,
        area_m2=projected.area,
        perimeter_m=projected.perimeter,
        length_m=projected.length,
        geodesic_area_m2=geodesic.area,
        geodesic_perimeter_m=geodesic.perimeter,
        geodesic_length_m=geodesic.length,
        deviation_pct=deviation,
        method=method,
        projections=projections,
        issues=issues,
    )


def _to_lonlat(
    polygons: list[Polygon], lines: list[LineString], ctx: CrsContext
) -> tuple[list[Polygon], list[LineString], bool]:
    unwrapped = False

    def convert(part: Polygon | LineString) -> Polygon | LineString:
        nonlocal unwrapped
        if ctx.to_lonlat is not None:
            part = transform(part, ctx.to_lonlat)
            if has_nonfinite(part):
                raise _MeasureError(
                    Issue(
                        "projection_failed",
                        f"Coordinates are outside the valid area of {ctx.label}.",
                    )
                )
        part, changed = unwrap_antimeridian(part)
        unwrapped |= changed
        return part

    ll_polygons = [p for p in map(convert, polygons) if isinstance(p, Polygon)]
    ll_lines = [ln for ln in map(convert, lines) if isinstance(ln, LineString)]
    return ll_polygons, ll_lines, unwrapped


def _geodesic(polygons: list[Polygon], lines: list[LineString], ctx: CrsContext) -> _Values:
    out = _Values()
    if polygons:
        out.area = out.perimeter = 0.0
        for p in polygons:
            # pyproj needs CCW exteriors and CW holes to subtract holes correctly
            area, perimeter = ctx.geod.geometry_area_perimeter(orient(p, 1.0))
            out.area += abs(area)
            out.perimeter += perimeter
    if lines:
        out.length = sum(ctx.geod.geometry_length(ln) for ln in lines)
    return out


def _bounds(parts: list[Polygon] | list[LineString]) -> tuple[float, float, float, float]:
    b = shapely.total_bounds(parts)
    return float(b[0]), float(b[1]), float(b[2]), float(b[3])


def _pick_method(
    strategy: Strategy, ctx: CrsContext, polygons: list[Polygon], lines: list[LineString]
) -> tuple[Method, list[Issue]]:
    if strategy is Strategy.LOCAL:
        return Method.LOCAL, []
    if strategy is Strategy.UTM:
        return Method.UTM, []
    if ctx.kind is CrsKind.UNSUITABLE:
        return Method.LOCAL, [
            Issue(
                "source_crs_unsuitable",
                f"{ctx.label} distorts area and distance; reprojected for measurement.",
            )
        ]
    if ctx.kind is CrsKind.PROJECTED:
        if ctx.covers(_bounds([*polygons, *lines])):
            return Method.SOURCE, []
        return Method.LOCAL, [
            Issue(
                "outside_crs_area_of_use",
                f"Feature lies outside the area of use of {ctx.label}; reprojected.",
            )
        ]
    return Method.LOCAL, []


def _projected(
    method: Method,
    polygons: list[Polygon],
    lines: list[LineString],
    ll_polygons: list[Polygon],
    ll_lines: list[LineString],
    ctx: CrsContext,
    options: MeasureOptions,
) -> tuple[_Values, list[str]]:
    out = _Values()
    labels: list[str] = []

    if method is Method.SOURCE:
        f = ctx.unit_to_m
        if polygons:
            out.area = sum(p.area for p in polygons) * f * f
            out.perimeter = sum(p.length for p in polygons) * f
        if lines:
            out.length = sum(ln.length for ln in lines) * f
        return out, [ctx.label]

    def dense(part: Polygon | LineString) -> Polygon | LineString:
        return densify_geodesic(part, ctx.geod, options.densify_max_segment_m)

    if method is Method.UTM:
        minx, miny, maxx, maxy = _bounds([*ll_polygons, *ll_lines])
        t, label = utm_projection(ctx, (minx + maxx) / 2, (miny + maxy) / 2)
        if ll_polygons:
            projected = [transform(dense(p), t) for p in ll_polygons]
            out.area = sum(p.area for p in projected)
            out.perimeter = sum(p.length for p in projected)
        if ll_lines:
            out.length = sum(transform(dense(ln), t).length for ln in ll_lines)
        _check_finite(out, label)
        return out, [label]

    # Method.LOCAL: each part gets its own projection centred on it, chosen per quantity:
    # equal-area for area, equidistant for lengths.
    if ll_polygons:
        out.area = out.perimeter = 0.0
        for p in ll_polygons:
            minx, miny, maxx, maxy = p.bounds
            d = cast(Polygon, dense(p))
            t_area, l_area = local_projection(ctx, "laea", (minx + maxx) / 2, (miny + maxy) / 2)
            out.area += transform(d, t_area).area
            labels.append(l_area)
            for ring in (d.exterior, *d.interiors):
                out.perimeter += _local_length(np.asarray(ring.coords), ctx, options, labels)
    if ll_lines:
        out.length = 0.0
        for ln in ll_lines:
            out.length += _local_length(np.asarray(dense(ln).coords), ctx, options, labels)
    _check_finite(out, "local projection")
    return out, _summarize(labels)


def _summarize(labels: list[str], max_per_kind: int = 3) -> list[str]:
    """Keep the projection list readable when a long line used many local centres."""
    unique = list(dict.fromkeys(labels))
    out: list[str] = []
    for kind in ("LAEA", "AEQD"):
        of_kind = [lbl for lbl in unique if lbl.startswith(kind)]
        if len(of_kind) > max_per_kind:
            out.append(f"{kind} ({len(of_kind)} local centres)")
        else:
            out.extend(of_kind)
    return out


def _local_length(
    coords: np.ndarray, ctx: CrsContext, options: MeasureOptions, labels: list[str]
) -> float:
    if len(coords) < 2:
        return 0.0
    lon, lat = coords[:, 0], coords[:, 1]
    bounds = (float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()))
    if max_extent_m(bounds) <= options.length_chunk_m / 2:
        return _aeqd_length(coords, ctx, labels)

    _, _, seg = ctx.geod.inv(lon[:-1], lat[:-1], lon[1:], lat[1:])
    chunk_of_segment = np.concatenate([[0.0], np.cumsum(seg)])[:-1] // options.length_chunk_m
    total = 0.0
    for chunk in np.unique(chunk_of_segment):
        idx = np.flatnonzero(chunk_of_segment == chunk)
        total += _aeqd_length(coords[idx[0] : idx[-1] + 2], ctx, labels)
    return total


def _aeqd_length(pts: np.ndarray, ctx: CrsContext, labels: list[str]) -> float:
    cx = (pts[:, 0].min() + pts[:, 0].max()) / 2
    cy = (pts[:, 1].min() + pts[:, 1].max()) / 2
    t, label = local_projection(ctx, "aeqd", cx, cy)
    x, y = t.transform(pts[:, 0], pts[:, 1])
    labels.append(label)
    return float(np.hypot(np.diff(x), np.diff(y)).sum())


def _check_finite(values: _Values, label: str) -> None:
    for v in (values.area, values.perimeter, values.length):
        if v is not None and not math.isfinite(v):
            raise _MeasureError(Issue("projection_failed", f"Projection to {label} failed."))


def _deviation(projected: _Values, geodesic: _Values) -> float | None:
    pairs = [
        (projected.area, geodesic.area),
        (projected.perimeter, geodesic.perimeter),
        (projected.length, geodesic.length),
    ]
    devs = [abs(p - g) / g * 100.0 for p, g in pairs if p is not None and g]
    return max(devs) if devs else None
