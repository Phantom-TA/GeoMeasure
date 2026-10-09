"""The committed sample files are what reviewers try first, so pin down what they produce."""

import json
from pathlib import Path

import pytest

from app.cli import main, measure
from app.geo.types import Strategy

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def run(name: str, crs: str | None = None, strategy: Strategy = Strategy.AUTO):
    rows, outcome = measure(SAMPLES / name, crs, strategy)
    return {
        r["properties"].get("Name") or r["properties"].get("name") or r["index"]: r for r in rows
    }, outcome


def codes(row):
    return {i["code"] for i in row["issues"]}


def test_farm_survey():
    rows, outcome = run("farm_survey.kml")
    assert outcome.feature_count == 5
    assert [layer["name"] for layer in outcome.layers] == ["Plots", "Infrastructure"]
    assert "geometry_repaired" in codes(rows["Disputed strip"])
    assert rows["Tube well"]["status"] == "not_applicable"
    assert rows["Farm road"]["length_m"] > 1000
    assert rows["Orchard"]["area_m2"] < rows["Orchard"]["geodesic_area_m2"] * 1.000001
    assert all(r["deviation_pct"] < 1e-4 for r in rows.values() if r["deviation_pct"])


def test_parcels_utm_measured_natively():
    rows, outcome = run("parcels_utm43n.zip")
    assert outcome.crs == "EPSG:32643" and outcome.feature_count == 12
    assert {r["method"] for r in rows.values()} == {"source"}


def test_parcels_without_prj_need_a_crs():
    rows, outcome = run("parcels_no_prj.zip")
    assert outcome.crs is None
    assert {r["status"] for r in rows.values()} == {"error"}
    rows, outcome = run("parcels_no_prj.zip", crs="EPSG:32643")
    assert {r["status"] for r in rows.values()} == {"ok"}


def test_web_mercator_matches_the_kml_original():
    merc, _ = run("fields_web_mercator.zip")
    kml, _ = run("farm_survey.kml")
    assert "source_crs_unsuitable" in codes(merc["North field"])
    assert merc["North field"]["area_m2"] == pytest.approx(kml["North field"]["area_m2"], rel=1e-6)


def test_pipeline_in_feet():
    rows, _ = run("pipeline_ny_feet.zip")
    row = rows["LI-7 main"]
    feet = 3500**2 + 2400**2
    expected_ft = feet**0.5 + (4500**2 + 500**2) ** 0.5
    assert row["method"] == "source"
    assert row["length_m"] == pytest.approx(expected_ft * 1200 / 3937)


def test_fiji_antimeridian():
    rows, outcome = run("fiji_antimeridian.kml")
    assert outcome.layers[0]["name"] == "fiji_antimeridian"  # named after the file
    row = rows["Taveuni parcel"]
    assert "antimeridian_normalized" in codes(row)
    assert row["area_m2"] < 2_000_000  # ~1.2 km2, not a planet-sized polygon


# --- CLI ----------------------------------------------------------------------------------


def test_cli_table(capsys):
    assert main(["measure", str(SAMPLES / "farm_survey.kml")]) == 0
    out = capsys.readouterr().out
    assert "geometry_type" in out and "5 features (kml, EPSG:4326)" in out


def test_cli_json(capsys):
    assert main(["measure", str(SAMPLES / "parcels_utm43n.zip"), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["features"]) == 12 and "geometry" not in data["features"][0]


def test_cli_rejects_bad_input(capsys, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("hello")
    assert main(["measure", str(bad)]) == 1
    assert "Unsupported file" in capsys.readouterr().err
