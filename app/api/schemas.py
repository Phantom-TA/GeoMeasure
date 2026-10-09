from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.db.models import FeatureRecord, FileRecord


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _m(value: float | None) -> float | None:
    """Metres / square metres to millimetre precision."""
    return None if value is None else round(value, 3)


def _sig(value: float | None, digits: int = 4) -> float | None:
    if value is None or value == 0 or not math.isfinite(value):
        return value
    return float(f"{value:.{digits}g}")


class IssueOut(BaseModel):
    code: str = Field(examples=["geometry_repaired"])
    message: str


class ErrorOut(BaseModel):
    code: str
    message: str


class LayerOut(BaseModel):
    name: str
    crs: str | None = Field(examples=["EPSG:4326"])
    crs_source: str = Field(description="file | override | assumed | unknown")
    feature_count: int
    issues: list[IssueOut]


class SummaryOut(BaseModel):
    geometry_types: dict[str, int] = Field(examples=[{"Polygon": 118, "Point": 2}])
    measurement_status: dict[str, int] = Field(examples=[{"ok": 118, "not_applicable": 2}])
    total_area_m2: float | None
    total_perimeter_m: float | None
    total_length_m: float | None
    total_geodesic_area_m2: float | None
    total_geodesic_length_m: float | None
    max_deviation_pct: float | None = Field(
        description="Largest projected-vs-geodesic difference of any feature, in percent."
    )
    features_with_issues: int


class FileOut(BaseModel):
    id: str = Field(examples=["3f2b8c1d9a7e4b6f8e2d1c0b9a8f7e6d"])
    filename: str = Field(examples=["survey.kml"])
    status: str = Field(
        examples=["COMPLETED"], description="PENDING | PROCESSING | COMPLETED | FAILED"
    )
    format: str | None = Field(examples=["kml"], description="kml | kmz | shapefile")
    feature_count: int | None = Field(examples=[120])
    crs: str | None = Field(
        examples=["EPSG:4326"], description="CRS of the data; 'mixed' if layers differ."
    )
    strategy: str = Field(description="Measurement strategy: auto | local | utm")
    crs_override: str | None
    size_bytes: int
    sha256: str
    bbox: list[float] | None = Field(description="[min_lon, min_lat, max_lon, max_lat] (WGS84)")
    layers: list[LayerOut] | None
    summary: SummaryOut | None
    issues: list[IssueOut] | None
    error: ErrorOut | None
    attempts: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    processing_ms: int | None
    links: dict[str, str]

    @classmethod
    def from_record(cls, r: FileRecord) -> FileOut:
        base = f"/api/files/{r.id}"
        summary = None
        if r.summary:
            summary = SummaryOut(
                **{
                    **r.summary,
                    **{k: _m(v) for k, v in r.summary.items() if k.startswith("total_")},
                    "max_deviation_pct": _sig(r.summary.get("max_deviation_pct")),
                }
            )
        return cls(
            id=r.id,
            filename=r.filename,
            status=r.status,
            format=r.format,
            feature_count=r.feature_count,
            crs=r.crs,
            strategy=r.strategy,
            crs_override=r.crs_override,
            size_bytes=r.size_bytes,
            sha256=r.sha256,
            bbox=r.bbox,
            layers=[LayerOut(**layer) for layer in r.layers] if r.layers is not None else None,
            summary=summary,
            issues=[IssueOut(**i) for i in r.issues] if r.issues is not None else None,
            error=ErrorOut(code=r.error_code, message=r.error_message or "")
            if r.error_code
            else None,
            attempts=r.attempts,
            created_at=_utc(r.created_at) or r.created_at,
            started_at=_utc(r.started_at),
            completed_at=_utc(r.completed_at),
            processing_ms=r.processing_ms,
            links={
                "self": base,
                "measurements": f"{base}/measurements",
                "features": f"{base}/features",
                "geojson": f"{base}/geojson",
            },
        )


class FileList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[FileOut]


class GeodesicOut(BaseModel):
    area_m2: float | None
    perimeter_m: float | None
    length_m: float | None


class MeasurementOut(BaseModel):
    index: int = Field(description="Position of the feature in the file (0-based, across layers).")
    source_id: str | None = Field(description="ID in the source file (Shapefile FID / KML id).")
    layer: str
    geometry_type: str | None = Field(examples=["Polygon"])
    crs: str | None
    status: str = Field(description="ok | not_applicable | skipped | error")
    area_m2: float | None
    perimeter_m: float | None
    length_m: float | None
    geodesic: GeodesicOut = Field(
        description="Independent check computed on the ellipsoid, without any projection."
    )
    deviation_pct: float | None = Field(
        description="Largest relative difference between projected and geodesic values."
    )
    method: str | None = Field(description="source | local | utm")
    projections: list[str] = Field(
        examples=[["LAEA(lat_0=28, lon_0=77)", "AEQD(lat_0=28, lon_0=77)"]]
    )
    issues: list[IssueOut]

    @classmethod
    def from_record(cls, r: FeatureRecord) -> MeasurementOut:
        return cls(
            index=r.index,
            source_id=r.source_id,
            layer=r.layer,
            geometry_type=r.geometry_type,
            crs=r.crs,
            status=r.status,
            area_m2=_m(r.area_m2),
            perimeter_m=_m(r.perimeter_m),
            length_m=_m(r.length_m),
            geodesic=GeodesicOut(
                area_m2=_m(r.geodesic_area_m2),
                perimeter_m=_m(r.geodesic_perimeter_m),
                length_m=_m(r.geodesic_length_m),
            ),
            deviation_pct=_sig(r.deviation_pct),
            method=r.method,
            projections=r.projections or [],
            issues=[IssueOut(**i) for i in r.issues or []],
        )


class MeasurementPage(BaseModel):
    file_id: str
    total: int
    limit: int
    offset: int
    summary: SummaryOut | None
    items: list[MeasurementOut]


class FeatureOut(BaseModel):
    index: int
    source_id: str | None
    layer: str
    geometry_type: str | None
    crs: str | None = Field(
        description="CRS of `geometry` (the source CRS, or EPSG:4326 if requested)."
    )
    has_z: bool
    geometry: dict[str, Any] | None = Field(description="GeoJSON geometry object.")
    properties: dict[str, Any]
    measurement: MeasurementOut

    @classmethod
    def from_record(cls, r: FeatureRecord, wgs84: bool) -> FeatureOut:
        return cls(
            index=r.index,
            source_id=r.source_id,
            layer=r.layer,
            geometry_type=r.geometry_type,
            crs="EPSG:4326" if wgs84 and r.geometry_wgs84 else r.crs,
            has_z=r.has_z,
            geometry=r.geometry_wgs84 if wgs84 else r.geometry,
            properties=r.properties,
            measurement=MeasurementOut.from_record(r),
        )


class FeaturePage(BaseModel):
    file_id: str
    total: int
    limit: int
    offset: int
    items: list[FeatureOut]


class HealthOut(BaseModel):
    status: str
    version: str
    database: str
    worker_mode: str
    workers: int
