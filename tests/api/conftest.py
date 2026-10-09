from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.samples import FIELDS_KML


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data", worker_mode="thread", workers=2, max_wait_seconds=15
    )


@pytest.fixture
def make_client(settings) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _make(job_fn: Any = None, **overrides: Any) -> TestClient:
        s = settings.model_copy(update=overrides)
        app = create_app(s) if job_fn is None else create_app(s, job_fn)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def upload(
    client: TestClient,
    data: bytes,
    filename: str = "survey.kml",
    *,
    wait: bool = True,
    **params: str,
):
    headers = {"Prefer": "wait=15"} if wait else {}
    return client.post(
        "/api/files/", files={"file": (filename, data)}, params=params, headers=headers
    )


def wait_for(client: TestClient, file_id: str, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/files/{file_id}").json()
        if body["status"] in ("COMPLETED", "FAILED"):
            return body
        time.sleep(0.1)
    raise AssertionError(f"file {file_id} did not finish: {body}")


@pytest.fixture
def kml_file(client) -> dict[str, Any]:
    res = upload(client, FIELDS_KML.encode())
    assert res.status_code == 201, res.text
    return res.json()
