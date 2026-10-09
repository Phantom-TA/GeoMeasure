"""All database access goes through here."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, Result, delete, func, insert, or_, select, update
from sqlalchemy.orm import Session

from app.db.models import FeatureRecord, FileRecord, FileStatus, utcnow

ACTIVE = (FileStatus.PENDING, FileStatus.PROCESSING)


def _rowcount(result: Result[Any]) -> int:
    return cast(CursorResult[Any], result).rowcount


def create_file(session: Session, **fields: Any) -> FileRecord:
    record = FileRecord(status=FileStatus.PENDING, attempts=0, **fields)
    session.add(record)
    session.commit()
    return record


def get_file(session: Session, file_id: str) -> FileRecord | None:
    return session.get(FileRecord, file_id)


def list_files(
    session: Session, *, status: str | None, limit: int, offset: int
) -> tuple[int, Sequence[FileRecord]]:
    query = select(FileRecord)
    if status:
        query = query.where(FileRecord.status == status)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.scalars(
        query.order_by(FileRecord.created_at.desc(), FileRecord.id).limit(limit).offset(offset)
    ).all()
    return total, rows


def active_file_ids(session: Session) -> list[str]:
    query = select(FileRecord.id).where(FileRecord.status.in_(ACTIVE))
    return list(session.scalars(query.order_by(FileRecord.created_at)))


def claim(session: Session, file_id: str, max_attempts: int) -> FileRecord | None:
    """Atomically take ownership of a job. Returns None if there is nothing to do.

    A job left PROCESSING by a crashed worker can be claimed again; after max_attempts
    it is failed so a file that keeps crashing workers cannot loop forever.
    """
    claimed = _rowcount(
        session.execute(
            update(FileRecord)
            .where(
                FileRecord.id == file_id,
                FileRecord.status.in_(ACTIVE),
                FileRecord.attempts < max_attempts,
            )
            .values(
                status=FileStatus.PROCESSING,
                attempts=FileRecord.attempts + 1,
                started_at=utcnow(),
            )
        )
    )
    session.commit()
    if claimed:
        return session.get(FileRecord, file_id)

    session.execute(
        update(FileRecord)
        .where(FileRecord.id == file_id, FileRecord.status.in_(ACTIVE))
        .values(
            status=FileStatus.FAILED,
            error_code="processing_crashed",
            error_message=f"Processing crashed {max_attempts} times; giving up on this file.",
            completed_at=utcnow(),
        )
    )
    session.commit()
    return None


@dataclass
class Outcome:
    format: str
    crs: str | None
    feature_count: int
    bbox: list[float] | None
    layers: list[dict[str, Any]]
    summary: dict[str, Any]
    issues: list[dict[str, str]]
    processing_ms: int


def complete(
    session: Session,
    file_id: str,
    features: Iterable[list[dict[str, Any]]],
    outcome_fn: Callable[[], Outcome],
) -> None:
    """Replace the file's features and mark it COMPLETED in a single transaction.

    Feature batches are produced lazily, so the summary is only known once they have all
    been written; hence outcome_fn.
    """
    session.execute(delete(FeatureRecord).where(FeatureRecord.file_id == file_id))
    for batch in features:
        if batch:
            session.execute(insert(FeatureRecord), batch)
    outcome = outcome_fn()
    session.execute(
        update(FileRecord)
        .where(FileRecord.id == file_id)
        .values(
            status=FileStatus.COMPLETED,
            format=outcome.format,
            crs=outcome.crs,
            feature_count=outcome.feature_count,
            bbox=outcome.bbox,
            layers=outcome.layers,
            summary=outcome.summary,
            issues=outcome.issues,
            error_code=None,
            error_message=None,
            completed_at=utcnow(),
            processing_ms=outcome.processing_ms,
        )
    )
    session.commit()


def fail(session: Session, file_id: str, code: str, message: str, processing_ms: int) -> None:
    session.execute(
        update(FileRecord)
        .where(FileRecord.id == file_id)
        .values(
            status=FileStatus.FAILED,
            error_code=code,
            error_message=message,
            completed_at=utcnow(),
            processing_ms=processing_ms,
        )
    )
    session.commit()


def reset_for_reprocess(session: Session, file_id: str, strategy: str) -> bool:
    updated = _rowcount(
        session.execute(
            update(FileRecord)
            .where(FileRecord.id == file_id, FileRecord.status.not_in(ACTIVE))
            .values(status=FileStatus.PENDING, strategy=strategy, attempts=0)
        )
    )
    session.commit()
    return bool(updated)


def delete_file(session: Session, file_id: str) -> None:
    session.execute(delete(FeatureRecord).where(FeatureRecord.file_id == file_id))
    session.execute(delete(FileRecord).where(FileRecord.id == file_id))
    session.commit()


@dataclass
class FeatureFilter:
    geometry_type: str | None = None
    status: str | None = None
    layer: str | None = None
    has_issues: bool | None = None


def query_features(
    session: Session, file_id: str, flt: FeatureFilter, *, limit: int, offset: int
) -> tuple[int, Sequence[FeatureRecord]]:
    query = select(FeatureRecord).where(FeatureRecord.file_id == file_id)
    if flt.geometry_type:
        query = query.where(func.lower(FeatureRecord.geometry_type) == flt.geometry_type.lower())
    if flt.status:
        query = query.where(FeatureRecord.status == flt.status)
    if flt.layer:
        query = query.where(FeatureRecord.layer == flt.layer)
    if flt.has_issues is not None:
        empty = or_(
            FeatureRecord.issues.is_(None), func.json_array_length(FeatureRecord.issues) == 0
        )
        query = query.where(~empty if flt.has_issues else empty)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.scalars(query.order_by(FeatureRecord.index).limit(limit).offset(offset)).all()
    return total, rows


def iter_features(session: Session, file_id: str, batch: int = 1000) -> Iterable[FeatureRecord]:
    last = -1
    while True:
        rows = session.scalars(
            select(FeatureRecord)
            .where(FeatureRecord.file_id == file_id, FeatureRecord.index > last)
            .order_by(FeatureRecord.index)
            .limit(batch)
        ).all()
        if not rows:
            return
        yield from rows
        last = rows[-1].index
