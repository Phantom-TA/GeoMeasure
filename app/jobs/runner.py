"""Background execution of processing jobs.

Job state lives in the database, not in memory: the runner only decides *where* a job runs.
That gives us crash recovery for free (unfinished jobs are resubmitted on startup) and keeps
the API process safe from crashes in native code (GDAL parses untrusted files).
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
from collections.abc import Callable
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db import repository
from app.db.session import get_session_factory
from app.services.processing import process_file

log = logging.getLogger(__name__)

JobFn = Callable[[str, Settings], None]


class JobRunner:
    def __init__(self, settings: Settings, job_fn: JobFn = process_file) -> None:
        self.settings = settings
        self._job_fn = job_fn
        self._lock = threading.Lock()
        self._pool: Executor | None = None
        self._closed = False

    def _new_pool(self) -> Executor:
        if self.settings.worker_mode == "thread":
            return ThreadPoolExecutor(self.settings.workers, thread_name_prefix="geo-job")
        # spawn (not fork) on every OS: forking a process that holds GDAL/PROJ state and
        # threads is unsafe. Workers are recycled to cap any native memory growth.
        return ProcessPoolExecutor(
            self.settings.workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=configure_logging,
            max_tasks_per_child=100,
        )

    def start(self) -> None:
        with self._lock:
            self._pool = self._new_pool()
            self._closed = False

    def recover(self) -> int:
        """Resubmit jobs left PENDING or PROCESSING by a previous run."""
        factory = get_session_factory(self.settings.resolved_database_url)
        with factory() as session:
            ids = repository.active_file_ids(session)
        for file_id in ids:
            self.submit(file_id)
        if ids:
            log.info("recovered %d unfinished job(s)", len(ids))
        return len(ids)

    def submit(self, file_id: str) -> Future[None]:
        with self._lock:
            if self._closed or self._pool is None:
                raise RuntimeError("job runner is not running")
            pool = self._pool
            try:
                future = pool.submit(self._job_fn, file_id, self.settings)
            except BrokenProcessPool:
                pool = self._pool = self._new_pool()
                future = pool.submit(self._job_fn, file_id, self.settings)
        future.add_done_callback(lambda f: self._on_done(file_id, pool, f))
        return future

    def _on_done(self, file_id: str, pool: Executor, future: Future[None]) -> None:
        if future.cancelled():
            return
        exc = future.exception()
        if exc is None:
            return
        if isinstance(exc, BrokenProcessPool):
            # A worker died (e.g. native crash). Retry from a fresh pool; repository.claim()
            # caps attempts, so a file that keeps crashing ends up FAILED.
            log.error("worker crashed while processing %s; retrying", file_id)
            threading.Thread(target=self._retry, args=(file_id, pool), daemon=True).start()
        else:
            log.error("job %s raised unexpectedly", file_id, exc_info=exc)

    def _retry(self, file_id: str, broken: Executor) -> None:
        with self._lock:
            if self._closed:
                return
            if self._pool is broken:
                self._pool = self._new_pool()
        self.submit(file_id)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            pool, self._pool = self._pool, None
        if pool is not None:
            # running jobs finish; queued ones stay PENDING in the DB and are recovered later
            pool.shutdown(wait=True, cancel_futures=True)
