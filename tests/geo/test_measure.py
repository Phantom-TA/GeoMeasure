import pytest
from pyproj import CRS, Transformer
from shapely import affinity
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from app.geo.crs import CrsContext
from app.geo.geometry import transform
from app.geo.measure import MeasureOptions, measure_geometry
from app.geo.types import MeasureStatus, Method, Strategy
from tests.geo.truth import EQUATOR_DEGREE_M, cell_area

WGS84 = CrsContext.build(CRS.from_epsg(4326))


def codes(m):
    return {i.code for i in m.issues}


def rel(a, b):
    return abs(a - b) / b


@pytest.mark.parametrize("lat", [0.0, 28.6, 45.0, -33.9, 70.0, 89.5])
def test_cell_area_matches_closed_form(lat):
    cell = box(10.0, lat - 0.005, 10.01, lat + 0.005)
    truth = cell_area(lat - 0.005, lat + 0.005, 0.01)
    m = measure_geometry(cell, WGS84)
    assert m.status is MeasureStatus.OK
    assert m.method is Method.LOCAL
    assert rel(m.area_m2, truth) < 1e-6
    assert rel(m.geodesic_area_m2, truth) < 1e-6
    assert m.deviation_pct < 1e-3
    assert m.length_m is None


def test_equator_degree_length():
    m = measure_geometry(LineString([(0, 0), (1, 0)]), WGS84)
    assert rel(m.length_m, EQUATOR_DEGREE_M) < 1e-6
    assert rel(m.geodesic_length_m, EQUATOR_DEGREE_M) < 1e-9
    assert m.area_m2 is None


def test_utm_strategy_close_but_less_exact_than_local():
    cell = box(77.0, 28.0, 77.01, 28.01)
    truth = cell_area(28.0, 28.01, 0.01)
    m = measure_geometry(cell, WGS84, MeasureOptions(strategy=Strategy.UTM))
    assert m.method is Method.UTM
    assert m.projections == ["UTM 43N (EPSG:32643)"]
    assert rel(m.area_m2, truth) < 2e-3


def test_web_mercator_source_is_reprojected():
    lat = 60.0
    cell = box(10.0, lat, 10.01, lat + 0.01)
    merc = transform(cell, Transformer.from_crs(4326, 3857, always_xy=True))
    naive = merc.area
    truth = cell_area(lat, lat + 0.01, 0.01)
    assert naive / truth > 3.9  # the trap: Web Mercator inflates area ~4x at 60N

    m = measure_geometry(merc, CrsContext.build(CRS.from_epsg(3857)))
    assert m.method is Method.LOCAL
    assert "source_crs_unsuitable" in codes(m)
    assert rel(m.area_m2, truth) < 1e-6


def test_projected_source_used_natively():
    ctx = CrsContext.build(CRS.from_epsg(32643))
    square = box(500_000, 3_100_000, 501_000, 3_101_000)
    m = measure_geometry(square, ctx)
    assert m.method is Method.SOURCE
    assert m.area_m2 == pytest.approx(1_000_000)
    assert m.perimeter_m == pytest.approx(4_000)
    # ground area differs from grid area by the UTM scale factor (0.9996 on the central meridian)
    assert m.geodesic_area_m2 == pytest.approx(1_000_000 / 0.9996**2, rel=1e-4)
    assert "high_deviation" not in codes(m)


def test_us_survey_feet_converted():
    ctx = CrsContext.build(CRS.from_epsg(2263))  # NY Long Island, US survey feet
    square = box(1_000_000, 200_000, 1_001_000, 201_000)
    m = measure_geometry(square, ctx)
    ft = 1200 / 3937
    assert m.method is Method.SOURCE
    assert m.area_m2 == pytest.approx(1_000_000 * ft * ft)
    assert m.perimeter_m == pytest.approx(4_000 * ft)
    assert m.deviation_pct < 0.1


def test_projected_feature_outside_area_of_use_is_reprojected():
    ctx = CrsContext.build(CRS.from_epsg(32643))
    far = box(5_000_000, 3_100_000, 5_001_000, 3_101_000)  # far outside zone 43N
    m = measure_geometry(far, ctx)
    assert m.method is Method.LOCAL
    assert "outside_crs_area_of_use" in codes(m)


def test_polygon_with_hole():
    outer = [(0, 0), (0.02, 0), (0.02, 0.02), (0, 0.02)]
    hole = [(0.005, 0.005), (0.015, 0.005), (0.015, 0.015), (0.005, 0.015)]
    m = measure_geometry(Polygon(outer, [hole]), WGS84)
    truth = cell_area(0, 0.02, 0.02) - cell_area(0.005, 0.015, 0.01)
    assert rel(m.area_m2, truth) < 1e-5
    assert rel(m.geodesic_area_m2, truth) < 1e-5


def test_antimeridian_polygon():
    poly = Polygon([(179.99, 0), (-179.99, 0), (-179.99, 0.01), (179.99, 0.01)])
    m = measure_geometry(poly, WGS84)
    truth = cell_area(0, 0.01, 0.02)
    assert "antimeridian_normalized" in codes(m)
    assert rel(m.area_m2, truth) < 1e-5
    assert rel(m.geodesic_area_m2, truth) < 1e-5


def test_polar_utm_falls_back_to_ups_and_flags_its_distortion():
    cell = box(10, 89.5, 10.5, 89.6)
    truth = cell_area(89.5, 89.6, 0.5)
    m = measure_geometry(cell, WGS84, MeasureOptions(strategy=Strategy.UTM))
    assert m.projections == ["UPS North (EPSG:32661)"]
    # UPS has scale 0.994 at the pole, so areas come out ~1.2% small; the self-check notices
    assert rel(m.area_m2, truth) == pytest.approx(1 - 0.994**2, rel=0.05)
    assert "high_deviation" in codes(m)

    local = measure_geometry(cell, WGS84)
    # near the pole straight edges and curved parallels enclose slightly different areas,
    # so compare against the geodesic polygon, which shares the polygon's edge definition
    assert rel(local.area_m2, local.geodesic_area_m2) < 1e-6
    assert rel(local.area_m2, truth) < 1e-4
    assert "high_deviation" not in codes(local)


def test_multipolygon_parts_far_apart():
    india = box(77.0, 28.0, 77.01, 28.01)
    brazil = box(-47.0, -15.0, -46.99, -14.99)
    m = measure_geometry(MultiPolygon([india, brazil]), WGS84)
    truth = cell_area(28.0, 28.01, 0.01) + cell_area(-15.0, -14.99, 0.01)
    assert rel(m.area_m2, truth) < 1e-6
    assert len(m.projections) == 4  # laea + aeqd per part


def test_bowtie_is_repaired():
    bowtie = Polygon([(0, 0), (0.01, 0.01), (0.01, 0), (0, 0.01)])
    m = measure_geometry(bowtie, WGS84)
    assert m.status is MeasureStatus.OK
    assert "geometry_repaired" in codes(m)
    assert rel(m.area_m2, cell_area(0, 0.01, 0.01) / 2) < 1e-3


def test_geometry_collection_measures_both():
    gc = GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 0)]), box(0, 0, 0.01, 0.01)])
    m = measure_geometry(gc, WGS84)
    assert rel(m.length_m, EQUATOR_DEGREE_M) < 1e-6
    assert rel(m.area_m2, cell_area(0, 0.01, 0.01)) < 1e-6


def test_z_coordinates_ignored():
    flat = box(77.0, 28.0, 77.01, 28.01)
    raised = Polygon([(x, y, 250.0) for x, y in flat.exterior.coords])
    assert measure_geometry(raised, WGS84).area_m2 == pytest.approx(
        measure_geometry(flat, WGS84).area_m2
    )


def test_large_polygon_matches_geodesic():
    big = box(70, 10, 80, 20)  # ~1100 km square
    m = measure_geometry(big, WGS84)
    assert rel(m.area_m2, m.geodesic_area_m2) < 1e-6
    assert m.deviation_pct < 0.001
    assert any("local centres" in p for p in m.projections)


def test_long_line_off_centre_matches_geodesic():
    line = LineString([(70, 10), (80, 10), (80, 20)])  # 2200 km, mostly far from its centre
    m = measure_geometry(line, WGS84)
    assert m.deviation_pct < 0.001


def test_scaled_shapes_scale_quadratically():
    small = box(77.0, 28.0, 77.001, 28.001)
    big = affinity.scale(small, 2, 2)
    a1 = measure_geometry(small, WGS84).area_m2
    a2 = measure_geometry(big, WGS84).area_m2
    assert a2 / a1 == pytest.approx(4, rel=1e-3)


@pytest.mark.parametrize(
    ("geom", "status", "code"),
    [
        (None, MeasureStatus.SKIPPED, "no_geometry"),
        (Polygon(), MeasureStatus.SKIPPED, "empty_geometry"),
        (Point(77, 28), MeasureStatus.NOT_APPLICABLE, "not_measurable"),
        (Point(float("nan"), 0), MeasureStatus.ERROR, "non_finite_coordinates"),
    ],
)
def test_unmeasurable_inputs(geom, status, code):
    m = measure_geometry(geom, WGS84)
    assert m.status is status
    assert code in codes(m)


def test_lonlat_hint_gives_identical_results():
    ctx = CrsContext.build(CRS.from_epsg(32643))
    square = box(500_000, 3_100_000, 501_000, 3_101_000)
    hint = transform(square, ctx.to_lonlat)
    with_hint = measure_geometry(square, ctx, lonlat=hint)
    without = measure_geometry(square, ctx)
    assert with_hint == without


def test_lonlat_hint_ignored_after_repair():
    ctx = CrsContext.build(CRS.from_epsg(32643))
    bowtie = Polygon(
        [(500_000, 3_100_000), (501_000, 3_101_000), (501_000, 3_100_000), (500_000, 3_101_000)]
    )
    wrong_hint = box(0, 0, 1, 1)  # would give a wildly different result if trusted
    m = measure_geometry(bowtie, ctx, lonlat=wrong_hint)
    assert m.area_m2 == pytest.approx(500_000)
    assert "geometry_repaired" in codes(m)


@pytest.mark.parametrize("bounds", [(10, 95, 10.01, 95.01), (1000, 10, 1000.01, 10.01)])
def test_out_of_range_lonlat(bounds):
    m = measure_geometry(box(*bounds), WGS84)
    assert m.status is MeasureStatus.ERROR
    assert "invalid_coordinates" in codes(m)


def test_0_360_longitudes_accepted():
    m = measure_geometry(box(200.0, 10.0, 200.01, 10.01), WGS84)
    assert m.status is MeasureStatus.OK
    assert rel(m.area_m2, cell_area(10.0, 10.01, 0.01)) < 1e-6


def test_unknown_crs():
    m = measure_geometry(box(0, 0, 1, 1), None)
    assert m.status is MeasureStatus.ERROR
    assert "crs_unknown" in codes(m)
