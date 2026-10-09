from __future__ import annotations

import contextlib
import json
import re
import shutil
import uuid
from collections.abc import Iterator
from concurrent.futures import Future
from pathlib import PurePath
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Header, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pyproj import CRS
from pyproj.exceptions import CRSError
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app import __version__
from app.api.errors import PROBLEM_JSON, ApiError
from app.api.schemas import (
    FeatureOut,
    FeaturePage,
    FileList,
    FileOut,
    HealthOut,
    MeasurementOut,
    MeasurementPage,
)
from app.core.config import Settings
from app.db import repository
from app.db.models import FileRecord, FileStatus
from app.geo.types import MeasureStatus, Strategy
from app.ingest.errors import IngestError
from app.ingest.prepare import precheck
from app.ingest.upload import clean_filename, save_upload
from app.jobs.runner import JobRunner
from app.services.processing import upload_path

router = APIRouter()

_PREFER_WAIT = re.compile(r"(?:^|[,;\s])wait\s*=\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_runner(request: Request) -> JobRunner:
    runner: JobRunner = request.app.state.runner
    return runner


def get_factory(request: Request) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    return factory


def get_db(factory: Annotated[sessionmaker[Session], Depends(get_factory)]) -> Iterator[Session]:
    with factory() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings)]
RunnerDep = Annotated[JobRunner, Depends(get_runner)]
DbDep = Annotated[Session, Depends(get_db)]

_PROBLEM = {"content": {PROBLEM_JSON: {}}, "description": "Problem details (RFC 9457)"}


def _problem_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {code: _PROBLEM for code in codes}


# --- helpers ------------------------------------------------------------------------------


def _file_or_404(db: Session, file_id: str) -> FileRecord:
    record = repository.get_file(db, file_id)
    if record is None:
        raise ApiError(404, "file_not_found", f"No file with id '{file_id}'.")
    return record


def _completed_or_409(db: Session, file_id: str) -> FileRecord:
    record = _file_or_404(db, file_id)
    if record.status == FileStatus.FAILED:
        raise ApiError(
            409,
            "file_failed",
            f"Processing failed: {record.error_message}",
            file_status=record.status,
            error_code=record.error_code,
        )
    if record.status != FileStatus.COMPLETED:
        raise ApiError(
            409,
            "file_not_ready",
            f"File is still {record.status}; poll GET /api/files/{file_id} until COMPLETED.",
            file_status=record.status,
        )
    return record


def _prefer_wait(prefer: str | None, cap: float) -> float:
    if not prefer:
        return 0.0
    match = _PREFER_WAIT.search(prefer)
    return min(float(match.group(1)), cap) if match else 0.0


def _await_job(
    db: Session,
    record: FileRecord,
    future: Future[None],
    response: Response,
    prefer: str | None,
    cap: float,
) -> FileOut:
    wait = _prefer_wait(prefer, cap)
    if wait:
        # a timeout or a worker crash just means "not done yet"; the database has the truth
        with contextlib.suppress(Exception):
            future.result(timeout=wait)
        db.refresh(record)
        response.headers["Preference-Applied"] = f"wait={wait:g}"
    response.headers["Location"] = f"/api/files/{record.id}"
    if record.status in (FileStatus.COMPLETED, FileStatus.FAILED):
        response.status_code = 201
    return FileOut.from_record(record)


def _parse_crs(value: str | None) -> str | None:
    if not value:
        return None
    try:
        crs = CRS.from_user_input(value.strip())
    except CRSError as exc:
        raise ApiError(422, "invalid_crs", f"Could not interpret CRS '{value}'.") from exc
    if not (crs.is_geographic or crs.is_projected):
        raise ApiError(422, "invalid_crs", "CRS must be geographic or projected.")
    return str(crs.to_string())


# --- endpoints ----------------------------------------------------------------------------


@router.post(
    "/files",
    status_code=202,
    response_model=FileOut,
    summary="Upload a Shapefile (.zip), KML or KMZ for processing",
    description=(
        "Returns **202** immediately and processes in the background; poll the file until "
        "`status` is `COMPLETED`. Send `Prefer: wait=10` to wait up to 10 s for the result, "
        "in which case a finished file is returned with **201**."
    ),
    responses={
        201: {"model": FileOut, "description": "Processed within the requested wait"},
        **_problem_responses(400, 413, 415, 422),
    },
)
def upload_file(
    response: Response,
    db: DbDep,
    settings: SettingsDep,
    runner: RunnerDep,
    file: Annotated[UploadFile, File(description="A .kml, .kmz, or .zip containing a Shapefile.")],
    crs: Annotated[
        str | None,
        Query(description="Use this CRS instead of / in absence of the file's, e.g. EPSG:32643."),
    ] = None,
    strategy: Annotated[
        Strategy | None,
        Query(description="auto (default): native CRS if suitable, else local projections."),
    ] = None,
    prefer: Annotated[str | None, Header(description="e.g. `wait=10`")] = None,
) -> FileOut:
    crs_override = _parse_crs(crs)
    filename = clean_filename(file.filename)
    file_id = uuid.uuid4().hex
    dest = upload_path(settings, file_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        size, sha256 = save_upload(file.file, dest, settings.max_upload_bytes)
        precheck(dest, filename, settings.archive_limits)
    except IngestError:
        shutil.rmtree(dest.parent, ignore_errors=True)
        raise

    record = repository.create_file(
        db,
        id=file_id,
        filename=filename,
        size_bytes=size,
        sha256=sha256,
        strategy=(strategy or settings.default_strategy).value,
        crs_override=crs_override,
    )
    future = runner.submit(file_id)
    return _await_job(db, record, future, response, prefer, settings.max_wait_seconds)


@router.get("/files", response_model=FileList, summary="List uploaded files")
def list_files(
    db: DbDep,
    status: Annotated[FileStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FileList:
    total, rows = repository.list_files(
        db, status=status.value if status else None, limit=limit, offset=offset
    )
    return FileList(
        total=total, limit=limit, offset=offset, items=[FileOut.from_record(r) for r in rows]
    )


@router.get(
    "/files/{file_id}",
    response_model=FileOut,
    summary="File information and processing status",
    responses=_problem_responses(404),
)
def get_file(file_id: str, db: DbDep) -> FileOut:
    return FileOut.from_record(_file_or_404(db, file_id))


@router.get(
    "/files/{file_id}/measurements",
    response_model=MeasurementPage,
    summary="Per-feature measurements",
    responses=_problem_responses(404, 409),
)
def get_measurements(
    file_id: str,
    db: DbDep,
    geometry_type: Annotated[str | None, Query(examples=["Polygon"])] = None,
    status: Annotated[MeasureStatus | None, Query()] = None,
    layer: Annotated[str | None, Query()] = None,
    has_issues: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MeasurementPage:
    record = _completed_or_409(db, file_id)
    flt = repository.FeatureFilter(
        geometry_type=geometry_type,
        status=status.value if status else None,
        layer=layer,
        has_issues=has_issues,
    )
    total, rows = repository.query_features(db, file_id, flt, limit=limit, offset=offset)
    file_out = FileOut.from_record(record)
    return MeasurementPage(
        file_id=file_id,
        total=total,
        limit=limit,
        offset=offset,
        summary=file_out.summary,
        items=[MeasurementOut.from_record(r) for r in rows],
    )


@router.get(
    "/files/{file_id}/features",
    response_model=FeaturePage,
    summary="Features with geometry, CRS, properties and measurements",
    responses=_problem_responses(404, 409),
)
def get_features(
    file_id: str,
    db: DbDep,
    geometry_crs: Annotated[
        Literal["source", "wgs84"],
        Query(description="Return geometry in the file's own CRS or in EPSG:4326."),
    ] = "source",
    geometry_type: Annotated[str | None, Query()] = None,
    status: Annotated[MeasureStatus | None, Query()] = None,
    layer: Annotated[str | None, Query()] = None,
    has_issues: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FeaturePage:
    _completed_or_409(db, file_id)
    flt = repository.FeatureFilter(
        geometry_type=geometry_type,
        status=status.value if status else None,
        layer=layer,
        has_issues=has_issues,
    )
    total, rows = repository.query_features(db, file_id, flt, limit=limit, offset=offset)
    wgs84 = geometry_crs == "wgs84"
    return FeaturePage(
        file_id=file_id,
        total=total,
        limit=limit,
        offset=offset,
        items=[FeatureOut.from_record(r, wgs84) for r in rows],
    )


@router.get(
    "/files/{file_id}/geojson",
    summary="Download all features with measurements as GeoJSON (RFC 7946, WGS84)",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"application/geo+json": {}}},
        **_problem_responses(404, 409),
    },
)
def export_geojson(
    file_id: str, db: DbDep, factory: Annotated[sessionmaker[Session], Depends(get_factory)]
) -> StreamingResponse:
    record = _completed_or_409(db, file_id)
    stem = PurePath(record.filename).stem or "features"
    return StreamingResponse(
        _geojson_stream(factory, file_id, stem),
        media_type="application/geo+json",
        headers={"Content-Disposition": f'inline; filename="{stem}.geojson"'},
    )


def _geojson_stream(factory: sessionmaker[Session], file_id: str, name: str) -> Iterator[bytes]:
    yield b'{"type":"FeatureCollection","name":' + json.dumps(name).encode() + b',"features":['
    first = True
    with factory() as session:
        for r in repository.iter_features(session, file_id):
            properties = dict(r.properties)
            properties.update(
                {
                    "_index": r.index,
                    "_layer": r.layer,
                    "_geometry_type": r.geometry_type,
                    "_status": r.status,
                    "_area_m2": r.area_m2,
                    "_perimeter_m": r.perimeter_m,
                    "_length_m": r.length_m,
                    "_geodesic_area_m2": r.geodesic_area_m2,
                    "_geodesic_length_m": r.geodesic_length_m,
                    "_deviation_pct": r.deviation_pct,
                    "_method": r.method,
                    "_issues": [i["code"] for i in r.issues or []],
                }
            )
            # geometry is stored as GeoJSON text, so it is spliced in without re-parsing
            feature = (
                f'{{"type":"Feature","id":{r.index},"geometry":{r.geometry_wgs84 or "null"},'
                f'"properties":{json.dumps(properties)}}}'
            )
            yield (b"" if first else b",") + feature.encode()
            first = False
    yield b"]}"


@router.post(
    "/files/{file_id}/reprocess",
    status_code=202,
    response_model=FileOut,
    summary="Re-measure a file with a different strategy, without re-uploading",
    responses={201: {"model": FileOut}, **_problem_responses(404, 409)},
)
def reprocess_file(
    file_id: str,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
    runner: RunnerDep,
    strategy: Annotated[Strategy | None, Query()] = None,
    prefer: Annotated[str | None, Header()] = None,
) -> FileOut:
    record = _file_or_404(db, file_id)
    new_strategy = (strategy or Strategy(record.strategy)).value
    if not repository.reset_for_reprocess(db, file_id, new_strategy):
        raise ApiError(409, "file_busy", "File is already queued or processing.")
    db.refresh(record)
    future = runner.submit(file_id)
    return _await_job(db, record, future, response, prefer, settings.max_wait_seconds)


@router.delete(
    "/files/{file_id}",
    status_code=204,
    summary="Delete a file, its features and the stored upload",
    responses=_problem_responses(404, 409),
)
def delete_file(file_id: str, db: DbDep, settings: SettingsDep) -> Response:
    record = _file_or_404(db, file_id)
    if record.status in (FileStatus.PENDING, FileStatus.PROCESSING):
        raise ApiError(409, "file_busy", "File is still being processed; try again shortly.")
    repository.delete_file(db, file_id)
    shutil.rmtree(upload_path(settings, file_id).parent, ignore_errors=True)
    return Response(status_code=204)


health_router = APIRouter()


@health_router.get("/health", response_model=HealthOut, tags=["meta"])
def health(db: DbDep, settings: SettingsDep) -> HealthOut:
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "unavailable"
    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        version=__version__,
        database=database,
        worker_mode=settings.worker_mode,
        workers=settings.workers,
    )
