"""Deployment configuration: storage settings, seeding at startup, URL handling and the
start scripts that Render runs (they must honour $PORT and $HOST)."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.config import Settings, get_settings
from frontend.api_client import normalize_base_url

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(url: str, timeout: float = 60) -> requests.Response:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.get(url, timeout=2)
            if response.ok:
                return response
        except requests.RequestException:
            pass
        time.sleep(0.2)
    pytest.fail(f"{url} did not become healthy")


# --- settings ------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_storage_env(monkeypatch):
    """The suite sets DATABASE_URL/UPLOAD_DIR globally; these tests need them unset."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("UPLOAD_DIR", raising=False)


def test_data_dir_places_database_and_uploads(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    assert settings.database_url == f"sqlite:///{(tmp_path / 'data').resolve() / 'invoices.db'}"
    assert settings.upload_dir == (tmp_path / "data").resolve() / "uploads"


def test_explicit_paths_win_over_data_dir(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, database_url="sqlite:////elsewhere/x.db",
                        upload_dir=tmp_path / "up")
    assert settings.database_url == "sqlite:////elsewhere/x.db"
    assert settings.upload_dir == tmp_path / "up"


def test_data_dir_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("UPLOAD_DIR", raising=False)
    settings = Settings(_env_file=None)
    assert settings.database_url == f"sqlite:///{tmp_path.resolve() / 'invoices.db'}"


def test_blank_environment_variables_mean_default(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("SEED_ON_STARTUP", "")
    settings = Settings(_env_file=None)
    assert settings.database_url.startswith("sqlite:///") and settings.database_url.endswith("invoices.db")
    assert settings.seed_on_startup is False


def test_defaults_are_local_development_values(monkeypatch):
    for key in ("DATA_DIR", "SEED_ON_STARTUP", "API_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)
    assert settings.seed_on_startup is False
    assert settings.api_base_url == "http://localhost:8000"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("http://localhost:8000", "http://localhost:8000"),
        ("https://zamp-invoice-api.onrender.com/", "https://zamp-invoice-api.onrender.com"),
        ("https://zamp-invoice-api.onrender.com/api/v1", "https://zamp-invoice-api.onrender.com"),
        ("zamp-invoice-api:10000", "http://zamp-invoice-api:10000"),
        ("  http://api:8000  ", "http://api:8000"),
    ],
)
def test_api_base_url_normalization(raw, expected):
    assert normalize_base_url(raw) == expected


def test_empty_api_base_url_is_refused():
    with pytest.raises(ValueError):
        normalize_base_url("  ")


# --- app startup ---------------------------------------------------------------------------------


def test_root_route_points_to_docs_and_health(client):
    body = client.get("/").json()
    assert body["docs"] == "/docs" and body["health"] == "/api/v1/health"


def test_seed_on_startup_is_idempotent(monkeypatch):
    from app.main import app

    monkeypatch.setattr(get_settings(), "seed_on_startup", True)
    for _ in range(2):  # two restarts: the second must not duplicate anything
        with TestClient(app) as client:
            pos = client.get("/api/v1/purchase-orders").json()
    assert sorted(po["po_number"] for po in pos) == ["PO-5001", "PO-5002", "PO-5003", "PO-5004"]


def test_concurrent_seed_does_not_break_startup(monkeypatch):
    from app import main

    def raced(_db):
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(get_settings(), "seed_on_startup", True)
    monkeypatch.setattr(main, "seed_purchase_orders", raced)
    with TestClient(main.app) as client:
        assert client.get("/api/v1/health").status_code == 200


# --- the start scripts Render runs ------------------------------------------------------------------


@pytest.fixture
def started():
    procs = []

    def start(script: str, **env) -> None:
        env = {"PYTHON": sys.executable} | env  # the interpreter running the suite has the dependencies
        procs.append(subprocess.Popen(["sh", f"scripts/{script}"], cwd=ROOT, env=os.environ | env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    yield start
    for proc in procs:
        proc.terminate()
        proc.wait(timeout=15)


def test_start_scripts_bind_render_port_and_serve(started, tmp_path):
    api_port, ui_port = _free_port(), _free_port()
    data_dir = tmp_path / "render-data"

    started("start_api.sh", PORT=str(api_port), HOST="127.0.0.1", DATA_DIR=str(data_dir),
            SEED_ON_STARTUP="true", DATABASE_URL="", UPLOAD_DIR="")
    health = _wait_for(f"http://127.0.0.1:{api_port}/api/v1/health").json()
    assert (health["status"], health["database"]) == ("ok", "ok")
    assert len(requests.get(f"http://127.0.0.1:{api_port}/api/v1/purchase-orders", timeout=5).json()) == 4
    assert (data_dir / "invoices.db").exists() and (data_dir / "uploads").is_dir()

    # The UI gets the API address the way Render's private network gives it: host:port, no scheme.
    started("start_ui.sh", PORT=str(ui_port), HOST="127.0.0.1", API_BASE_URL=f"127.0.0.1:{api_port}")
    assert _wait_for(f"http://127.0.0.1:{ui_port}/_stcore/health").text == "ok"


def test_start_scripts_default_to_all_interfaces():
    for script in ("start_api.sh", "start_ui.sh"):
        text = (ROOT / "scripts" / script).read_text()
        assert '"${HOST:-0.0.0.0}"' in text and "${PORT:-" in text
