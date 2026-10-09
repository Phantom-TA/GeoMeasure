import json
import threading

import pytest
from shapely.geometry import LineString, box

from app.db import repository
from app.db.session import get_session_factory, init_db
from tests.api.conftest import upload, wait_for
from tests.geo.truth import cell_area
from tests.samples import FIELDS_KML, make_zip, shapefile_members, write_shapefile


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["database"] == "ok"


# --- upload ---------------------------------------------------------------------------------


def test_upload_and_wait(client):
    res = upload(client, FIELDS_KML.encode())
    assert res.status_code == 201
    assert res.headers["Location"] == f"/api/files/{res.json()['id']}"
    assert res.headers["Preference-Applied"] == "wait=15"
    body = res.json()
    assert body["status"] == "COMPLETED"
    assert body["filename"] == "survey.kml"
    assert body["format"] == "kml"
    assert body["feature_count"] == 4
    assert body["crs"] == "EPSG:4326"
    assert [layer["name"] for layer in body["layers"]] == ["Fields", "Infrastructure"]
    assert body["summary"]["geometry_types"] == {
        "Polygon": 1,
        "LineString": 1,
        "Point": 1,
        "GeometryCollection": 1,
    }
    assert body["bbox"] == pytest.approx([77.0, 28.0, 77.1, 28.1])


def test_upload_async_then_poll(client):
    res = upload(client, FIELDS_KML.encode(), wait=False)
    assert res.status_code == 202
    assert res.json()["status"] in ("PENDING", "PROCESSING", "COMPLETED")
    assert wait_for(client, res.json()["id"])["status"] == "COMPLETED"


def test_trailing_slash_optional(client, kml_file):
    fid = kml_file["id"]
    for path in (f"/api/files/{fid}", f"/api/files/{fid}/", "/api/files", "/api/files/"):
        res = client.get(path, follow_redirects=False)
        assert res.status_code == 200, path


# --- measurements -------------------------------------------------------------------------


def test_measurements(client, kml_file):
    body = client.get(f"/api/files/{kml_file['id']}/measurements/").json()
    assert body["total"] == 4
    by_type = {m["geometry_type"]: m for m in body["items"]}

    polygon = by_type["Polygon"]
    assert polygon["source_id"] == "field-1"
    assert polygon["status"] == "ok"
    assert polygon["area_m2"] == pytest.approx(cell_area(28.0, 28.01, 0.01), rel=1e-6)
    assert polygon["geodesic"]["area_m2"] == pytest.approx(polygon["area_m2"], rel=1e-6)
    assert polygon["length_m"] is None
    assert polygon["method"] == "local"

    assert by_type["LineString"]["length_m"] == pytest.approx(9836.19, abs=0.01)
    assert by_type["Point"]["status"] == "not_applicable"
    assert by_type["Point"]["area_m2"] is None and by_type["Point"]["length_m"] is None


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("geometry_type=polygon", [0]),
        ("status=not_applicable", [2]),
        ("layer=Infrastructure", [1, 2, 3]),
        ("has_issues=true", [2]),
        ("has_issues=false", [0, 1, 3]),
        ("limit=2&offset=1", [1, 2]),
    ],
)
def test_measurement_filters(client, kml_file, query, expected):
    body = client.get(f"/api/files/{kml_file['id']}/measurements?{query}").json()
    assert [m["index"] for m in body["items"]] == expected


def test_measurements_validation(client, kml_file):
    res = client.get(f"/api/files/{kml_file['id']}/measurements?limit=5000")
    assert res.status_code == 422 and res.json()["code"] == "validation_error"


# --- features / export --------------------------------------------------------------------


def test_features_source_geometry(client, kml_file):
    feature = client.get(f"/api/files/{kml_file['id']}/features?limit=1").json()["items"][0]
    assert feature["geometry_type"] == "Polygon"
    assert feature["crs"] == "EPSG:4326"
    assert feature["has_z"] is True
    assert feature["properties"] == {"Name": "North field", "crop": "wheat"}
    assert feature["geometry"]["type"] == "Polygon"
    assert len(feature["geometry"]["coordinates"][0][0]) == 3  # source keeps Z
    assert feature["measurement"]["status"] == "ok"


def test_features_wgs84_geometry_is_2d(client, kml_file):
    feature = client.get(f"/api/files/{kml_file['id']}/features?limit=1&geometry_crs=wgs84").json()[
        "items"
    ][0]
    assert len(feature["geometry"]["coordinates"][0][0]) == 2


def test_geojson_export(client, kml_file):
    res = client.get(f"/api/files/{kml_file['id']}/geojson")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/geo+json")
    fc = json.loads(res.content)
    assert fc["type"] == "FeatureCollection"
    assert [f["id"] for f in fc["features"]] == [0, 1, 2, 3]
    props = fc["features"][0]["properties"]
    assert props["crop"] == "wheat"
    assert props["_area_m2"] > 0 and props["_status"] == "ok"


# --- shapefiles, CRS and strategies ------------------------------------------------------


@pytest.fixture
def utm_zip(tmp_path):
    shp = write_shapefile(
        tmp_path / "src", "plots", [box(500000, 3100000, 501000, 3101000)], crs="EPSG:32643"
    )
    return make_zip(shapefile_members(shp))


def test_projected_shapefile_measured_natively(client, utm_zip):
    body = upload(client, utm_zip, "plots.zip").json()
    assert body["format"] == "shapefile" and body["crs"] == "EPSG:32643"
    m = client.get(f"/api/files/{body['id']}/measurements").json()["items"][0]
    assert m["method"] == "source"
    assert m["area_m2"] == pytest.approx(1_000_000)
    assert m["projections"] == ["EPSG:32643"]


def test_strategy_parameter(client, utm_zip):
    body = upload(client, utm_zip, "plots.zip", strategy="utm").json()
    m = client.get(f"/api/files/{body['id']}/measurements").json()["items"][0]
    assert m["method"] == "utm" and m["projections"] == ["UTM 43N (EPSG:32643)"]


def test_crs_override_rescues_missing_prj(client, tmp_path):
    shp = write_shapefile(tmp_path / "src", "noprj", [box(500000, 3100000, 501000, 3101000)])
    members = {k: v for k, v in shapefile_members(shp).items() if not k.endswith(".prj")}
    data = make_zip(members)

    unknown = upload(client, data, "noprj.zip").json()
    assert unknown["status"] == "COMPLETED" and unknown["crs"] is None
    m = client.get(f"/api/files/{unknown['id']}/measurements").json()["items"][0]
    assert m["status"] == "error" and m["issues"][0]["code"] == "crs_unknown"

    fixed = upload(client, data, "noprj.zip", crs="EPSG:32643").json()
    assert fixed["crs"] == "EPSG:32643" and fixed["crs_override"] == "EPSG:32643"
    assert fixed["layers"][0]["crs_source"] == "override"
    m = client.get(f"/api/files/{fixed['id']}/measurements").json()["items"][0]
    assert m["status"] == "ok" and m["area_m2"] == pytest.approx(1_000_000)


def test_lines_shapefile(client, tmp_path):
    shp = write_shapefile(
        tmp_path / "src", "roads", [LineString([(0, 0), (1, 0)])], geometry_type="LineString"
    )
    body = upload(client, make_zip(shapefile_members(shp)), "roads.zip").json()
    m = client.get(f"/api/files/{body['id']}/measurements").json()["items"][0]
    assert m["length_m"] == pytest.approx(111_319.491, abs=0.01)


# --- rejected uploads ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "name", "status", "code"),
    [
        (b"", "empty.kml", 400, "empty_file"),
        (b'{"type": "FeatureCollection"}', "x.geojson", 415, "unsupported_format"),
        (
            b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "b">]><kml/>',
            "x.kml",
            415,
            "xml_dtd_not_allowed",
        ),
        (b"PK\x03\x04garbage", "x.zip", 422, "invalid_archive"),
        (make_zip({"readme.txt": b"hello"}), "x.zip", 422, "no_supported_data"),
    ],
)
def test_rejected_immediately(client, settings, data, name, status, code):
    res = upload(client, data, name)
    assert res.status_code == status
    assert res.headers["content-type"] == "application/problem+json"
    assert res.json()["code"] == code
    assert not any(settings.upload_dir.iterdir())  # nothing left behind


def test_invalid_crs(client):
    res = upload(client, FIELDS_KML.encode(), crs="EPSG:999999")
    assert res.status_code == 422 and res.json()["code"] == "invalid_crs"


def test_invalid_strategy(client):
    res = upload(client, FIELDS_KML.encode(), strategy="magic")
    assert res.status_code == 422 and res.json()["errors"][0]["loc"] == ["query", "strategy"]


def test_upload_too_large(make_client):
    small = make_client(max_upload_bytes=1000)
    res = upload(small, b"<kml>" + b" " * 200_000 + b"</kml>")
    assert res.status_code == 413 and res.json()["code"] == "file_too_large"


def test_processing_failure_is_reported(client):
    data = make_zip({"broken.shp": b"not a shapefile", "broken.dbf": b"x"})
    body = upload(client, data, "broken.zip").json()
    assert body["status"] == "FAILED"
    assert body["error"]["code"] == "no_readable_layers"
    res = client.get(f"/api/files/{body['id']}/measurements")
    assert res.status_code == 409 and res.json()["code"] == "file_failed"


# --- lifecycle ----------------------------------------------------------------------------


def test_not_ready_and_busy(make_client):
    release = threading.Event()

    def blocked_job(file_id, settings):
        release.wait(10)

    client = make_client(job_fn=blocked_job)
    fid = upload(client, FIELDS_KML.encode(), wait=False).json()["id"]
    try:
        res = client.get(f"/api/files/{fid}/measurements")
        assert res.status_code == 409 and res.json()["code"] == "file_not_ready"
        assert client.delete(f"/api/files/{fid}").json()["code"] == "file_busy"
        assert client.post(f"/api/files/{fid}/reprocess").json()["code"] == "file_busy"
    finally:
        release.set()


def test_reprocess_with_other_strategy(client, kml_file):
    fid = kml_file["id"]
    res = client.post(f"/api/files/{fid}/reprocess?strategy=utm", headers={"Prefer": "wait=15"})
    assert res.status_code == 201 and res.json()["strategy"] == "utm"
    items = client.get(f"/api/files/{fid}/measurements").json()["items"]
    assert {m["method"] for m in items if m["status"] == "ok"} == {"utm"}
    assert len(items) == 4  # features replaced, not duplicated


def test_delete(client, kml_file, settings):
    fid = kml_file["id"]
    assert client.delete(f"/api/files/{fid}").status_code == 204
    assert client.get(f"/api/files/{fid}").status_code == 404
    assert not (settings.upload_dir / fid).exists()


def test_list_files(client):
    for _ in range(3):
        upload(client, FIELDS_KML.encode())
    upload(client, make_zip({"broken.shp": b"x", "broken.dbf": b"x"}), "bad.zip")
    assert client.get("/api/files").json()["total"] == 4
    assert client.get("/api/files?status=FAILED").json()["total"] == 1
    page = client.get("/api/files?limit=2&offset=1").json()
    assert len(page["items"]) == 2


def test_not_found(client):
    res = client.get("/api/files/does-not-exist/measurements")
    assert res.status_code == 404 and res.json()["code"] == "file_not_found"


# --- resilience ---------------------------------------------------------------------------


def test_unfinished_jobs_recovered_on_startup(make_client, settings):
    settings.ensure_dirs()
    init_db(settings.resolved_database_url)
    factory = get_session_factory(settings.resolved_database_url)
    with factory() as session:
        for fid, status in (("a" * 32, "PENDING"), ("b" * 32, "PROCESSING")):
            path = settings.upload_dir / fid / "upload.bin"
            path.parent.mkdir(parents=True)
            path.write_text(FIELDS_KML, encoding="utf-8")
            repository.create_file(
                session,
                id=fid,
                filename="survey.kml",
                size_bytes=1,
                sha256="x",
                strategy="auto",
                crs_override=None,
            )
            if status == "PROCESSING":  # left behind by a crash mid-job
                record = repository.get_file(session, fid)
                record.status, record.attempts = "PROCESSING", 1
                session.commit()

    client = make_client()
    assert wait_for(client, "a" * 32)["status"] == "COMPLETED"
    recovered = wait_for(client, "b" * 32)
    assert recovered["status"] == "COMPLETED" and recovered["attempts"] == 2


@pytest.mark.timeout(120)
def test_worker_crash_is_contained(make_client):
    from tests.api.jobs import crash_on_poison

    client = make_client(job_fn=crash_on_poison, worker_mode="process", workers=1)
    poison = upload(client, FIELDS_KML.encode(), "poison.kml", wait=False).json()["id"]
    result = wait_for(client, poison, timeout=90)
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "processing_crashed"
    assert result["attempts"] == 3

    assert client.get("/health").json()["status"] == "ok"  # API never went down
    healthy = upload(client, FIELDS_KML.encode(), "fine.kml").json()
    assert wait_for(client, healthy["id"], timeout=60)["status"] == "COMPLETED"
