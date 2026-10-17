"""Smoke tests: every module imports, the DB initialises, the API responds."""

import importlib

import pymupdf
import pytest

MODULES = [
    "app.config",
    "app.main",
    "app.api",
    "app.db",
    "app.db.models",
    "app.db.seed",
    "app.schemas",
    "app.extraction",
    "app.normalization",
    "app.matching",
    "app.duplicates",
    "app.validation",
    "app.workflow",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)


def test_health(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_list_runs_empty(client):
    resp = client.get("/api/v1/runs")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_extract_text_from_generated_pdf():
    from app.extraction import extract_text

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "INVOICE INV-001")
    pdf_bytes = doc.tobytes()
    doc.close()

    assert "INV-001" in extract_text(pdf_bytes)
