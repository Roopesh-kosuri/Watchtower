"""
Live REST API + SSE tests -- runs the real FastAPI app via a real
uvicorn.Server in a background thread (not TestClient's mocked
transport), so this exercises actual HTTP, actual SSE streaming, and the
real startup sequence, including auth wiring.

Note on structure: app/main.py loads and validates config at IMPORT TIME
(see its module docstring), not inside a fixture -- this is deliberate
(a bad config should fail before the process looks like it's starting).
The consequence for testing is that the config path has to be known
BEFORE `import app.main` runs, which happens at module collection time,
before any pytest fixture (including tmp_path) is available. So this
suite uses a fixed config file (pytest_api_config.yaml) and a fixed db
path cleaned up at module setup, rather than a per-test temp path.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_PATH = str(PROJECT_ROOT / "tests" / "pytest_api_config.yaml")
SCHEMA_PATH = str(PROJECT_ROOT / "schema" / "001_init.sql")
DB_PATH = PROJECT_ROOT / "tests" / "data" / "pytest_api_test.db"

os.environ["WATCHTOWER_CONFIG"] = CONFIG_PATH
os.environ["WATCHTOWER_SCHEMA"] = SCHEMA_PATH

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402

import app.main as watchtower_main  # noqa: E402


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    for ext in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + ext)
        if p.exists():
            p.unlink()

    port = _find_free_port()
    config = uvicorn.Config(watchtower_main.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                break
        except httpx.RequestError:
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError("server did not become healthy in time")

    yield url

    server.should_exit = True
    thread.join(timeout=5)


def test_health_requires_no_auth(base_url):
    resp = httpx.get(f"{base_url}/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_services_returns_configured_services(base_url):
    resp = httpx.get(f"{base_url}/api/services")
    assert resp.status_code == 200
    names = {s["name"] for s in resp.json()}
    assert names == {"api-test-svc", "api-test-logs"}


def test_get_service_404_for_invalid_id(base_url):
    resp = httpx.get(f"{base_url}/api/services/99999")
    assert resp.status_code == 404


def test_metrics_huge_time_range_does_not_crash(base_url):
    services = httpx.get(f"{base_url}/api/services").json()
    svc_id = next(s["id"] for s in services if s["name"] == "api-test-svc")
    resp = httpx.get(f"{base_url}/api/services/{svc_id}/metrics", params={"since": "2000-01-01T00:00:00Z"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_metrics_no_data_in_range_returns_empty_not_error(base_url):
    services = httpx.get(f"{base_url}/api/services").json()
    svc_id = next(s["id"] for s in services if s["name"] == "api-test-svc")
    resp = httpx.get(f"{base_url}/api/services/{svc_id}/metrics", params={"since": "2099-01-01T00:00:00Z"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_metrics_malformed_date_returns_400(base_url):
    services = httpx.get(f"{base_url}/api/services").json()
    svc_id = next(s["id"] for s in services if s["name"] == "api-test-svc")
    resp = httpx.get(f"{base_url}/api/services/{svc_id}/metrics", params={"since": "not-a-date"})
    assert resp.status_code == 400


def test_metrics_on_log_service_returns_400(base_url):
    services = httpx.get(f"{base_url}/api/services").json()
    svc_id = next(s["id"] for s in services if s["name"] == "api-test-logs")
    resp = httpx.get(f"{base_url}/api/services/{svc_id}/metrics")
    assert resp.status_code == 400


def test_logs_on_http_service_returns_400(base_url):
    services = httpx.get(f"{base_url}/api/services").json()
    svc_id = next(s["id"] for s in services if s["name"] == "api-test-svc")
    resp = httpx.get(f"{base_url}/api/services/{svc_id}/logs")
    assert resp.status_code == 400


def test_push_log_ingestion_and_search_roundtrip(base_url):
    resp = httpx.post(
        f"{base_url}/ingest/logs/api-test-logs",
        json={"lines": ['{"level": "ERROR", "message": "pytest roundtrip error"}']},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ingested": 1}

    resp = httpx.get(f"{base_url}/api/logs", params={"q": "pytest roundtrip"})
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 1
    assert results[0]["level"] == "ERROR"
    assert results[0]["service_name"] == "api-test-logs"


def test_incidents_list_status_filter_rejects_invalid_value(base_url):
    resp = httpx.get(f"{base_url}/api/incidents", params={"status": "not-a-real-status"})
    assert resp.status_code == 400


def test_sse_stream_receives_a_real_pushed_log_event(base_url):
    received = []
    with httpx.stream("GET", f"{base_url}/api/events", timeout=10) as response:
        assert response.status_code == 200

        def push_soon():
            time.sleep(0.3)
            httpx.post(
                f"{base_url}/ingest/logs/api-test-logs",
                json={"lines": ['{"level": "INFO", "message": "sse live test line"}']},
            )

        threading.Thread(target=push_soon, daemon=True).start()

        buffer = ""
        deadline = time.time() + 8
        for chunk in response.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                if frame.strip() and not frame.startswith(":"):
                    received.append(frame)
            if received or time.time() > deadline:
                break

    assert len(received) >= 1
    assert any("sse live test line" in f or "log_event" in f for f in received)
