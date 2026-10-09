"""Awkward real-world files, end to end through the API."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from pyproj import Transformer
from shapely.geometry import MultiLineString, MultiPolygon, Polygon, box

from app.geo.geometry import transform
from tests.api.conftest import upload, wait_for
from tests.geo.truth import cell_area
from tests.samples import (
    FIELDS_KML,
    kml_document,
    kml_line,
    kml_point,
    kml_polygon,
    make_zip,
    shapefile_members,
    write_shapefile,
)


def measurements(client, file_id):
    return client.get(f"/api/files/{file_id}/measurements").json()["items"]


def codes(item):
    return {i["code"] for i in item["issues"]}


def shp_zip(tmp_path, name, geoms, **kwargs):
    shp = write_shapefile(tmp_path / name, name, geoms, **kwargs)
    return make_zip(shapefile_members(shp))


# --- CRS handling -------------------------------------------------------------------------


def test_distorting_custom_crs_falls_back_to_local(client, tmp_path):
    # A transverse Mercator with scale factor 1.5 has no area of use and inflates areas 2.25x;
    # the geodesic cross-check catches it and the feature is re-measured locally.
    crs = "+proj=tmerc +lat_0=0 +lon_0=77 +k=1.5 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    cell = box(77.0, 28.0, 77.01, 28.01)
    projected = transform(cell, Transformer.from_crs(4326, crs, always_xy=True))
    body = upload(client, shp_zip(tmp_path, "scaled", [projected], crs=crs), "scaled.zip").json()
    m = measurements(client, body["id"])[0]
    assert "source_crs_fallback" in codes(m)
    assert m["method"] == "local"
    assert m["area_m2"] == pytest.approx(cell_area(28.0, 28.01, 0.01), rel=1e-5)


def test_us_survey_feet(client, tmp_path):
    data = shp_zip(tmp_path, "ny", [box(1_000_000, 200_000, 1_001_000, 201_000)], crs="EPSG:2263")
    body = upload(client, data, "ny.zip").json()
    m = measurements(client, body["id"])[0]
    ft = 1200 / 3937
    assert m["method"] == "source"
    assert m["area_m2"] == pytest.approx(1_000_000 * ft * ft, abs=0.001)
    assert m["perimeter_m"] == pytest.approx(4000 * ft, abs=0.001)


def test_web_mercator_reprojected(client, tmp_path):
    cell = box(10.0, 60.0, 10.01, 60.01)
    merc = transform(cell, Transformer.from_crs(4326, 3857, always_xy=True))
    body = upload(client, shp_zip(tmp_path, "merc", [merc], crs="EPSG:3857"), "merc.zip").json()
    m = measurements(client, body["id"])[0]
    assert "source_crs_unsuitable" in codes(m)
    assert m["area_m2"] == pytest.approx(cell_area(60.0, 60.01, 0.01), rel=1e-5)


def test_non_wgs84_datum(client, tmp_path):
    data = shp_zip(
        tmp_path, "nad27", [box(500_000, 4_500_000, 501_000, 4_501_000)], crs="EPSG:26718"
    )  # NAD27 / UTM zone 18N
    body = upload(client, data, "nad27.zip").json()
    assert body["crs"] == "EPSG:26718"
    assert -75.1 < body["bbox"][0] < -74.9  # converted to WGS84 for output
    m = measurements(client, body["id"])[0]
    assert m["method"] == "source" and m["area_m2"] == pytest.approx(1_000_000)
    assert m["deviation_pct"] < 0.1


def test_layers_with_different_crs(client, tmp_path):
    a = write_shapefile(tmp_path / "a", "wgs", [box(77, 28, 77.01, 28.01)])
    b = write_shapefile(
        tmp_path / "b", "utm", [box(500000, 3100000, 501000, 3101000)], crs="EPSG:32643"
    )
    body = upload(client, make_zip(shapefile_members(a) | shapefile_members(b)), "both.zip").json()
    assert body["crs"] == "mixed"
    assert {layer["crs"] for layer in body["layers"]} == {"EPSG:4326", "EPSG:32643"}


def test_wrong_crs_gives_actionable_error(client):
    data = kml_document(kml_polygon([(10, 95), (10.01, 95), (10.01, 95.01), (10, 95.01)]))
    body = upload(client, data, "bad.kml").json()
    m = measurements(client, body["id"])[0]
    assert m["status"] == "error"
    assert "invalid_coordinates" in codes(m)


# --- geometry -----------------------------------------------------------------------------


def test_antimeridian_kml(client):
    data = kml_document(
        kml_polygon([(179.99, -16.0), (-179.99, -16.0), (-179.99, -15.99), (179.99, -15.99)])
    )
    body = upload(client, data, "fiji.kml").json()
    m = measurements(client, body["id"])[0]
    assert "antimeridian_normalized" in codes(m)
    assert m["area_m2"] == pytest.approx(cell_area(-16.0, -15.99, 0.02), rel=1e-5)


def test_bowtie_repaired(client):
    data = kml_document(kml_polygon([(0, 0), (0.01, 0.01), (0.01, 0), (0, 0.01)]))
    body = upload(client, data, "bowtie.kml").json()
    m = measurements(client, body["id"])[0]
    assert m["status"] == "ok" and "geometry_repaired" in codes(m)
    assert body["summary"]["features_with_issues"] == 1


def test_3d_line_measured_on_the_ground(client):
    data = kml_document(kml_line([(0, 0, 0), (1, 0, 5000)]))
    body = upload(client, data, "track.kml").json()
    feature = client.get(f"/api/files/{body['id']}/features").json()["items"][0]
    assert feature["has_z"] is True
    assert feature["measurement"]["length_m"] == pytest.approx(111_319.491, abs=0.01)


def test_multipart_and_holes_shapefile(client, tmp_path):
    holed = Polygon(
        [(0, 0), (0.02, 0), (0.02, 0.02), (0, 0.02)],
        [[(0.005, 0.005), (0.015, 0.005), (0.015, 0.015), (0.005, 0.015)]],
    )
    multi = MultiPolygon([box(1, 1, 1.01, 1.01), box(2, 2, 2.01, 2.01)])
    data = shp_zip(tmp_path, "polys", [holed, multi], geometry_type="MultiPolygon")
    items = measurements(client, upload(client, data, "polys.zip").json()["id"])
    hole_truth = cell_area(0, 0.02, 0.02) - cell_area(0.005, 0.015, 0.01)
    assert items[0]["area_m2"] == pytest.approx(hole_truth, rel=1e-5)
    assert items[1]["area_m2"] == pytest.approx(
        cell_area(1, 1.01, 0.01) + cell_area(2, 2.01, 0.01), rel=1e-5
    )

    lines = MultiLineString([[(0, 0), (0.5, 0)], [(0.5, 0), (1, 0)]])
    data = shp_zip(tmp_path, "lines", [lines], geometry_type="MultiLineString")
    item = measurements(client, upload(client, data, "lines.zip").json()["id"])[0]
    assert item["length_m"] == pytest.approx(111_319.491, abs=0.01)


def test_null_geometry_skipped(client, tmp_path):
    data = shp_zip(tmp_path, "nulls", [box(0, 0, 0.01, 0.01), None])
    items = measurements(client, upload(client, data, "nulls.zip").json()["id"])
    assert [i["status"] for i in items] == ["ok", "skipped"]
    assert "no_geometry" in codes(items[1])


# --- content ------------------------------------------------------------------------------


def test_empty_kml_is_valid(client):
    body = upload(client, kml_document(), "empty.kml").json()
    assert body["status"] == "COMPLETED" and body["feature_count"] == 0
    assert {i["code"] for i in body["issues"]} == {"no_features"}
    assert client.get(f"/api/files/{body['id']}/measurements").json()["total"] == 0


def test_points_only(client):
    data = kml_document(kml_point(1, 2, "a"), kml_point(3, 4, "b"))
    body = upload(client, data, "points.kml").json()
    assert body["summary"]["measurement_status"] == {"not_applicable": 2}
    assert body["summary"]["total_area_m2"] is None


def test_kmz(client):
    body = upload(client, make_zip({"doc.kml": FIELDS_KML.encode()}), "survey.kmz").json()
    assert body["format"] == "kmz" and body["feature_count"] == 4


def test_path_in_filename_is_stripped(client, settings):
    body = upload(client, FIELDS_KML.encode(), "../../etc/survey ü.kml").json()
    assert body["filename"] == "survey ü.kml"
    stored = list((settings.upload_dir / body["id"]).iterdir())
    assert [p.name for p in stored] == ["upload.bin"]  # never stored under the client's name


# --- concurrency --------------------------------------------------------------------------


def test_parallel_uploads_thread_mode(client):
    with ThreadPoolExecutor(8) as pool:
        ids = list(
            pool.map(
                lambda i: upload(client, FIELDS_KML.encode(), f"s{i}.kml", wait=False).json()["id"],
                range(8),
            )
        )
    assert {wait_for(client, i)["status"] for i in ids} == {"COMPLETED"}


def test_parallel_uploads_process_mode(make_client):
    client = make_client(worker_mode="process", workers=2)
    ids = [
        upload(client, FIELDS_KML.encode(), f"s{i}.kml", wait=False).json()["id"] for i in range(6)
    ]
    results = [wait_for(client, i, timeout=60) for i in ids]
    assert {r["status"] for r in results} == {"COMPLETED"}
    assert all(r["feature_count"] == 4 for r in results)
