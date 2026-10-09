from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app import __version__
from app.api import errors
from app.api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    TrailingSlashMiddleware,
)
from app.api.routes import health_router, router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import get_session_factory, init_db
from app.jobs.runner import JobFn, JobRunner
from app.services.processing import process_file

DESCRIPTION = """
Upload a **Shapefile** (zipped), **KML** or **KMZ** and get per-feature measurements:
area and perimeter for polygons, length for lines.

Measurements are never taken in latitude/longitude degrees. Each feature is measured in a
projection chosen for it, and every result is cross-checked against an independent
geodesic calculation on the ellipsoid.
"""

# Room for multipart boundaries and form fields on top of the file itself.
_MULTIPART_OVERHEAD = 64 * 1024

STATIC_DIR = Path(__file__).parent / "static"
_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://unpkg.com; "
    "style-src 'self' https://unpkg.com; "
    "img-src 'self' data: https://tile.openstreetmap.org; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)


def create_app(settings: Settings | None = None, job_fn: JobFn = process_file) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.ensure_dirs()
        init_db(settings.resolved_database_url)
        runner = JobRunner(settings, job_fn)
        runner.start()
        app.state.runner = runner
        await run_in_threadpool(runner.recover)
        yield
        await run_in_threadpool(runner.shutdown)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = get_session_factory(settings.resolved_database_url)

    errors.install(app)
    app.include_router(router, prefix="/api", tags=["files"])
    app.include_router(health_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def viewer() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Content-Security-Policy": _CSP})

    app.add_middleware(
        BodySizeLimitMiddleware, max_bytes=settings.max_upload_bytes + _MULTIPART_OVERHEAD
    )
    app.add_middleware(TrailingSlashMiddleware)
    app.add_middleware(RequestContextMiddleware)  # outermost: sees every response, even 413s
    return app


app = create_app()
