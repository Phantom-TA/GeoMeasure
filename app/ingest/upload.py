from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePath
from typing import BinaryIO

from app.ingest.errors import IngestError

_CHUNK = 1024 * 1024
_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


def save_upload(src: BinaryIO, dest: Path, max_bytes: int) -> tuple[int, str]:
    """Copy the upload to disk, enforcing the size limit and hashing as we go."""
    digest = hashlib.sha256()
    size = 0
    with dest.open("wb") as out:
        while chunk := src.read(_CHUNK):
            size += len(chunk)
            if size > max_bytes:
                raise IngestError(
                    "file_too_large", f"File exceeds the {max_bytes:,} byte upload limit."
                )
            digest.update(chunk)
            out.write(chunk)
    if size == 0:
        raise IngestError("empty_file", "The uploaded file is empty.")
    return size, digest.hexdigest()


def clean_filename(name: str | None) -> str:
    """Display name only; uploads are stored under generated names, never this one."""
    base = PurePath((name or "").replace("\\", "/")).name
    base = _UNSAFE.sub("", base).strip()
    return base[:255] or "upload"
