import pytest
from pyproj import CRS

from app.geo.crs import CrsContext, CrsKind, resolve_crs, utm_zone
from app.geo.types import CrsSource


@pytest.mark.parametrize(
    ("lon", "lat", "zone"),
    [
        (77.2, 28.6, 43),  # Delhi
        (-0.1, 51.5, 30),  # London
        (-180.0, 0.0, 1),
        (179.99, 0.0, 60),
        (5.3, 60.4, 32),  # Bergen: Norway exception
        (15.6, 78.2, 33),  # Svalbard exception
        (8.0, 78.0, 31),
    ],
)
def test_utm_zone(lon, lat, zone):
    assert utm_zone(lon, lat) == zone


@pytest.mark.parametrize(
    ("epsg", "kind"),
    [
        (4326, CrsKind.GEOGRAPHIC),
        (32643, CrsKind.PROJECTED),
        (2263, CrsKind.PROJECTED),
        (3857, CrsKind.UNSUITABLE),
        (3395, CrsKind.UNSUITABLE),
        (4087, CrsKind.UNSUITABLE),
    ],
)
def test_crs_kind(epsg, kind):
    assert CrsContext.build(CRS.from_epsg(epsg)).kind is kind


def test_unit_factor():
    assert CrsContext.build(CRS.from_epsg(2263)).unit_to_m == pytest.approx(1200 / 3937)
    assert CrsContext.build(CRS.from_epsg(32643)).unit_to_m == 1.0


def test_resolve_from_file():
    crs, source, issues = resolve_crs("EPSG:32643", None, None)
    assert crs.to_epsg() == 32643 and source is CrsSource.FILE and not issues


def test_resolve_override_wins():
    crs, source, issues = resolve_crs("EPSG:4326", CRS.from_epsg(32643), None)
    assert crs.to_epsg() == 32643 and source is CrsSource.OVERRIDE
    assert issues[0].code == "crs_overridden"


def test_resolve_missing_but_lonlat_like():
    crs, source, issues = resolve_crs(None, None, (77, 28, 78, 29))
    assert crs.to_epsg() == 4326 and source is CrsSource.ASSUMED
    assert issues[0].code == "crs_assumed"


def test_resolve_missing_and_projected_coords():
    crs, source, issues = resolve_crs(None, None, (500000, 3100000, 501000, 3101000))
    assert crs is None and source is CrsSource.UNKNOWN
    assert issues[0].code == "crs_unknown"


def test_resolve_garbage():
    crs, source, issues = resolve_crs("not a crs", None, None)
    assert crs is None and source is CrsSource.UNKNOWN
    assert issues[0].code == "crs_unparseable"
