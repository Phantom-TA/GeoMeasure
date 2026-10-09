from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.geo.types import Strategy
from app.ingest.archive import ArchiveLimits


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GEO_", env_file=".env", extra="ignore")

    app_name: str = "GeoMeasure API"
    data_dir: Path = Path("data")
    database_url: str | None = None

    max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_zip_entries: int = Field(default=1000, gt=0)
    max_uncompressed_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    max_compression_ratio: float = Field(default=200.0, gt=1)

    worker_mode: Literal["process", "thread"] = "process"
    workers: int = Field(default=2, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    max_wait_seconds: float = Field(default=30.0, ge=0)

    default_strategy: Strategy = Strategy.AUTO
    geodesic_warn_pct: float = Field(default=0.5, gt=0)

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'geomeasure.db').as_posix()}"

    @property
    def archive_limits(self) -> ArchiveLimits:
        return ArchiveLimits(
            max_entries=self.max_zip_entries,
            max_uncompressed_bytes=self.max_uncompressed_bytes,
            max_ratio=self.max_compression_ratio,
        )

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.upload_dir, self.work_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
