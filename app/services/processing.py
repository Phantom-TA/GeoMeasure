"""Process one uploaded file end to end: unpack, read, measure, persist."""

from __future__ import annotations

import logging
import tempfile
import time
import warnings
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError, ProjError

from app.core.config import Settings
from app.db import repository
from app.db.session import get_session_factory
from app.geo.crs import WGS84, CrsContext, CrsKind, crs_label
from app.geo.geometry import transform
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


# --- vectorised helpers: one native call per batch instead of one per feature ------------


def _project(geoms: np.ndarray, transformer: Transformer) -> np.ndarray:
    return np.asarray(transform(geoms, transformer), dtype=object)


def _drop_unrenderable(geoms: np.ndarray) -> np.ndarray:
    """None for missing, empty, or non-finite geometries (they have no valid GeoJSON)."""
    out = geoms.copy()
    out[shapely.is_empty(out)] = None
    coords, idx = shapely.get_coordinates(out, include_z=True, return_index=True)
    # 2D geometries report z as NaN, so only check z where the geometry really has one
    bad_xy = ~np.isfinite(coords[:, :2]).all(axis=1)
    bad_z = shapely.has_z(out)[idx] & ~np.isfinite(coords[:, 2])
    out[np.unique(idx[bad_xy | bad_z])] = None
    return out


def _geojson(geoms: np.ndarray) -> list[str | None]:
    return list(shapely.to_geojson(_drop_unrenderable(geoms)))


@dataclass
class _LayerSetup:
    ctx: CrsContext | None
    label: str | None
    issues: list[Issue]
    to_wgs84: Transformer | None = None

    def lonlat(self, flat: np.ndarray) -> np.ndarray | None:
        """Lon/lat geometries on the layer's datum (None if the CRS is unusable)."""
        if self.ctx is None:
            return None
        if self.ctx.to_lonlat is None:
            return flat
        return _project(flat, self.ctx.to_lonlat)

    def wgs84(self, flat: np.ndarray, lonlat: np.ndarray | None) -> np.ndarray | None:
        if self.ctx is None or lonlat is None:
            return None
        if self.to_wgs84 is None:  # the layer's datum is WGS84, so lon/lat already is
            return lonlat
        return _project(flat, self.to_wgs84)


def _layer_setup(layer: Layer) -> _LayerSetup:
    if layer.crs is None:
        return _LayerSetup(None, None, [])
    label = crs_label(layer.crs)
    try:
        ctx = CrsContext.build(layer.crs)
        to_wgs84 = None
        if not ctx.is_wgs84:
            to_wgs84 = Transformer.from_crs(layer.crs, WGS84, always_xy=True)
    except (CRSError, ProjError) as exc:
        return _LayerSetup(None, label, [Issue("crs_unusable", f"Unusable CRS: {exc}")])
    return _LayerSetup(ctx, label, [], to_wgs84)


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
        self._bounds: np.ndarray | None = None
        self._layer_info: list[dict[str, Any]] = []

    def batches(self) -> Iterator[list[dict[str, Any]]]:
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
            for start in range(0, len(layer.features), BATCH_SIZE):
                yield self._batch(layer.features[start : start + BATCH_SIZE], setup)

    def _batch(self, features: list[Feature], setup: _LayerSetup) -> list[dict[str, Any]]:
        geoms = np.empty(len(features), dtype=object)
        geoms[:] = [f.geometry for f in features]
        flat = shapely.force_2d(geoms)
        lonlat = setup.lonlat(flat)
        wgs84 = setup.wgs84(flat, lonlat)
        source_json = _geojson(geoms)
        wgs84_json = _geojson(wgs84) if wgs84 is not None else [None] * len(features)
        has_z = shapely.has_z(geoms)
        if wgs84 is not None:
            self._track_bounds(_drop_unrenderable(wgs84))
        # Hand the pre-projected lon/lat to the engine only for projected layers; for
        # geographic layers it would be the same geometry.
        hints = (
            lonlat if setup.ctx is not None and setup.ctx.kind is not CrsKind.GEOGRAPHIC else None
        )

        rows = []
        for i, feature in enumerate(features):
            m = measure_geometry(
                feature.geometry,
                setup.ctx,
                self.options,
                lonlat=hints[i] if hints is not None else None,
            )
            issues = [x.as_dict() for x in [*feature.issues, *m.issues]]
            self._track(feature, m, bool(issues))
            rows.append(
                {
                    "file_id": self.file_id,
                    "index": feature.index,
                    "layer": feature.layer,
                    "source_id": feature.source_id,
                    "geometry_type": feature.geometry_type,
                    "crs": setup.label,
                    "has_z": bool(has_z[i]),
                    "geometry": source_json[i],
                    "geometry_wgs84": wgs84_json[i],
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
            )
        return rows

    def _track(self, feature: Feature, m: Measurement, has_issues: bool) -> None:
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

    def _track_bounds(self, geoms: np.ndarray) -> None:
        b = shapely.bounds(geoms)
        if np.isnan(b).all():
            return
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            batch = np.array(
                [np.nanmin(b[:, 0]), np.nanmin(b[:, 1]), np.nanmax(b[:, 2]), np.nanmax(b[:, 3])]
            )
        if self._bounds is None:
            self._bounds = batch
        else:
            self._bounds = np.concatenate(
                [np.minimum(self._bounds[:2], batch[:2]), np.maximum(self._bounds[2:], batch[2:])]
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
            bbox=[float(v) for v in self._bounds] if self._bounds is not None else None,
            layers=self._layer_info,
            summary=summary,
            issues=[i.as_dict() for i in self.issues],
            processing_ms=processing_ms,
        )
