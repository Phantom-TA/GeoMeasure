"""Process one uploaded file end to end: unpack, read, measure, persist."""

from __future__ import annotations

import logging
import math
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import shapely
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError, ProjError
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from app.core.config import Settings
from app.db import repository
from app.db.session import get_session_factory
from app.geo.crs import WGS84, CrsContext, crs_label
from app.geo.geometry import has_nonfinite, transform
from app.geo.measure import MeasureOptions, measure_geometry
from app.geo.types import Feature, Issue, Layer, Measurement, Strategy
from app.ingest.errors import IngestError
from app.ingest.prepare import load_layers, prepare

log = logging.getLogger(__name__)

BATCH_SIZE = 1000


def upload_path(settings: Settings, file_id: str) -> Path:
    return settings.upload_dir / file_id / "upload.bin"


def process_file(file_id: str, settings: Settings) -> None:
    factory = get_session_factory(settings.resolved_database_url)
    with factory() as session:
        record = repository.claim(session, file_id, settings.max_attempts)
        if record is None:
            return
        filename, strategy, crs_override = record.filename, record.strategy, record.crs_override

    started = time.perf_counter()

    def elapsed_ms() -> int:
        return round((time.perf_counter() - started) * 1000)

    try:
        settings.work_dir.mkdir(parents=True, exist_ok=True)
        override = CRS.from_user_input(crs_override) if crs_override else None
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as work:
            source = prepare(
                upload_path(settings, file_id), filename, Path(work), settings.archive_limits
            )
            layers, issues = load_layers(source, override)

        options = MeasureOptions(
            strategy=Strategy(strategy), deviation_warn_pct=settings.geodesic_warn_pct
        )
        builder = ResultBuilder(file_id, layers, issues, options)
        with factory() as session:
            repository.complete(
                session,
                file_id,
                builder.batches(),
                lambda: builder.outcome(source.format.value, elapsed_ms()),
            )
        log.info("processed %s: %d features in %d ms", file_id, builder.count, elapsed_ms())
    except IngestError as exc:
        log.info("rejected %s: %s", file_id, exc.code)
        with factory() as session:
            repository.fail(session, file_id, exc.code, exc.message, elapsed_ms())
    except Exception:
        log.exception("unexpected error processing %s", file_id)
        with factory() as session:
            repository.fail(
                session,
                file_id,
                "internal_error",
                "Unexpected error while processing the file; see server logs.",
                elapsed_ms(),
            )


def _geojson(geom: BaseGeometry | None) -> dict[str, Any] | None:
    if geom is None or geom.is_empty or has_nonfinite(geom):
        return None
    return dict(mapping(geom))


@dataclass
class _LayerSetup:
    ctx: CrsContext | None
    label: str | None
    to_wgs84: Callable[[BaseGeometry], BaseGeometry | None]
    issues: list[Issue]


def _no_wgs84(_: BaseGeometry) -> None:
    return None


def _layer_setup(layer: Layer) -> _LayerSetup:
    if layer.crs is None:
        return _LayerSetup(None, None, _no_wgs84, [])
    label = crs_label(layer.crs)
    try:
        ctx = CrsContext.build(layer.crs)
        if layer.crs.equals(WGS84, ignore_axis_order=True):
            return _LayerSetup(ctx, label, shapely.force_2d, [])
        transformer = Transformer.from_crs(layer.crs, WGS84, always_xy=True)
    except (CRSError, ProjError) as exc:
        return _LayerSetup(None, label, _no_wgs84, [Issue("crs_unusable", f"Unusable CRS: {exc}")])

    def to_wgs84(geom: BaseGeometry) -> BaseGeometry | None:
        try:
            return transform(shapely.force_2d(geom), transformer)
        except (ProjError, ValueError):
            return None

    return _LayerSetup(ctx, label, to_wgs84, [])


class ResultBuilder:
    """Measures features lazily while they are written, and accumulates the file summary."""

    def __init__(
        self, file_id: str, layers: list[Layer], issues: list[Issue], options: MeasureOptions
    ) -> None:
        self.file_id = file_id
        self.layers = layers
        self.issues = issues
        self.options = options
        self.count = 0
        self._types: Counter[str] = Counter()
        self._statuses: Counter[str] = Counter()
        self._totals: dict[str, float] = {}
        self._max_deviation: float | None = None
        self._with_issues = 0
        self._bounds: tuple[float, float, float, float] | None = None
        self._layer_info: list[dict[str, Any]] = []

    def batches(self) -> Iterator[list[dict[str, Any]]]:
        batch: list[dict[str, Any]] = []
        for layer in self.layers:
            setup = _layer_setup(layer)
            self._layer_info.append(
                {
                    "name": layer.name,
                    "crs": setup.label,
                    "crs_source": layer.crs_source.value,
                    "feature_count": len(layer.features),
                    "issues": [i.as_dict() for i in [*layer.issues, *setup.issues]],
                }
            )
            for feature in layer.features:
                measurement = measure_geometry(feature.geometry, setup.ctx, self.options)
                batch.append(self._row(feature, measurement, setup))
                if len(batch) >= BATCH_SIZE:
                    yield batch
                    batch = []
        if batch:
            yield batch

    def _row(self, feature: Feature, m: Measurement, setup: _LayerSetup) -> dict[str, Any]:
        geom = feature.geometry
        wgs84 = setup.to_wgs84(geom) if geom is not None else None
        wgs84_json = _geojson(wgs84)
        issues = [i.as_dict() for i in [*feature.issues, *m.issues]]
        self._track(feature, m, wgs84 if wgs84_json else None, bool(issues))
        return {
            "file_id": self.file_id,
            "index": feature.index,
            "layer": feature.layer,
            "source_id": feature.source_id,
            "geometry_type": feature.geometry_type,
            "crs": setup.label,
            "has_z": bool(geom is not None and shapely.has_z(geom)),
            "geometry": _geojson(geom),
            "geometry_wgs84": wgs84_json,
            "properties": feature.properties,
            "status": m.status.value,
            "area_m2": m.area_m2,
            "perimeter_m": m.perimeter_m,
            "length_m": m.length_m,
            "geodesic_area_m2": m.geodesic_area_m2,
            "geodesic_perimeter_m": m.geodesic_perimeter_m,
            "geodesic_length_m": m.geodesic_length_m,
            "deviation_pct": m.deviation_pct,
            "method": m.method.value if m.method else None,
            "projections": m.projections,
            "issues": issues,
        }

    def _track(
        self, feature: Feature, m: Measurement, wgs84: BaseGeometry | None, has_issues: bool
    ) -> None:
        self.count += 1
        self._types[feature.geometry_type or "None"] += 1
        self._statuses[m.status.value] += 1
        self._with_issues += has_issues
        for key, value in (
            ("total_area_m2", m.area_m2),
            ("total_perimeter_m", m.perimeter_m),
            ("total_length_m", m.length_m),
            ("total_geodesic_area_m2", m.geodesic_area_m2),
            ("total_geodesic_length_m", m.geodesic_length_m),
        ):
            if value is not None:
                self._totals[key] = self._totals.get(key, 0.0) + value
        if m.deviation_pct is not None:
            self._max_deviation = max(self._max_deviation or 0.0, m.deviation_pct)
        if wgs84 is not None:
            b = wgs84.bounds
            if all(math.isfinite(v) for v in b):
                cur = self._bounds or b
                self._bounds = (
                    min(cur[0], b[0]),
                    min(cur[1], b[1]),
                    max(cur[2], b[2]),
                    max(cur[3], b[3]),
                )

    def outcome(self, fmt: str, processing_ms: int) -> repository.Outcome:
        labels = {info["crs"] for info in self._layer_info if info["crs"]}
        crs = labels.pop() if len(labels) == 1 else ("mixed" if labels else None)
        summary: dict[str, Any] = {
            "geometry_types": dict(self._types),
            "measurement_status": dict(self._statuses),
            "total_area_m2": self._totals.get("total_area_m2"),
            "total_perimeter_m": self._totals.get("total_perimeter_m"),
            "total_length_m": self._totals.get("total_length_m"),
            "total_geodesic_area_m2": self._totals.get("total_geodesic_area_m2"),
            "total_geodesic_length_m": self._totals.get("total_geodesic_length_m"),
            "max_deviation_pct": self._max_deviation,
            "features_with_issues": self._with_issues,
        }
        return repository.Outcome(
            format=fmt,
            crs=crs,
            feature_count=self.count,
            bbox=list(self._bounds) if self._bounds else None,
            layers=self._layer_info,
            summary=summary,
            issues=[i.as_dict() for i in self.issues],
            processing_ms=processing_ms,
        )
