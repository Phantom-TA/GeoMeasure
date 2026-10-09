from datetime import date

import pytest
from pyproj import CRS
from shapely.geometry import box

from app.geo.readers import ReadError, ShapefileSource, read_kml, read_shapefile
from app.geo.types import CrsSource
from tests.samples import FIELDS_KML, write_shapefile


@pytest.fixture
def kml_path(tmp_path):
    p = tmp_path / "survey.kml"
    p.write_text(FIELDS_KML, encoding="utf-8")
    return p


def test_kml_folders_become_layers(kml_path):
    layers = read_kml(kml_path)
    assert [layer.name for layer in layers] == ["Fields", "Infrastructure"]
    assert [f.index for layer in layers for f in layer.features] == [0, 1, 2, 3]
    assert all(layer.crs.to_epsg() == 4326 for layer in layers)
    assert all(layer.crs_source is CrsSource.FILE for layer in layers)


def test_kml_properties_are_cleaned(kml_path):
    field = read_kml(kml_path)[0].features[0]
    assert field.source_id == "field-1"
    assert field.properties == {"Name": "North field", "crop": "wheat"}
    assert field.geometry_type == "Polygon"


def test_kml_geometry_types(kml_path):
    infra = read_kml(kml_path)[1].features
    assert [f.geometry_type for f in infra] == ["LineString", "Point", "GeometryCollection"]
    assert infra[0].source_id is None


def test_kml_garbage(tmp_path):
    bad = tmp_path / "bad.kml"
    bad.write_text("<kml><Document><Placemark>", encoding="utf-8")
    with pytest.raises(ReadError):
        read_kml(bad)


def test_shapefile_with_attributes(tmp_path):
    shp = write_shapefile(
        tmp_path,
        "parcels",
        [box(77, 28, 77.01, 28.01), box(78, 28, 78.01, 28.01)],
        fields={"owner": ["A", "B"], "acres": [2.5, None], "surveyed": [date(2024, 1, 5), None]},
    )
    layer = read_shapefile(ShapefileSource("parcels", shp), start_index=10)
    assert layer.crs.to_epsg() == 4326 and layer.crs_source is CrsSource.FILE
    f0, f1 = layer.features
    assert (f0.index, f0.source_id) == (10, "0")
    assert f0.properties == {"owner": "A", "acres": 2.5, "surveyed": "2024-01-05"}
    assert f1.properties["acres"] is None


def test_shapefile_projected(tmp_path):
    shp = write_shapefile(
        tmp_path, "utm", [box(500000, 3100000, 501000, 3101000)], crs="EPSG:32643"
    )
    assert read_shapefile(ShapefileSource("utm", shp)).crs.to_epsg() == 32643


def test_missing_prj_with_lonlat_coords_is_assumed(tmp_path):
    shp = write_shapefile(tmp_path, "noprj", [box(77, 28, 77.01, 28.01)])
    shp.with_suffix(".prj").unlink()
    layer = read_shapefile(ShapefileSource("noprj", shp))
    assert layer.crs.to_epsg() == 4326 and layer.crs_source is CrsSource.ASSUMED
    assert layer.issues[0].code == "crs_assumed"


def test_missing_prj_with_projected_coords_is_unknown(tmp_path):
    shp = write_shapefile(tmp_path, "noprj", [box(500000, 3100000, 501000, 3101000)])
    shp.with_suffix(".prj").unlink()
    layer = read_shapefile(ShapefileSource("noprj", shp))
    assert layer.crs is None and layer.issues[0].code == "crs_unknown"


def test_crs_override(tmp_path):
    shp = write_shapefile(tmp_path, "noprj", [box(500000, 3100000, 501000, 3101000)])
    shp.with_suffix(".prj").unlink()
    layer = read_shapefile(ShapefileSource("noprj", shp), crs_override=CRS.from_epsg(32643))
    assert layer.crs.to_epsg() == 32643 and layer.crs_source is CrsSource.OVERRIDE


def test_missing_shx_is_rebuilt(tmp_path):
    shp = write_shapefile(tmp_path, "noshx", [box(77, 28, 77.01, 28.01)])
    shp.with_suffix(".shx").unlink()
    assert len(read_shapefile(ShapefileSource("noshx", shp)).features) == 1


def test_missing_dbf_reads_geometry_only(tmp_path):
    shp = write_shapefile(tmp_path, "nodbf", [box(77, 28, 77.01, 28.01)])
    shp.with_suffix(".dbf").unlink()
    layer = read_shapefile(ShapefileSource("nodbf", shp))
    assert layer.features[0].properties == {}
    assert layer.features[0].geometry is not None


def test_null_geometry_kept_as_feature(tmp_path):
    shp = write_shapefile(tmp_path, "nulls", [box(77, 28, 77.01, 28.01), None])
    features = read_shapefile(ShapefileSource("nulls", shp)).features
    assert len(features) == 2 and features[1].geometry is None


def test_google_earth_export(tmp_path):
    from tests.samples import GOOGLE_EARTH_KML

    p = tmp_path / "ge.kml"
    p.write_text(GOOGLE_EARTH_KML, encoding="utf-8")
    plot, track = read_kml(p)[0].features  # the NetworkLink is not followed
    assert plot.properties == {"Name": "Plot 7", "plot_no": 7, "area_ha": 1.25}
    assert track.geometry_type == "LineString"  # gx:Track becomes a line
    assert track.properties["begin"] == "2024-05-01T10:00:00Z"
    assert track.properties["end"] == "2024-05-01T10:02:00Z"
