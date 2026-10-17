"""Demo tooling: reset a dedicated demo database, then run the samples through the live API.

    export DATABASE_URL=sqlite:///$PWD/data/demo.db UPLOAD_DIR=$PWD/data/demo_uploads
    python -m scripts.demo reset            # fresh demo database with the seed POs
    uvicorn app.main:app                    # (same environment)
    python -m scripts.demo run              # POST each sample in order and check the outcome

`reset` only works on a SQLite database whose file name starts with "demo", so it can
never wipe the development database. `run` checks what the API actually returns
against data/samples/manifest.json; it makes no decisions itself.
"""

import argparse
import shutil
import sys
from pathlib import Path

import requests
from sqlalchemy.engine import make_url

from app.config import get_settings
from scripts.generate_samples import SAMPLES_DIR, load_manifest

REQUIRED_POS = {"PO-5001", "PO-5002", "PO-5004"}


def _demo_paths() -> tuple[Path, Path]:
    settings = get_settings()
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        sys.exit("reset: DATABASE_URL must point to a SQLite file (e.g. sqlite:///$PWD/data/demo.db).")
    db_path = Path(url.database).resolve()
    if not db_path.name.startswith("demo"):
        sys.exit(f"reset: refusing to reset {db_path.name}: the demo database file name must start with 'demo'.\n"
                 "       export DATABASE_URL=sqlite:///$PWD/data/demo.db UPLOAD_DIR=$PWD/data/demo_uploads")
    upload_dir = settings.upload_dir.resolve()
    if not upload_dir.name.startswith("demo"):
        sys.exit(f"reset: refusing to clear {upload_dir}: the demo upload directory name must start with 'demo'.")
    return db_path, upload_dir


def reset() -> None:
    db_path, upload_dir = _demo_paths()
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    shutil.rmtree(upload_dir, ignore_errors=True)
    upload_dir.mkdir(parents=True)

    from app.db.database import SessionLocal, init_db
    from app.db.seed import seed_purchase_orders

    init_db()
    with SessionLocal() as db:
        result = seed_purchase_orders(db)
    print(f"Demo database reset: {db_path}")
    print(f"Upload directory cleared: {upload_dir}")
    print(f"Seeded {len(result.pos_created)} purchase orders: {', '.join(result.pos_created)}")


def _check(sample: dict, status_code: int, body: dict, run_ids: dict[str, str]) -> tuple[str, list[str]]:
    """Return (which expectation matched, problems)."""
    run = body.get("run") or body  # a FAILED run comes back inside the error envelope
    actual = {
        "status": run.get("status"),
        "decision": run.get("decision"),
        "deciding_checks": body.get("deciding_checks"),
        "error_code": (body.get("error") or {}).get("code"),
        "failed_stage": (run.get("error") or {}).get("stage"),
        "http_status": status_code,
    }
    candidates = [("expected", sample["expected"])]
    if "expected_without_ocr" in sample:
        candidates.append(("expected_without_ocr", sample["expected_without_ocr"]))

    best = None
    for label, expected in candidates:
        problems = [f"{key}: expected {value!r}, got {actual.get(key)!r}"
                    for key, value in expected.items() if key != "duplicate_of" and actual.get(key) != value]
        if "duplicate_of" in expected and not problems:
            original = run_ids.get(expected["duplicate_of"])
            check = next((c for c in body.get("checks", []) if c["name"] == "duplicate_invoice"), {})
            referenced = [m["run_id"] for m in (check.get("evidence") or {}).get("matches", [])]
            if original is None:
                problems.append(f"{expected['duplicate_of']} was not processed earlier in this demo")
            elif referenced != [original]:
                problems.append(f"duplicate evidence references {referenced}, expected the earlier run {original}")
        if best is None or len(problems) < len(best[1]):
            best = (label, problems)
        if not problems:
            break
    return best


def run(api: str, allow_existing: bool, samples_dir: Path = SAMPLES_DIR) -> int:
    base = f"{api.rstrip('/')}/api/v1"
    try:
        requests.get(f"{base}/health", timeout=5).raise_for_status()
    except requests.RequestException as exc:
        sys.exit(f"run: backend not reachable at {api} ({exc}). Start it with `uvicorn app.main:app`.")

    existing = requests.get(f"{base}/runs/stats", timeout=10).json()["total_runs"]
    if existing and not allow_existing:
        sys.exit(f"run: the backend already has {existing} run(s). The demo needs a fresh database, or the happy "
                 "path would itself be a duplicate. Run `python -m scripts.demo reset` (and restart the backend), "
                 "or pass --allow-existing.")
    pos = {po["po_number"] for po in requests.get(f"{base}/purchase-orders", timeout=10).json()}
    if missing := REQUIRED_POS - pos:
        sys.exit(f"run: seed purchase orders missing: {sorted(missing)}. Run `python -m scripts.demo reset`.")

    run_ids: dict[str, str] = {}
    failures = 0
    print(f"{'#':<3}{'sample':<40}{'HTTP':<6}{'status':<11}{'decision':<14}result")
    for sample in load_manifest(samples_dir):
        path = samples_dir / sample["file"]
        with path.open("rb") as handle:
            response = requests.post(f"{base}/invoices/process", timeout=300,
                                     files={"file": (path.name, handle, "application/pdf")})
        body = response.json()
        run = body.get("run") or body
        run_ids[sample["file"]] = run.get("run_id")
        label, problems = _check(sample, response.status_code, body, run_ids)
        verdict = "ok" if not problems else "MISMATCH"
        note = " (server has no OCR)" if label == "expected_without_ocr" and not problems else ""
        print(f"{sample['order']:<3}{sample['file']:<40}{response.status_code:<6}{run.get('status') or '-':<11}"
              f"{run.get('decision') or '-':<14}{verdict}{note}")
        reason = body.get("decision_reason") or (body.get("error") or {}).get("message")
        if reason:
            print(f"   reason: {reason[:160]}")
        for problem in problems:
            print(f"   !! {problem}")
        failures += bool(problems)

    print(f"\n{len(run_ids) - failures}/{len(run_ids)} samples behaved as expected.")
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.demo", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("reset", help="recreate the demo database (file name must start with 'demo') and seed POs")
    run_parser = sub.add_parser("run", help="POST every sample to the API in order and check the outcomes")
    run_parser.add_argument("--api", default=get_settings().api_base_url, help="backend base URL")
    run_parser.add_argument("--allow-existing", action="store_true", help="run even if the backend has runs")
    run_parser.add_argument("--samples", type=Path, default=SAMPLES_DIR,
                            help="folder with the PDFs and manifest.json (default: the demo set; "
                                 "data/samples/extra holds edge cases)")
    args = parser.parse_args()
    if args.command == "reset":
        reset()
    else:
        sys.exit(run(args.api, args.allow_existing, args.samples))


if __name__ == "__main__":
    main()
