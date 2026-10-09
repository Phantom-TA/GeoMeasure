"""Builders for sample geospatial files used across the test suite."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import shapely
from pyogrio import raw
from shapely.geometry.base import BaseGeometry

FIELDS_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document><name>Survey</name>
  <Folder><name>Fields</name>
    <Placemark id="field-1"><name>North field</name>
      <ExtendedData><Data name="crop"><value>wheat</value></Data></ExtendedData>
      <Polygon><outerBoundaryIs><LinearRing><coordinates>
        77.0,28.0,0 77.01,28.0,0 77.01,28.01,0 77.0,28.01,0 77.0,28.0,0
      </coordinates></LinearRing></outerBoundaryIs></Polygon>
    </Placemark>
  </Folder>
  <Folder><name>Infrastructure</name>
    <Placemark><name>Road</name>
      <LineString><coordinates>77.0,28.0 77.1,28.0</coordinates></LineString>
    </Placemark>
    <Placemark><name>Tower</name><Point><coordinates>77.05,28.05</coordinates></Point></Placemark>
    <Placemark><name>Mixed</name>
      <MultiGeometry>
        <Point><coordinates>77.0,28.0</coordinates></Point>
        <LineString><coordinates>77.0,28.0 77.0,28.1</coordinates></LineString>
      </MultiGeometry>
    </Placemark>
  </Folder>
</Document>
</kml>
"""


# Typical Google Earth export: styles, typed SchemaData, a gx:Track, and a NetworkLink.
GOOGLE_EARTH_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
<Document>
  <name>Site survey.kmz</name>
  <Style id="s1"><LineStyle><color>ff0000ff</color></LineStyle></Style>
  <Schema name="plot" id="plotSchema">
    <SimpleField type="int" name="plot_no"/><SimpleField type="double" name="area_ha"/>
  </Schema>
  <Placemark>
    <name>Plot 7</name><styleUrl>#s1</styleUrl>
    <ExtendedData><SchemaData schemaUrl="#plotSchema">
      <SimpleData name="plot_no">7</SimpleData><SimpleData name="area_ha">1.25</SimpleData>
    </SchemaData></ExtendedData>
    <Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing><coordinates>
      77.0,28.0,0 77.01,28.0,0 77.01,28.01,0 77.0,28.01,0 77.0,28.0,0
    </coordinates></LinearRing></outerBoundaryIs></Polygon>
  </Placemark>
  <Placemark><name>Drone flight</name>
    <gx:Track>
      <when>2024-05-01T10:00:00Z</when>
      <when>2024-05-01T10:01:00Z</when>
      <when>2024-05-01T10:02:00Z</when>
      <gx:coord>77.0 28.0 120</gx:coord>
      <gx:coord>77.01 28.0 120</gx:coord>
      <gx:coord>77.02 28.0 125</gx:coord>
    </gx:Track>
  </Placemark>
  <NetworkLink><name>remote</name><Link><href>http://example.com/x.kml</href></Link></NetworkLink>
</Document></kml>
"""


def kml_document(*placemarks: str) -> bytes:
    body = "".join(placemarks)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{body}</Document></kml>'
    ).encode()


def kml_polygon(coords: Sequence[tuple[float, ...]], name: str = "poly") -> str:
    ring = " ".join(",".join(str(c) for c in pt) for pt in [*coords, coords[0]])
    return (
        f"<Placemark><name>{name}</name><Polygon><outerBoundaryIs><LinearRing>"
        f"<coordinates>{ring}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
    )


def kml_line(coords: Sequence[tuple[float, ...]], name: str = "line") -> str:
    pts = " ".join(",".join(str(c) for c in pt) for pt in coords)
    return (
        f"<Placemark><name>{name}</name>"
        f"<LineString><coordinates>{pts}</coordinates></LineString></Placemark>"
    )


def kml_point(lon: float, lat: float, name: str = "pt") -> str:
    return (
        f"<Placemark><name>{name}</name>"
        f"<Point><coordinates>{lon},{lat}</coordinates></Point></Placemark>"
    )


def write_shapefile(
    directory: Path,
    name: str,
    geometries: Sequence[BaseGeometry],
    *,
    crs: str | None = "EPSG:4326",
    fields: dict[str, list[object]] | None = None,
    geometry_type: str = "Polygon",
    encoding: str | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fields = fields or {"name": [f"f{i}" for i in range(len(geometries))]}
    path = directory / f"{name}.shp"
    raw.write(
        str(path),
        geometry=shapely.to_wkb(np.array(geometries, dtype=object)),
        field_data=[_column(v) for v in fields.values()],
        fields=list(fields.keys()),
        crs=crs,
        geometry_type=geometry_type,
        driver="ESRI Shapefile",
        encoding=encoding,
    )
    return path


def _column(values: list[object]) -> np.ndarray:
    present = [v for v in values if v is not None]
    if present and all(isinstance(v, int | float) for v in present):
        return np.array([np.nan if v is None else v for v in values], dtype=float)
    return np.array(values, dtype=object)


def shapefile_members(shp: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in shp.parent.glob(f"{shp.stem}.*")}


def make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in members.items():
            zf.writestr(arcname, data)
    return buf.getvalue()
