from pathlib import Path

from fastapi.testclient import TestClient

from literature_workbench.api.app import create_app
from literature_workbench.config import AppSettings


def test_health_reports_injected_version_and_loopback_scope(tmp_path: Path) -> None:
    settings = AppSettings(version="0.1.0-test", data_root=tmp_path)
    client = TestClient(create_app(settings))
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "0.1.0-test",
        "network_scope": "loopback-only",
    }
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
