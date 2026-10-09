import io
import zipfile

import pytest
from shapely.geometry import box

from app.ingest.archive import ArchiveLimits, ExtractionBudget
from app.ingest.errors import IngestError
from app.ingest.prepare import SourceFormat, load_layers, prepare
from tests.samples import FIELDS_KML, make_zip, shapefile_members, write_shapefile

BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE kml [<!ENTITY a "lol"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>&b;</name></Document></kml>"""


@pytest.fixture
def parcels(tmp_path):
    shp = write_shapefile(tmp_path / "src", "parcels", [box(77, 28, 77.01, 28.01)])
    return shapefile_members(shp)


@pytest.fixture
def run(tmp_path):
    def _run(data: bytes, filename: str = "upload.zip", limits: ArchiveLimits | None = None):
        upload = tmp_path / "upload.bin"
        upload.write_bytes(data)
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        source = prepare(upload, filename, work, limits)
        return source, work

    return _run


def error_code(fn):
    with pytest.raises(IngestError) as exc:
        fn()
    return exc.value.code


def codes(issues):
    return {i.code for i in issues}


# --- KML -------------------------------------------------------------------------------


def test_plain_kml(run):
    source, _ = run(FIELDS_KML.encode(), "survey.kml")
    assert source.format is SourceFormat.KML
    layers, _ = load_layers(source)
    assert sum(len(layer.features) for layer in layers) == 4


def test_kml_with_bom_and_whitespace(run):
    source, _ = run(b"\xef\xbb\xbf\n  " + FIELDS_KML.encode(), "survey.kml")
    assert source.format is SourceFormat.KML


@pytest.mark.parametrize(
    "payload",
    [
        BILLION_LAUGHS,
        '<?xml version="1.0"?><!DOCTYPE kml SYSTEM "file:///etc/passwd"><kml></kml>',
    ],
)
def test_kml_with_dtd_refused(run, payload):
    assert error_code(lambda: run(payload.encode(), "evil.kml")) == "xml_dtd_not_allowed"


@pytest.mark.parametrize(
    ("data", "name", "code"),
    [
        (b"", "x.kml", "empty_file"),
        (b'{"type": "FeatureCollection", "features": []}', "x.geojson", "unsupported_format"),
        (b"\x89PNG\r\n\x1a\n" + b"\0" * 50, "x.png", "unsupported_format"),
        (b"<html><body>not kml</body></html>", "x.kml", "unsupported_format"),
        (b"PK\x03\x04" + b"garbage" * 20, "x.zip", "invalid_archive"),
    ],
)
def test_rejected_uploads(run, data, name, code):
    assert error_code(lambda: run(data, name)) == code


def test_content_beats_extension(run):
    source, _ = run(FIELDS_KML.encode(), "misnamed.zip")
    assert source.format is SourceFormat.KML


# --- Shapefile archives --------------------------------------------------------------------


def test_shapefile_at_root(run, parcels):
    source, _ = run(make_zip(parcels))
    assert source.format is SourceFormat.SHAPEFILE
    layers, issues = load_layers(source)
    assert [layer.name for layer in layers] == ["parcels"]
    assert not issues


def test_nested_folder_and_os_junk_ignored(run, parcels):
    members = {f"export/gis/{k}": v for k, v in parcels.items()}
    members |= {
        "__MACOSX/export/gis/._parcels.shp": b"junk",
        "export/.DS_Store": b"junk",
        "export/gis/parcels.shp.xml": b"<metadata/>",
    }
    source, _ = run(make_zip(members))
    layers, issues = load_layers(source)
    assert [layer.name for layer in layers] == ["parcels"]
    assert not issues


def test_multiple_shapefiles_get_continuous_indexes(run, tmp_path):
    a = write_shapefile(tmp_path / "a", "roads", [box(0, 0, 1, 1)] * 2)
    b = write_shapefile(tmp_path / "b", "lakes", [box(0, 0, 1, 1)] * 3)
    source, _ = run(make_zip(shapefile_members(a) | shapefile_members(b)))
    layers, _ = load_layers(source)
    assert [layer.name for layer in layers] == ["lakes", "roads"]
    assert [f.index for layer in layers for f in layer.features] == [0, 1, 2, 3, 4]


def test_same_name_in_two_folders(run, parcels):
    members = {f"2023/{k}": v for k, v in parcels.items()} | {
        f"2024/{k}": v for k, v in parcels.items()
    }
    source, _ = run(make_zip(members))
    assert [s.name for s in source.shapefiles] == ["2023/parcels", "2024/parcels"]


def test_uppercase_extensions(run, parcels):
    members = {
        k.rsplit(".", 1)[0] + "." + k.rsplit(".", 1)[1].upper(): v for k, v in parcels.items()
    }
    source, _ = run(make_zip(members))
    layers, _ = load_layers(source)
    assert len(layers[0].features) == 1


def test_missing_shx_and_dbf(run, parcels):
    members = {k: v for k, v in parcels.items() if not k.endswith((".shx", ".dbf"))}
    source, _ = run(make_zip(members))
    layers, _ = load_layers(source)
    assert {"missing_shx", "missing_dbf"} <= codes(layers[0].issues)
    assert len(layers[0].features) == 1


def test_sidecar_without_shp(run, parcels):
    members = {k: v for k, v in parcels.items() if not k.endswith(".shp")}
    assert error_code(lambda: run(make_zip(members))) == "no_supported_data"


def test_zip_without_geodata(run):
    assert error_code(lambda: run(make_zip({"readme.txt": b"hi"}))) == "no_supported_data"


def test_unreadable_shapefile_does_not_sink_the_others(run, parcels):
    members = dict(parcels) | {"broken.shp": b"not a shapefile at all", "broken.dbf": b"nope"}
    source, _ = run(make_zip(members))
    layers, issues = load_layers(source)
    assert [layer.name for layer in layers] == ["parcels"]
    assert "unreadable_layer" in codes(issues)


def test_path_traversal_names_never_touch_disk(run, parcels, tmp_path):
    members = {f"../../../evil/{k}": v for k, v in parcels.items()}
    source, work = run(make_zip(members))
    assert {p.name for p in work.iterdir()} == {
        f"layer0{ext}" for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg")
    }
    assert not (tmp_path.parent / "evil").exists()
    assert len(load_layers(source)[0][0].features) == 1


def test_shapefile_wins_over_kml_in_same_zip(run, parcels):
    source, _ = run(make_zip(dict(parcels) | {"notes.kml": FIELDS_KML.encode()}))
    assert source.format is SourceFormat.SHAPEFILE
    assert "ignored_entries" in codes(source.issues)


@pytest.mark.parametrize("keep_cpg", [True, False])
def test_legacy_cp1252_attributes(run, tmp_path, keep_cpg):
    shp = write_shapefile(
        tmp_path / "src",
        "cafes",
        [box(0, 0, 1, 1)],
        fields={"name": ["Café Zürich"]},
        encoding="cp1252",
    )
    members = shapefile_members(shp)
    assert b"Caf\xe9 Z\xfcrich" in members["cafes.dbf"]  # really single-byte, not UTF-8
    if not keep_cpg:
        members.pop("cafes.cpg", None)  # older exports often have no .cpg at all
    source, _ = run(make_zip(members))
    assert load_layers(source)[0][0].features[0].properties["name"] == "Café Zürich"


# --- KMZ ----------------------------------------------------------------------------------


def test_kmz_prefers_doc_kml(run):
    data = make_zip(
        {
            "other.kml": b"<kml/>",
            "doc.kml": FIELDS_KML.encode(),
            "files/icon.png": b"\x89PNG",
        }
    )
    source, _ = run(data, "survey.kmz")
    assert source.format is SourceFormat.KMZ
    layers, issues = load_layers(source)
    assert len(layers) == 2
    assert "ignored_entries" in codes(issues)


# --- Hostile archives ---------------------------------------------------------------------


def test_zip_bomb_refused(run):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.shp", b"\0" * (50 * 1024 * 1024))
    assert error_code(lambda: run(buf.getvalue())) == "archive_suspicious_compression"


def test_too_many_entries(run):
    data = make_zip({f"f{i}.txt": b"x" for i in range(20)})
    limits = ArchiveLimits(max_entries=10)
    assert error_code(lambda: run(data, limits=limits)) == "archive_too_many_entries"


def test_uncompressed_size_limit(run, parcels):
    limits = ArchiveLimits(max_uncompressed_bytes=100)
    assert error_code(lambda: run(make_zip(parcels), limits=limits)) == "archive_too_large"


def test_extraction_budget_counts_real_bytes(tmp_path):
    data = make_zip({"a.shp": b"x" * 5000})
    zf = zipfile.ZipFile(io.BytesIO(data))
    budget = ExtractionBudget(limit=1000)
    assert error_code(lambda: budget.extract(zf, zf.infolist()[0], tmp_path / "a")) == (
        "archive_too_large"
    )


def test_encrypted_entries_refused(run, parcels):
    data = bytearray(make_zip(parcels))
    pos = data.find(b"PK\x01\x02")
    while pos != -1:  # set the "encrypted" flag on every central-directory entry
        data[pos + 8] |= 0x1
        pos = data.find(b"PK\x01\x02", pos + 4)
    assert error_code(lambda: run(bytes(data))) == "archive_encrypted"
