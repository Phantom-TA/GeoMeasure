from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GEO_", env_file=".env", extra="ignore")

    app_name: str = "GeoMeasure API"
    data_dir: Path = Path("data")
    database_url: str | None = None

    max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_zip_entries: int = Field(default=1000, gt=0)
    max_uncompressed_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    max_compression_ratio: float = Field(default=200.0, gt=1)

    worker_threads: int = Field(default=2, ge=1)
    geodesic_warn_pct: float = Field(default=0.5, gt=0)

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'geomeasure.db').as_posix()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
