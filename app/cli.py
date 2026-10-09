"""Measure a file from the command line, using the same pipeline as the API.

python -m app.cli measure samples/farm_survey.kml
python -m app.cli measure parcels.zip --crs EPSG:32643 --strategy utm --json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from pyproj import CRS
from pyproj.exceptions import CRSError

from app.geo.measure import MeasureOptions
from app.geo.types import Strategy
from app.ingest.errors import IngestError
from app.ingest.prepare import load_layers, precheck, prepare
from app.services.processing import ResultBuilder

_COLUMNS = ("index", "layer", "geometry_type", "status", "area_m2", "length_m", "deviation_pct")


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4g}" if abs(value) < 1e-2 else f"{value:,.2f}"
    return str(value)


def measure(path: Path, crs: str | None, strategy: Strategy) -> tuple[list[dict[str, Any]], Any]:
    override = CRS.from_user_input(crs) if crs else None
    precheck(path, path.name)
    with tempfile.TemporaryDirectory() as work:
        source = prepare(path, path.name, Path(work))
        layers, issues = load_layers(source, override)
    builder = ResultBuilder("cli", layers, issues, MeasureOptions(strategy=strategy))
    rows = [row for batch in builder.batches() for row in batch]
    return rows, builder.outcome(source.format.value, 0)


def _print_table(rows: list[dict[str, Any]]) -> None:
    table = [[_fmt(r[c]) for c in _COLUMNS] for r in rows]
    widths = [max(len(c), *(len(t[i]) for t in table)) for i, c in enumerate(_COLUMNS)]
    print("  ".join(c.ljust(w) for c, w in zip(_COLUMNS, widths, strict=True)))
    for t in table:
        print("  ".join(v.ljust(w) for v, w in zip(t, widths, strict=True)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="geomeasure", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("measure", help="measure the features in a file")
    m.add_argument("file", type=Path)
    m.add_argument("--crs", help="CRS to use if the file has none, e.g. EPSG:32643")
    m.add_argument("--strategy", choices=[s.value for s in Strategy], default="auto")
    m.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    try:
        rows, outcome = measure(args.file, args.crs, Strategy(args.strategy))
    except (IngestError, CRSError, OSError) as exc:
        print(f"error: {getattr(exc, 'message', exc)}", file=sys.stderr)
        return 1

    for row in rows:
        row.pop("geometry")
        row.pop("geometry_wgs84")
        row.pop("file_id")
    if args.json:
        print(json.dumps({"summary": outcome.summary, "features": rows}, indent=2))
        return 0
    _print_table(rows)
    s = outcome.summary
    print(
        f"\n{outcome.feature_count} features ({outcome.format}, {outcome.crs}); "
        f"total area {_fmt(s['total_area_m2'])} m2, total length {_fmt(s['total_length_m'])} m"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
