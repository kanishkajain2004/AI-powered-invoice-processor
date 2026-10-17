"""Container test for the API image (Dockerfile): Tesseract is present and scanned PDFs are
OCR'd and validated inside the container, exactly as on Render.

Building the image takes minutes, so this only runs on request:

    RUN_DOCKER_TESTS=1 pytest tests/test_docker.py -v
"""

import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "samples"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_TESTS") != "1" or not _docker_ready(),
    reason="set RUN_DOCKER_TESTS=1 with a running Docker daemon to build and test the API image",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def api():
    tag = "zamp-invoice-api:test"
    subprocess.run(["docker", "build", "-t", tag, str(ROOT)], check=True)
    name, port = f"invoice-api-test-{uuid.uuid4().hex[:8]}", _free_port()
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:10000",
                    "-e", "PORT=10000", "-e", "SEED_ON_STARTUP=true", tag], check=True, capture_output=True)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            try:
                if requests.get(f"{base}/api/v1/health", timeout=2).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.5)
        else:
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True).stdout
            pytest.fail(f"container did not become healthy:\n{logs[-2000:]}")
        yield {"base": f"{base}/api/v1", "name": name}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _process(api, path: Path) -> requests.Response:
    with path.open("rb") as handle:
        return requests.post(f"{api['base']}/invoices/process", timeout=180,
                             files={"file": (path.name, handle, "application/pdf")})


def test_ocr_is_available_in_the_image(api):
    health = requests.get(f"{api['base']}/health", timeout=10).json()
    assert health["ocr"] == "available"
    assert health["ocr_engine"].startswith("tesseract ")


def test_seed_purchase_orders_exist(api):
    assert len(requests.get(f"{api['base']}/purchase-orders", timeout=10).json()) == 4


def test_scanned_pdf_is_ocrd_and_validated(api):
    response = _process(api, SAMPLES / "05_scanned_INV-1003.pdf")

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["status"], body["decision"]) == ("COMPLETED", "APPROVED")
    assert body["invoice"]["invoice_number"] == "INV-1003"
    assert body["invoice"]["total_amount"] == "80000.00"
    detail = requests.get(f"{api['base']}/runs/{body['run_id']}", timeout=10).json()
    assert detail["extraction"]["method"] == "ocr"


def test_text_pdf_still_uses_the_text_layer(api):
    body = _process(api, SAMPLES / "01_happy_path_INV-1001.pdf").json()

    assert body["decision"] == "APPROVED"
    detail = requests.get(f"{api['base']}/runs/{body['run_id']}", timeout=10).json()
    assert detail["extraction"]["method"] == "text"


def test_container_runs_as_an_unprivileged_user(api):
    uid = subprocess.run(["docker", "exec", api["name"], "id", "-u"], capture_output=True, text=True).stdout
    assert uid.strip() == "10001"
