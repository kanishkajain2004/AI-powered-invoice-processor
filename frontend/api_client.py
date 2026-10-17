"""HTTP client for the invoice backend (/api/v1). The frontend's only data source."""

from dataclasses import dataclass
from typing import Any

import requests

PROCESS_TIMEOUT = 180  # the backend processes synchronously; OCR can take a while
READ_TIMEOUT = 15


@dataclass
class BackendError(Exception):
    """A non-2xx response, carrying the backend's error envelope."""

    status_code: int
    code: str
    message: str
    details: Any = None
    run: dict | None = None  # set when the backend created a run that then failed

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class BackendUnavailable(BackendError):
    pass


def normalize_base_url(url: str) -> str:
    """Accept "https://api.example.com/", "api.example.com" or "host:port" (no scheme)."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("API_BASE_URL is empty")
    if url.endswith("/api/v1"):
        url = url[: -len("/api/v1")]
    return url if "://" in url else f"http://{url}"


class ApiClient:
    def __init__(self, base_url: str):
        self.base_url = normalize_base_url(base_url)
        self.api = f"{self.base_url}/api/v1"

    def _request(self, method: str, path: str, *, timeout: float = READ_TIMEOUT, **kwargs) -> Any:
        try:
            response = requests.request(method, f"{self.api}{path}", timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            raise BackendUnavailable(0, "backend_unavailable",
                                     f"Cannot reach the backend at {self.base_url} ({type(exc).__name__}).") from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.ok:
            return body
        error = (body or {}).get("error") or {}
        raise BackendError(
            status_code=response.status_code,
            code=error.get("code", "http_error"),
            message=error.get("message", f"The backend returned HTTP {response.status_code}."),
            details=error.get("details"),
            run=(body or {}).get("run"),
        )

    def health(self) -> dict:
        return self._request("GET", "/health", timeout=5)

    def stats(self) -> dict:
        return self._request("GET", "/runs/stats")

    def list_runs(self, *, status: str | None = None, decision: str | None = None,
                  page: int = 1, page_size: int = 20) -> dict:
        params = {"page": page, "page_size": page_size}
        if status:
            params["status"] = status
        if decision:
            params["decision"] = decision
        return self._request("GET", "/runs", params=params)

    def get_run(self, run_id: str) -> dict:
        return self._request("GET", f"/runs/{run_id}")

    def get_result(self, run_id: str) -> dict:
        return self._request("GET", f"/runs/{run_id}/result")

    def process(self, file_name: str, content: bytes, content_type: str = "application/pdf") -> dict:
        return self._request("POST", "/invoices/process", timeout=PROCESS_TIMEOUT,
                             files={"file": (file_name, content, content_type or "application/pdf")})
