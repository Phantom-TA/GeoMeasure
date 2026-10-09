from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class FileStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012
        dict[str, Any]: JSON,
        list[Any]: JSON,
        datetime: DateTime(timezone=True),
    }


class FileRecord(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)
    strategy: Mapped[str] = mapped_column(String(16))
    crs_override: Mapped[str | None] = mapped_column(Text)

    format: Mapped[str | None] = mapped_column(String(16))
    crs: Mapped[str | None] = mapped_column(Text)
    feature_count: Mapped[int | None]
    bbox: Mapped[list[Any] | None]
    layers: Mapped[list[Any] | None]
    summary: Mapped[dict[str, Any] | None]
    issues: Mapped[list[Any] | None]
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    processing_ms: Mapped[int | None]


class FeatureRecord(Base):
    __tablename__ = "features"
    __table_args__ = (
        UniqueConstraint("file_id", "index"),
        Index("ix_features_file_type", "file_id", "geometry_type"),
        Index("ix_features_file_status", "file_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    index: Mapped[int]
    layer: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str | None] = mapped_column(Text)
    geometry_type: Mapped[str | None] = mapped_column(String(32))
    crs: Mapped[str | None] = mapped_column(Text)
    has_z: Mapped[bool] = mapped_column(default=False)
    geometry: Mapped[dict[str, Any] | None]
    geometry_wgs84: Mapped[dict[str, Any] | None]
    properties: Mapped[dict[str, Any]]

    status: Mapped[str] = mapped_column(String(16))
    area_m2: Mapped[float | None]
    perimeter_m: Mapped[float | None]
    length_m: Mapped[float | None]
    geodesic_area_m2: Mapped[float | None]
    geodesic_perimeter_m: Mapped[float | None]
    geodesic_length_m: Mapped[float | None]
    deviation_pct: Mapped[float | None]
    method: Mapped[str | None] = mapped_column(String(16))
    projections: Mapped[list[Any]]
    issues: Mapped[list[Any]]
