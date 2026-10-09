import io

import pytest

from app.ingest.errors import IngestError
from app.ingest.upload import clean_filename, save_upload


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("survey.kml", "survey.kml"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\Desktop\\fields.zip", "fields.zip"),
        ("bad\x00name\r\n.kml", "badname.kml"),
        ("", "upload"),
        (None, "upload"),
        ("a" * 300 + ".kml", "a" * 255),
    ],
)
def test_clean_filename(raw, clean):
    assert clean_filename(raw) == clean


def test_save_upload_hashes_and_sizes(tmp_path):
    size, digest = save_upload(io.BytesIO(b"hello"), tmp_path / "f", max_bytes=10)
    assert size == 5
    assert digest == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_save_upload_enforces_limit_while_streaming(tmp_path):
    with pytest.raises(IngestError) as exc:
        save_upload(io.BytesIO(b"x" * 5000), tmp_path / "f", max_bytes=1000)
    assert exc.value.code == "file_too_large"


def test_save_upload_rejects_empty(tmp_path):
    with pytest.raises(IngestError) as exc:
        save_upload(io.BytesIO(b""), tmp_path / "f", max_bytes=1000)
    assert exc.value.code == "empty_file"
