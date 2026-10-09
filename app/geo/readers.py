"""Read Shapefile and KML layers into Feature objects."""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyogrio
import shapely
from pyogrio import raw
from pyogrio.errors import DataLayerError, DataSourceError
from pyproj import CRS

from app.geo.crs import resolve_crs
from app.geo.types import Feature, Issue, Layer

# GDAL can rebuild a missing .shx index from the .shp itself.
pyogrio.set_gdal_config_options({"SHAPE_RESTORE_SHX": "YES"})

# Fields the LIBKML driver adds to every placemark; dropped when they hold their default.
_KML_DEFAULTS: dict[str, Any] = {
    "id": None,
    "description": None,
    "timestamp": None,
    "begin": None,
    "end": None,
    "altitudeMode": None,
    "tessellate": -1,
    "extrude": 0,
    "visibility": -1,
    "drawOrder": None,
    "icon": None,
}


class ReadError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ShapefileSource:
    name: str
    path: Path
    issues: tuple[Issue, ...] = ()


def clean_value(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def read_shapefile(
    source: ShapefileSource, *, start_index: int = 0, crs_override: CRS | None = None
) -> Layer:
    return _read_layer(
        str(source.path),
        layer=None,
        name=source.name,
        is_kml=False,
        start_index=start_index,
        crs_override=crs_override,
        issues=list(source.issues),
    )


def read_kml(path: Path, *, start_index: int = 0, crs_override: CRS | None = None) -> list[Layer]:
    try:
        layer_names = [str(row[0]) for row in pyogrio.list_layers(str(path))]
    except DataSourceError as exc:
        raise ReadError("unreadable_kml", f"Could not parse KML: {exc}") from exc

    layers: list[Layer] = []
    index = start_index
    for name in layer_names:
        layer = _read_layer(
            str(path),
            layer=name,
            name=name,
            is_kml=True,
            start_index=index,
            crs_override=crs_override,
            issues=[],
        )
        index += len(layer.features)
        layers.append(layer)
    return layers


def _read_layer(
    path: str,
    *,
    layer: str | None,
    name: str,
    is_kml: bool,
    start_index: int,
    crs_override: CRS | None,
    issues: list[Issue],
) -> Layer:
    try:
        meta, fids, wkb, field_data = raw.read(
            path, layer=layer, return_fids=True, datetime_as_string=True
        )
    except (DataSourceError, DataLayerError) as exc:
        raise ReadError("unreadable_layer", f"Could not read layer '{name}': {exc}") from exc

    geometries = shapely.from_wkb(wkb, on_invalid="ignore") if wkb is not None else None
    fields = [str(f) for f in meta["fields"]]
    count = len(fids) if fids is not None else 0

    bounds = None
    if geometries is not None and count:
        tb = shapely.total_bounds(geometries)
        if np.isfinite(tb).all():
            bounds = (float(tb[0]), float(tb[1]), float(tb[2]), float(tb[3]))
    crs, crs_source, crs_issues = resolve_crs(meta.get("crs"), crs_override, bounds)

    features: list[Feature] = []
    for i in range(count):
        props = {f: clean_value(col[i]) for f, col in zip(fields, field_data, strict=True)}
        feature_issues: list[Issue] = []
        source_id: str | None = str(fids[i])
        if is_kml:
            source_id = props.get("id")
            props = {
                k: v for k, v in props.items() if not (k in _KML_DEFAULTS and v == _KML_DEFAULTS[k])
            }
            props.pop("id", None)

        geom = geometries[i] if geometries is not None else None
        if geom is None and wkb is not None and wkb[i] is not None:
            feature_issues.append(
                Issue("unsupported_geometry", "Geometry could not be decoded (unsupported type).")
            )
        features.append(
            Feature(
                index=start_index + i,
                layer=name,
                source_id=source_id,
                geometry=geom,
                properties=props,
                issues=feature_issues,
            )
        )

    return Layer(
        name=name, crs=crs, crs_source=crs_source, features=features, issues=[*issues, *crs_issues]
    )
