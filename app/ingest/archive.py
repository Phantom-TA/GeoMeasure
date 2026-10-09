"""Inspect and unpack zip uploads without trusting anything inside them.

Archive member names are only used to group Shapefile parts; nothing is ever written to a
path taken from the archive, so path traversal ("zip slip") is impossible by construction.
"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.geo.types import Issue
from app.ingest.errors import IngestError

SHAPEFILE_PARTS = (".shp", ".shx", ".dbf", ".prj", ".cpg")
_JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
_RATIO_MIN_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int = 1000
    max_uncompressed_bytes: int = 1024 * 1024 * 1024
    max_ratio: float = 200.0


@dataclass
class ShapefileGroup:
    name: str
    parts: dict[str, zipfile.ZipInfo]


@dataclass
class ArchiveContents:
    shapefiles: list[ShapefileGroup] = field(default_factory=list)
    kml: list[zipfile.ZipInfo] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


def _is_junk(path: PurePosixPath) -> bool:
    return (
        "__MACOSX" in path.parts or path.name.startswith("._") or path.name.lower() in _JUNK_NAMES
    )


def inspect_zip(zf: zipfile.ZipFile, limits: ArchiveLimits) -> ArchiveContents:
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > limits.max_entries:
        raise IngestError(
            "archive_too_many_entries",
            f"Archive has {len(infos)} files; the limit is {limits.max_entries}.",
        )
    total = sum(i.file_size for i in infos)
    if total > limits.max_uncompressed_bytes:
        raise IngestError(
            "archive_too_large",
            f"Archive expands to {total:,} bytes; the limit is {limits.max_uncompressed_bytes:,}.",
        )

    contents = ArchiveContents()
    groups: dict[tuple[str, str], dict[str, zipfile.ZipInfo]] = {}
    ignored: list[str] = []

    for info in infos:
        path = PurePosixPath(info.filename.replace("\\", "/"))
        if _is_junk(path):
            continue
        if info.flag_bits & 0x1:
            raise IngestError("archive_encrypted", "Password-protected archives are not supported.")
        if (
            info.file_size > _RATIO_MIN_BYTES
            and info.file_size / max(info.compress_size, 1) > limits.max_ratio
        ):
            raise IngestError(
                "archive_suspicious_compression",
                f"'{path.name}' has an implausible compression ratio (possible zip bomb).",
            )

        suffix = path.suffix.lower()
        if suffix in SHAPEFILE_PARTS:
            key = (str(path.parent).lower(), path.stem.lower())
            groups.setdefault(key, {})[suffix] = info
        elif suffix == ".kml":
            contents.kml.append(info)
        elif suffix in (".zip", ".kmz"):
            contents.issues.append(
                Issue("nested_archive_ignored", f"Nested archive '{path.name}' was not opened.")
            )
        elif not path.name.lower().endswith(".shp.xml"):
            ignored.append(path.name)

    if ignored:
        preview = ", ".join(ignored[:5]) + (" ..." if len(ignored) > 5 else "")
        contents.issues.append(
            Issue("ignored_entries", f"{len(ignored)} unrelated file(s) ignored: {preview}")
        )

    stems = [stem for _, stem in groups]
    for (parent, stem), parts in groups.items():
        shp = parts.get(".shp")
        if shp is None:
            contents.issues.append(
                Issue("incomplete_shapefile", f"'{stem}' has no .shp file and was skipped.")
            )
            continue
        shp_path = PurePosixPath(shp.filename.replace("\\", "/"))
        name = shp_path.stem
        if stems.count(stem) > 1 and parent not in ("", "."):
            name = f"{shp_path.parent.as_posix()}/{shp_path.stem}"
        contents.shapefiles.append(ShapefileGroup(name=name, parts=parts))

    contents.shapefiles.sort(key=lambda g: g.name.lower())
    return contents


class ExtractionBudget:
    """Counts bytes actually decompressed, in case an archive lies about its sizes."""

    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def extract(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path) -> None:
        with zf.open(info) as src, dest.open("wb") as out:
            while chunk := src.read(1024 * 1024):
                self.remaining -= len(chunk)
                if self.remaining < 0:
                    raise IngestError(
                        "archive_too_large", "Archive expands beyond the allowed size."
                    )
                out.write(chunk)


def copy_file(src: Path, dest: Path) -> None:
    shutil.copyfile(src, dest)
