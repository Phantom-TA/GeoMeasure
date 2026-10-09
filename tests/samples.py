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
