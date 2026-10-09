from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pyproj import CRS
from shapely.geometry.base import BaseGeometry


class Strategy(StrEnum):
    AUTO = "auto"
    LOCAL = "local"
    UTM = "utm"


class Method(StrEnum):
    SOURCE = "source"
    LOCAL = "local"
    UTM = "utm"


class MeasureStatus(StrEnum):
    OK = "ok"
    NOT_APPLICABLE = "not_applicable"
    SKIPPED = "skipped"
    ERROR = "error"


class CrsSource(StrEnum):
    FILE = "file"
    OVERRIDE = "override"
    ASSUMED = "assumed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Issue:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass
class Feature:
    index: int
    layer: str
    source_id: str | None
    geometry: BaseGeometry | None
    properties: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)

    @property
    def geometry_type(self) -> str | None:
        return None if self.geometry is None else self.geometry.geom_type


@dataclass
class Layer:
    name: str
    crs: CRS | None
    crs_source: CrsSource
    features: list[Feature]
    issues: list[Issue] = field(default_factory=list)


@dataclass
class Measurement:
    status: MeasureStatus
    area_m2: float | None = None
    perimeter_m: float | None = None
    length_m: float | None = None
    geodesic_area_m2: float | None = None
    geodesic_perimeter_m: float | None = None
    geodesic_length_m: float | None = None
    deviation_pct: float | None = None
    method: Method | None = None
    projections: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
