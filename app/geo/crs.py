"""CRS classification and selection of projections suitable for measurement."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

from pyproj import CRS, Geod, Transformer
from pyproj.exceptions import CRSError

from app.geo.types import CrsSource, Issue

WGS84 = CRS.from_epsg(4326)

# Feature centres are snapped to this grid so nearby features share a cached transformer.
# Equal-area projections preserve area everywhere, so the snap costs no area accuracy;
# for lengths the extra distortion is ~1e-6 relative.
CENTER_SNAP_DEG = 0.25


class CrsKind(StrEnum):
    GEOGRAPHIC = "geographic"
    PROJECTED = "projected"
    UNSUITABLE = "unsuitable_projected"


def crs_label(crs: CRS) -> str:
    auth = crs.to_authority(min_confidence=70)
    return f"{auth[0]}:{auth[1]}" if auth else crs.name


def horizontal_part(crs: CRS) -> CRS:
    if crs.is_compound and crs.sub_crs_list:
        crs = crs.sub_crs_list[0]
    if crs.is_bound and crs.source_crs is not None:
        crs = crs.source_crs
    return crs


def is_unsuitable_projection(crs: CRS) -> bool:
    """Projections whose scale varies so much that measuring in them is meaningless.

    Web Mercator inflates areas by 1/cos^2(lat): 2x at 45 deg, 4x at 60 deg.
    """
    op = crs.coordinate_operation
    method = (op.method_name if op else "").lower()
    if "mercator" in method and "transverse" not in method and "oblique" not in method:
        return True
    return any(hint in method for hint in ("equidistant cylindrical", "plate carr", "miller"))


@dataclass(frozen=True)
class CrsContext:
    crs: CRS
    kind: CrsKind
    label: str
    geod: Geod
    ellipsoid: str
    is_wgs84: bool
    to_lonlat: Transformer | None
    unit_to_m: float
    area_of_use: tuple[float, float, float, float] | None

    @classmethod
    def build(cls, crs: CRS) -> CrsContext:
        crs = horizontal_part(crs)
        geodetic = crs.geodetic_crs or WGS84
        geod = geodetic.get_geod() or Geod(ellps="WGS84")
        ellipsoid = f"+a={geod.a!r} +rf={1 / geod.f!r}" if geod.f else f"+R={geod.a!r}"
        is_wgs84 = geodetic.equals(WGS84, ignore_axis_order=True)
        aou = crs.area_of_use
        area = (aou.west, aou.south, aou.east, aou.north) if aou else None

        if crs.is_geographic:
            kind, to_lonlat, unit = CrsKind.GEOGRAPHIC, None, 1.0
        else:
            kind = CrsKind.UNSUITABLE if is_unsuitable_projection(crs) else CrsKind.PROJECTED
            to_lonlat = Transformer.from_crs(crs, geodetic, always_xy=True)
            unit = crs.axis_info[0].unit_conversion_factor if crs.axis_info else 1.0
        return cls(crs, kind, crs_label(crs), geod, ellipsoid, is_wgs84, to_lonlat, unit, area)

    def covers(self, lonlat_bounds: tuple[float, float, float, float], margin: float = 1.0) -> bool:
        """True if the feature lies inside the CRS's published area of use."""
        if self.area_of_use is None:
            return True
        west, south, east, north = self.area_of_use
        minx, miny, maxx, maxy = lonlat_bounds
        if miny < south - margin or maxy > north + margin:
            return False
        lons = [_wrap_lon(minx), _wrap_lon(maxx)]
        if west <= east:
            return all(west - margin <= x <= east + margin for x in lons)
        return all(x >= west - margin or x <= east + margin for x in lons)


def _wrap_lon(lon: float) -> float:
    return ((lon + 180.0) % 360.0) - 180.0


def _snap(value: float) -> float:
    return round(value / CENTER_SNAP_DEG) * CENTER_SNAP_DEG


@lru_cache(maxsize=4096)
def _pipeline(projection: str, ellipsoid: str) -> Transformer:
    # Lon/lat -> projection on the same ellipsoid is a pure conversion, so we build the PROJ
    # pipeline directly. Transformer.from_crs would search the PROJ database each time
    # (~13 ms vs ~0.3 ms) for an identical result.
    return Transformer.from_pipeline(
        "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
        f"+step {projection} {ellipsoid}"
    )


def local_projection(ctx: CrsContext, kind: str, lon: float, lat: float) -> tuple[Transformer, str]:
    """Azimuthal projection centred on the feature.

    kind="laea": Lambert Azimuthal Equal-Area, preserves area exactly.
    kind="aeqd": Azimuthal Equidistant, preserves distance from the centre.
    """
    lat0 = max(-90.0, min(90.0, _snap(lat)))
    lon0 = _wrap_lon(_snap(lon))
    if lon0 == 180.0:
        lon0 = -180.0
    transformer = _pipeline(f"+proj={kind} +lat_0={lat0!r} +lon_0={lon0!r}", ctx.ellipsoid)
    return transformer, f"{kind.upper()}(lat_0={lat0:g}, lon_0={lon0:g})"


def utm_zone(lon: float, lat: float) -> int:
    """UTM zone number, including the Norway and Svalbard exceptions."""
    lon = _wrap_lon(lon)
    zone = min(int((lon + 180.0) // 6.0) + 1, 60)
    if 56.0 <= lat < 64.0 and 3.0 <= lon < 12.0:
        return 32
    if 72.0 <= lat < 84.0 and 0.0 <= lon < 42.0:
        if lon < 9.0:
            return 31
        if lon < 21.0:
            return 33
        if lon < 33.0:
            return 35
        return 37
    return zone


def utm_projection(ctx: CrsContext, lon: float, lat: float) -> tuple[Transformer, str]:
    """UTM zone of the given point, or UPS beyond UTM's latitude limits (84N / 80S)."""
    north = lat >= 0
    south_flag = "" if north else " +south"
    if lat > 84.0 or lat < -80.0:
        epsg = f" (EPSG:{32661 if north else 32761})" if ctx.is_wgs84 else ""
        label = f"UPS {'North' if north else 'South'}{epsg}"
        return _pipeline(f"+proj=ups{south_flag}", ctx.ellipsoid), label
    zone = utm_zone(lon, lat)
    epsg = f" (EPSG:{(32600 if north else 32700) + zone})" if ctx.is_wgs84 else ""
    label = f"UTM {zone}{'N' if north else 'S'}{epsg}"
    return _pipeline(f"+proj=utm +zone={zone}{south_flag}", ctx.ellipsoid), label


def looks_geographic(bounds: tuple[float, float, float, float]) -> bool:
    minx, miny, maxx, maxy = bounds
    return -180.0 <= minx <= maxx <= 180.0 and -90.0 <= miny <= maxy <= 90.0


def resolve_crs(
    file_crs: str | None,
    override: CRS | None,
    bounds: tuple[float, float, float, float] | None,
) -> tuple[CRS | None, CrsSource, list[Issue]]:
    """Decide which CRS a layer's coordinates are in."""
    if override is not None:
        issues = []
        if file_crs:
            issues.append(Issue("crs_overridden", f"File declares {file_crs}; using override."))
        return override, CrsSource.OVERRIDE, issues

    if file_crs:
        try:
            return CRS.from_user_input(file_crs), CrsSource.FILE, []
        except CRSError:
            reason = Issue("crs_unparseable", "The file's CRS definition could not be parsed.")
            return None, CrsSource.UNKNOWN, [reason]

    if bounds is not None and looks_geographic(bounds):
        return (
            WGS84,
            CrsSource.ASSUMED,
            [
                Issue(
                    "crs_assumed",
                    "No CRS in file; coordinates fit lon/lat ranges, assumed EPSG:4326.",
                )
            ],
        )
    return (
        None,
        CrsSource.UNKNOWN,
        [
            Issue(
                "crs_unknown",
                "No CRS in file and coordinates are not lon/lat; pass ?crs=EPSG:xxxx.",
            )
        ],
    )
