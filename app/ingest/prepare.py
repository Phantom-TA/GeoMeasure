"""Turn a raw upload into readable layers: detect, unpack safely, read."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pyproj import CRS

from app.geo.readers import ReadError, ShapefileSource, read_kml, read_shapefile
from app.geo.types import Issue, Layer
from app.ingest.archive import ArchiveLimits, ExtractionBudget, copy_file, inspect_zip
from app.ingest.detect import Container, detect_container
from app.ingest.errors import IngestError


class SourceFormat(StrEnum):
    SHAPEFILE = "shapefile"
    KML = "kml"
    KMZ = "kmz"


@dataclass
class PreparedSource:
    format: SourceFormat
    shapefiles: list[ShapefileSource] = field(default_factory=list)
    kml: Path | None = None
    issues: list[Issue] = field(default_factory=list)


def prepare(
    upload: Path, filename: str, workdir: Path, limits: ArchiveLimits | None = None
) -> PreparedSource:
    """Validate the upload and place the files GDAL needs into workdir."""
    limits = limits or ArchiveLimits()
    if detect_container(upload, filename) is Container.KML:
        kml = workdir / "doc.kml"
        copy_file(upload, kml)
        return PreparedSource(SourceFormat.KML, kml=kml)

    try:
        with zipfile.ZipFile(upload) as zf:
            return _prepare_zip(zf, workdir, limits)
    except zipfile.BadZipFile as exc:
        raise IngestError("invalid_archive", f"The zip file is corrupt: {exc}") from exc


def _prepare_zip(zf: zipfile.ZipFile, workdir: Path, limits: ArchiveLimits) -> PreparedSource:
    contents = inspect_zip(zf, limits)
    budget = ExtractionBudget(limits.max_uncompressed_bytes)

    if contents.shapefiles:
        source = PreparedSource(SourceFormat.SHAPEFILE, issues=contents.issues)
        if contents.kml:
            source.issues.append(
                Issue("ignored_entries", "KML files inside a Shapefile archive were ignored.")
            )
        for n, group in enumerate(contents.shapefiles):
            for ext, info in group.parts.items():
                budget.extract(zf, info, workdir / f"layer{n}{ext}")
            source.shapefiles.append(
                ShapefileSource(
                    name=group.name,
                    path=workdir / f"layer{n}.shp",
                    issues=tuple(_missing_part_issues(group.name, group.parts)),
                )
            )
        return source

    if contents.kml:
        main = next(
            (i for i in contents.kml if i.filename.replace("\\", "/").lower() == "doc.kml"),
            contents.kml[0],
        )
        kml = workdir / "doc.kml"
        budget.extract(zf, main, kml)
        source = PreparedSource(SourceFormat.KMZ, kml=kml, issues=contents.issues)
        if len(contents.kml) > 1:
            source.issues.append(
                Issue(
                    "ignored_entries",
                    f"Read '{main.filename}'; {len(contents.kml) - 1} other KML file(s) ignored.",
                )
            )
        return source

    raise IngestError(
        "no_supported_data",
        "The archive contains no Shapefile (.shp) or KML file.",
    )


def _missing_part_issues(name: str, parts: dict[str, zipfile.ZipInfo]) -> list[Issue]:
    issues = []
    if ".shx" not in parts:
        issues.append(Issue("missing_shx", f"'{name}.shx' missing; index rebuilt from the .shp."))
    if ".dbf" not in parts:
        issues.append(Issue("missing_dbf", f"'{name}.dbf' missing; features have no attributes."))
    return issues


def load_layers(
    source: PreparedSource, crs_override: CRS | None = None
) -> tuple[list[Layer], list[Issue]]:
    layers: list[Layer] = []
    issues = list(source.issues)
    index = 0

    for shp in source.shapefiles:
        try:
            layer = read_shapefile(shp, start_index=index, crs_override=crs_override)
        except ReadError as exc:
            issues.append(Issue(exc.code, exc.message))
            continue
        layers.append(layer)
        index += len(layer.features)

    if source.kml is not None:
        try:
            layers.extend(read_kml(source.kml, start_index=index, crs_override=crs_override))
        except ReadError as exc:
            raise IngestError(exc.code, exc.message) from exc

    if not layers:
        details = "; ".join(i.message for i in issues if i.code == "unreadable_layer")
        raise IngestError("no_readable_layers", details or "No readable layers in the file.")
    return layers, issues
