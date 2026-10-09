"""Generate the files in samples/ (run from the repo root: python scripts/make_samples.py)."""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import shapely
from pyogrio import raw
from pyproj import Transformer
from shapely.geometry import LineString, Polygon

OUT = Path(__file__).resolve().parent.parent / "samples"


def _zip_shapefile(
    name: str, geoms: list, fields: dict, crs: str | None, gtype: str, drop: tuple[str, ...] = ()
) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{name}.shp"
        raw.write(
            str(path),
            geometry=shapely.to_wkb(np.array(geoms, dtype=object)),
            field_data=[np.asarray(v) for v in fields.values()],
            fields=list(fields),
            crs=crs,
            geometry_type=gtype,
            driver="ESRI Shapefile",
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for part in sorted(Path(tmp).glob(f"{name}.*")):
                if part.suffix not in drop:
                    # fixed timestamp keeps regenerated archives byte-identical
                    info = zipfile.ZipInfo(f"{name}/{part.name}", date_time=(2024, 1, 1, 0, 0, 0))
                    zf.writestr(info, part.read_bytes(), zipfile.ZIP_DEFLATED)
        return buf.getvalue()


def _coords(geom: Polygon | LineString) -> str:
    ring = geom.exterior.coords if isinstance(geom, Polygon) else geom.coords
    return " ".join(f"{x:.6f},{y:.6f},0" for x, y in ring)


def farm_survey_kml() -> str:
    fields = [
        (
            "North field",
            "wheat",
            Polygon(
                [
                    (77.2010, 28.6130),
                    (77.2062, 28.6127),
                    (77.2069, 28.6166),
                    (77.2041, 28.6181),
                    (77.2008, 28.6170),
                ]
            ),
        ),
        (
            "Orchard",
            "mango",
            Polygon(
                [(77.2075, 28.6120), (77.2120, 28.6118), (77.2124, 28.6150), (77.2079, 28.6153)]
            ),
        ),
    ]
    pond = Polygon([(77.2092, 28.6131), (77.2101, 28.6131), (77.2101, 28.6139), (77.2092, 28.6139)])
    placemarks = []
    for name, crop, poly in fields:
        placemarks.append(
            f'<Placemark><name>{name}</name><ExtendedData><Data name="crop"><value>{crop}'
            f"</value></Data></ExtendedData><Polygon><outerBoundaryIs><LinearRing><coordinates>"
            f"{_coords(poly)}</coordinates></LinearRing></outerBoundaryIs>"
            + (
                f"<innerBoundaryIs><LinearRing><coordinates>{_coords(pond)}</coordinates>"
                "</LinearRing></innerBoundaryIs>"
                if name == "Orchard"
                else ""
            )
            + "</Polygon></Placemark>"
        )
    # drawn by hand in a hurry: the boundary crosses itself
    placemarks.append(
        "<Placemark><name>Disputed strip</name><Polygon><outerBoundaryIs><LinearRing>"
        "<coordinates>77.2000,28.6100,0 77.2030,28.6115,0 77.2030,28.6100,0 77.2000,28.6115,0 "
        "77.2000,28.6100,0</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
    )
    road = LineString([(77.2000, 28.6122), (77.2045, 28.6123), (77.2130, 28.6116)])
    placemarks.append(
        f"<Placemark><name>Farm road</name><LineString><coordinates>{_coords(road)}"
        "</coordinates></LineString></Placemark>"
    )
    placemarks.append(
        "<Placemark><name>Tube well</name><Point><coordinates>77.2050,28.6150,0</coordinates>"
        "</Point></Placemark>"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2">'
        "<Document><name>Farm survey</name><Folder><name>Plots</name>"
        + "".join(placemarks[:3])
        + "</Folder><Folder><name>Infrastructure</name>"
        + "".join(placemarks[3:])
        + "</Folder></Document></kml>\n"
    )


def fiji_kml() -> str:
    # a parcel straddling the 180th meridian
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2">'
        "<Document><Placemark><name>Taveuni parcel</name><Polygon><outerBoundaryIs><LinearRing>"
        "<coordinates>179.995,-16.80,0 -179.995,-16.80,0 -179.995,-16.79,0 179.995,-16.79,0 "
        "179.995,-16.80,0</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
        "</Document></kml>\n"
    )


def main() -> None:
    OUT.mkdir(exist_ok=True)
    # fixed line endings so regenerated samples are byte-identical on every OS
    (OUT / "farm_survey.kml").write_bytes(farm_survey_kml().encode("utf-8"))
    (OUT / "fiji_antimeridian.kml").write_bytes(fiji_kml().encode("utf-8"))

    rng = np.random.default_rng(7)
    parcels, ids, owners, crops = [], [], [], []
    for i in range(12):
        x0, y0 = 714_000 + (i % 4) * 160, 3_168_000 + (i // 4) * 120
        w, h = rng.uniform(110, 150), rng.uniform(80, 110)
        skew = rng.uniform(-10, 10)
        parcels.append(Polygon([(x0, y0), (x0 + w, y0 + skew), (x0 + w, y0 + h), (x0, y0 + h)]))
        ids.append(f"P-{i + 1:03d}")
        owners.append(["Sharma", "Verma", "Singh", "Patel"][i % 4])
        crops.append(["wheat", "rice", "mustard"][i % 3])
    fields = {"parcel_id": ids, "owner": owners, "crop": crops}
    (OUT / "parcels_utm43n.zip").write_bytes(
        _zip_shapefile("parcels", parcels, fields, "EPSG:32643", "Polygon")
    )
    (OUT / "parcels_no_prj.zip").write_bytes(
        _zip_shapefile("parcels", parcels, fields, "EPSG:32643", "Polygon", drop=(".prj",))
    )

    # a gas line on Long Island, in NY State Plane (US survey feet)
    pipeline = LineString([(1_050_000, 210_000), (1_053_500, 212_400), (1_058_000, 212_900)])
    (OUT / "pipeline_ny_feet.zip").write_bytes(
        _zip_shapefile("pipeline", [pipeline], {"name": ["LI-7 main"]}, "EPSG:2263", "LineString")
    )

    # the same Delhi fields, exported in Web Mercator by a web mapping tool
    to_merc = Transformer.from_crs(4326, 3857, always_xy=True)
    north_field = Polygon(
        [
            (77.2010, 28.6130),
            (77.2062, 28.6127),
            (77.2069, 28.6166),
            (77.2041, 28.6181),
            (77.2008, 28.6170),
        ]
    )
    merc = Polygon([to_merc.transform(x, y) for x, y in north_field.exterior.coords])
    (OUT / "fields_web_mercator.zip").write_bytes(
        _zip_shapefile("fields", [merc], {"name": ["North field"]}, "EPSG:3857", "Polygon")
    )
    for path in sorted(OUT.iterdir()):
        print(f"{path.name:28s} {path.stat().st_size:>7,d} bytes")


if __name__ == "__main__":
    main()
