"""Identify an upload by its content, not its extension."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from app.ingest.errors import IngestError

_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
_SNIFF_BYTES = 64 * 1024
_KML_ROOT = re.compile(rb"<(?:[\w-]+:)?kml[\s/>]", re.IGNORECASE)
_DTD = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)


class Container(StrEnum):
    ZIP = "zip"
    KML = "kml"


def detect_container(path: Path, filename: str) -> Container:
    with path.open("rb") as fh:
        head = fh.read(_SNIFF_BYTES)
    if not head:
        raise IngestError("empty_file", "The uploaded file is empty.")
    if head.startswith(_ZIP_MAGIC):
        return Container.ZIP

    text = head.removeprefix(b"\xef\xbb\xbf").lstrip()
    if text.startswith(b"<"):
        root = _KML_ROOT.search(text)
        if root:
            # KML never needs a DTD; refusing one rules out entity-expansion and XXE attacks
            if _DTD.search(text, 0, root.start()):
                raise IngestError(
                    "xml_dtd_not_allowed", "KML files with DOCTYPE/ENTITY declarations are refused."
                )
            return Container.KML

    suffix = Path(filename).suffix.lower() or "unknown"
    raise IngestError(
        "unsupported_format",
        f"Unsupported file ({suffix}). Upload a .kml, a .kmz, or a .zip containing a Shapefile.",
    )
