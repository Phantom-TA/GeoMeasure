"""Job functions for runner tests. Module-level so spawned worker processes can import them."""

import os

from app.core.config import Settings
from app.db import repository
from app.db.models import FileRecord
from app.db.session import get_session_factory
from app.services.processing import process_file


def crash_on_poison(file_id: str, settings: Settings) -> None:
    """Simulates a native crash (e.g. GDAL segfault) for files named *poison*."""
    factory = get_session_factory(settings.resolved_database_url)
    with factory() as session:
        record = session.get(FileRecord, file_id)
        is_poison = record is not None and "poison" in record.filename
    if not is_poison:
        process_file(file_id, settings)
        return
    with factory() as session:
        if repository.claim(session, file_id, settings.max_attempts) is None:
            return
    os._exit(1)
