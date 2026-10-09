import json
import logging

from app.core.logging import JsonFormatter, request_id_var


def test_request_id_generated_and_echoed(client):
    res = client.get("/health")
    assert len(res.headers["x-request-id"]) == 16


def test_request_id_propagated_from_client(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.access"):
        res = client.get("/health", headers={"X-Request-ID": "trace-abc.123"})
    assert res.headers["x-request-id"] == "trace-abc.123"
    assert any("GET /health -> 200" in r.getMessage() for r in caplog.records)


def test_unsafe_request_id_replaced(client):
    res = client.get("/health", headers={"X-Request-ID": "x" * 100})
    assert res.headers["x-request-id"] != "x" * 100


def test_request_id_on_rejected_upload(make_client):
    small = make_client(max_upload_bytes=100)
    res = small.post("/api/files", files={"file": ("a.kml", b"x" * 100_000)})
    assert res.status_code == 413 and "x-request-id" in res.headers


def test_json_log_format():
    token = request_id_var.set("req-1")
    try:
        record = logging.LogRecord("app", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        record.request_id = request_id_var.get()
        entry = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert entry["message"] == "hello world"
    assert entry["request_id"] == "req-1"
    assert entry["level"] == "INFO"
