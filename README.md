# AI-Powered Invoice Processing (Zamp PS-1)

Accounts-payable teams receive invoices as PDFs and must decide, for each one, whether it can be paid. Does it match an approved purchase order? Is it a duplicate? Do the amounts add up? This application automates that decision.

You upload an invoice PDF. The application extracts and normalizes its fields, checks it for duplicates, matches it to a purchase order and validates it. It then records one of three decisions, each with its evidence: **APPROVED**, **NEEDS_REVIEW** (sent to a human) or **REJECTED** (a confirmed duplicate).

Everything runs locally: FastAPI, Streamlit, SQLite and PyMuPDF, plus Tesseract for scanned invoices if you install it. **No AI or LLM API key is needed.** Extraction and every business rule are deterministic code.

---

## Quick start

Requires **Python 3.12** (the version the project was built and tested with).

```bash
cd Zamp.ai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt         # runtime + test tools

python -m app.db.seed                       # create data/invoices.db and the seed purchase orders
uvicorn app.main:app                        # terminal 1: API on http://localhost:8000
streamlit run frontend/streamlit_app.py     # terminal 2: UI on http://localhost:8501
```

| What | URL |
|---|---|
| Web UI | http://localhost:8501 |
| API documentation (Swagger UI) | http://localhost:8000/docs |
| API base path | http://localhost:8000/api/v1 |
| Health check | http://localhost:8000/api/v1/health |

To run the five demo invoices on their own clean database, see [Demo](#demo).

---

## Setup in detail

### 1. Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

- **`requirements.txt`** is what the app needs at runtime, pinned to the versions the tests pass with: FastAPI, uvicorn, python-multipart, SQLAlchemy 2, Pydantic 2, pydantic-settings, PyMuPDF, pytesseract, Pillow, Streamlit, requests and pandas. This is what Render installs.
- **`requirements-dev.txt`** adds the test tools, pytest and httpx.
- **`.python-version`** pins Python 3.12, for Render and for tools like pyenv.

### 2. Optional: OCR for scanned PDFs

Text PDFs need nothing more. Scanned (image-only) PDFs are read with the Tesseract binary, installed separately:

```bash
brew install tesseract                 # macOS
sudo apt-get install tesseract-ocr     # Debian/Ubuntu
tesseract --version                    # check it is on PATH
```

Restart the backend after installing it. If Tesseract is not on `PATH`, set `TESSERACT_CMD` to its full path. Without it, a scanned upload is recorded as a **processing failure**, not a business decision. The API returns HTTP 503 with error code `ocr_unavailable` and a message explaining how to install it.

### 3. Configuration (optional)

Every setting has a working default. To override one, export it as an environment variable, or copy `.env.example` to `.env` and edit it there. A variable set to an empty value counts as unset. Nothing in the configuration is secret.

| Variable | Default | Purpose |
|---|---|---|
| `DATA_DIR` | *(unset)* | One directory for both the database (`invoices.db`) and the uploads (`uploads/`), e.g. a mounted disk |
| `DATABASE_URL` | `sqlite:///<project>/data/invoices.db` | SQLite database (absolute path). Overrides `DATA_DIR` |
| `UPLOAD_DIR` | `<project>/data/uploads` | Where uploaded PDFs are stored, as `<run_id>.pdf`. Overrides `DATA_DIR` |
| `SEED_ON_STARTUP` | `false` | Create the seed POs at backend startup if they are missing (safe to leave on) |
| `API_BASE_URL` | `http://localhost:8000` | Where the Streamlit UI (and `scripts.demo run`) find the backend. `https://host`, `host` or `host:port` all work |
| `HOST` / `PORT` | `0.0.0.0` / `8000` (API), `8501` (UI) | Used by `scripts/start_api.sh` and `scripts/start_ui.sh`; Render sets `PORT` |
| `MAX_UPLOAD_MB` | `20` | Largest accepted upload. Keep it equal to `server.maxUploadSize` in `.streamlit/config.toml` |
| `MAX_PAGES` | `20` | Largest accepted PDF |
| `AMOUNT_TOLERANCE_PCT` | `1.0` | Allowed difference between the invoice total and the PO amount, as % of the PO amount |
| `ARITHMETIC_TOLERANCE` | `0.01` | Rounding allowance for subtotal + tax = total and for line arithmetic |
| `DATE_ORDER` | `DMY` | How numeric dates are read: `DMY` (03/04/2026 = 3 April) or `MDY` |
| `MIN_TEXT_CHARS_PER_PAGE` | `30` | A page with less text than this, but with images or drawings, is treated as scanned |
| `TESSERACT_CMD` | *(unset)* | Path to the tesseract binary, if it is not on `PATH` |
| `OCR_LANG` / `OCR_DPI` | `eng` / `300` | Tesseract language and render resolution |
| `LOG_LEVEL` | `INFO` | Level of the application's server-side log |

### 4. Database and seed data

```bash
python -m app.db.seed
```

This creates the tables, if they are missing, and four purchase orders:

| PO | Vendor | Amount | Status |
|---|---|---|---|
| PO-5001 | Acme Office Supplies Pvt Ltd | INR 50,000.00 | APPROVED |
| PO-5002 | Bluewave Technologies Pvt Ltd | INR 120,000.00 | APPROVED |
| PO-5003 | Northstar Services Pvt Ltd | INR 75,000.00 | CLOSED |
| PO-5004 | Acme Office Supplies Pvt Ltd | INR 80,000.00 | APPROVED |

The seed can be re-run any number of times. Existing vendors and POs are matched by name and PO number, and are never duplicated or overwritten. The backend also creates missing tables at startup. Data persists across restarts.

### 5. Start the backend and the frontend

```bash
uvicorn app.main:app                        # add --reload while developing
streamlit run frontend/streamlit_app.py     # in a second terminal, same virtualenv
```

These listen on `localhost` only. To run exactly as in deployment (listening on all interfaces, port from `$PORT`), use `sh scripts/start_api.sh` and `sh scripts/start_ui.sh`.

### 6. Run the tests

```bash
pytest                              # the whole suite
pytest tests/test_api.py -v         # just the API integration tests
pytest tests/test_frontend.py       # the UI against a real backend process (it starts its own)
```

The tests use temporary SQLite databases and upload directories, so they never touch `data/`. Three tests use real OCR and are skipped when Tesseract is not installed. The "OCR unavailable" path is always tested, against a backend whose Tesseract is deliberately unreachable.

---

## Demo

Five realistic invoice PDFs in `data/samples/` cover each outcome. **Process them in their numbered order, on a fresh database.** The duplicate (#2) is rejected because #1 was approved first.

| # | File | Expected result |
|---|---|---|
| 1 | `01_happy_path_INV-1001.pdf`: Acme, PO-5001, INR 50,000.00 | **APPROVED** |
| 2 | `02_duplicate_resend_INV-1001.pdf`: the same invoice re-sent as a copy (a different file) | **REJECTED**, a confirmed duplicate of run #1 |
| 3 | `03_amount_mismatch_INV-1002.pdf`: INR 55,000.00 against the INR 50,000.00 PO-5001 | **NEEDS_REVIEW** (`amount_within_tolerance`) |
| 4 | `04_missing_invoice_number_PO-5002.pdf`: the *Bill No.* left blank | **NEEDS_REVIEW** (`required_fields`) |
| 5 | `05_scanned_INV-1003.pdf`: an image-only scan, PO-5004, INR 80,000.00 | With Tesseract: **APPROVED**. Without it: **processing FAILED** (`ocr_unavailable`) |

```bash
export DATABASE_URL="sqlite:///$PWD/data/demo.db" UPLOAD_DIR="$PWD/data/demo_uploads"
python -m scripts.demo reset                 # fresh demo DB + seed POs; refuses any DB not named demo*
uvicorn app.main:app                         # terminal 1 (same two variables exported)
streamlit run frontend/streamlit_app.py      # terminal 2; then upload 01 → 05 at http://localhost:8501
python -m scripts.demo run                   # or: POST 01 → 05 to the live API and check every outcome
```

**More test files.** `data/samples/extra/` holds seven edge cases. Each is independent, so they can be uploaded in any order, before or after the demo set:

| File | Expected result |
|---|---|
| `06_within_tolerance_INV-2001.pdf`: INR 50,400.00 against the INR 50,000.00 PO-5001 (0.8% over) | **APPROVED** |
| `07_closed_po_NS-2026-118.pdf`: Northstar against PO-5003, which is CLOSED | **NEEDS_REVIEW** (`po_status`) |
| `08_vendor_mismatch_BW-2026-0611.pdf`: Bluewave quoting Acme's PO-5001 | **NEEDS_REVIEW** (`vendor_match`) |
| `09_unknown_po_INV-2002.pdf`: quotes PO-9999, which does not exist | **NEEDS_REVIEW** (`po_match`) |
| `10_currency_mismatch_INV-2003.pdf`: billed in USD against an INR PO | **NEEDS_REVIEW** (`currency_match`) |
| `11_arithmetic_error_INV-2004.pdf`: IGST misprinted, so subtotal + tax ≠ total | **NEEDS_REVIEW** (`totals_arithmetic`) |
| `12_not_a_pdf.pdf`: an email saved as `.pdf` | Upload rejected, **HTTP 422** `invalid_pdf`; no run is created |

Check them against the live API with `python -m scripts.demo run --samples data/samples/extra --allow-existing`.

[docs/DEMO.md](docs/DEMO.md) has the full scenario details and a **five-minute presentation script**. Regenerate every PDF with `python -m scripts.generate_samples`.

---

## Deploying to Render

The repository includes a Render Blueprint, [render.yaml](render.yaml), that defines two web services from the same code:

| Service | Start command | Health check | Key settings |
|---|---|---|---|
| `zamp-invoice-api` (FastAPI) | `sh scripts/start_api.sh` | `/api/v1/health` (checks the database too) | `SEED_ON_STARTUP=true` |
| `zamp-invoice-ui` (Streamlit) | `sh scripts/start_ui.sh` | `/_stcore/health` | `API_BASE_URL` = the API's public URL |

Both services bind `0.0.0.0` on the `PORT` Render assigns, install `requirements.txt` and use Python 3.12 (`.python-version`). The UI calls the API from its server, so no CORS setup is needed.

Steps:
1. Push the repository to GitHub or GitLab.
2. In Render: **New → Blueprint**, then pick the repository.
3. When asked for `API_BASE_URL`, enter `https://zamp-invoice-api.onrender.com` (the API's URL, shown on its service page).

**Storage.** On Render's free plan the filesystem is temporary. The database and uploads are wiped on every deploy, restart and idle spin-down, and the seed POs are recreated at startup. To keep history:
- attach a disk to the API service (paid plans; see the commented block in `render.yaml`);
- set `DATA_DIR` to its mount path, for example `/var/data`;
- keep the API to **one instance**, because SQLite is single-writer.

**OCR.** Render's Python environment has no Tesseract, so scanned PDFs fail cleanly there (`ocr_unavailable`, HTTP 503). Text PDFs work normally. Enabling OCR in the cloud needs a Docker-based service that installs `tesseract-ocr`.

## Architecture

```
 Streamlit UI (frontend/)  ──HTTP──▶  FastAPI /api/v1 (app/api/)  ──▶  Workflow orchestrator (app/workflow/)
   upload · run details ·               validate upload, run the        RECEIVED → EXTRACTION → NORMALIZATION
   dashboard (no business rules)        workflow, present results       → REQUIRED_FIELD_VALIDATION → DUPLICATE_CHECK
                                                                         → PO_MATCHING → BUSINESS_VALIDATION
                                                                         → DECISION → PERSISTENCE → COMPLETED | FAILED
                                                                                         │
          extraction/  PyMuPDF text layer, or Tesseract OCR → layout rebuild → deterministic label parser
          normalization/  references, vendor identity, currency, Decimal money, dates
          duplicates/  vendor identity + invoice number; same file; vendor + date + amount
          matching/  unique PO by normalized reference; status, vendor and currency checks
          validation/  11 checks → decision by explicit priority
                                                                                         │
                                       SQLite (SQLAlchemy): purchase_orders, processing_runs, run_stages,
                                       validation_checks, run_events
```

- **One run per upload.** Each run gets a UUID `run_id`. A stage's row is written when it starts and again when it ends, with its timings, output summary or error. Stages that never ran have no row.
- **Processing status and business decision are separate.** A run's `status` is `COMPLETED` or `FAILED`. Only a `COMPLETED` run has a `decision`. A failed extraction is never shown as a decision.
- **The UI calls only the API.** It holds no data of its own and applies no business rules.

<details>
<summary>Project layout</summary>

```
app/
  main.py            FastAPI app: startup creates and checks tables, configures logging
  config.py          Settings from environment / .env
  api/               /api/v1 routes, error envelope, presenters (what the API exposes), upload checks
  db/                engine, ORM models, Money type (Decimal as integer paise), seed command
  schemas/           Pydantic models shared by every stage
  extraction/        reader (PDF/OCR), layout rebuild, deterministic parser
  normalization/     value normalizers and comparison keys
  duplicates/        duplicate detection
  matching/          PO matching
  validation/        the 11 checks and the decision
  workflow/          orchestrator; transaction-safe decision persistence
frontend/            Streamlit pages, API client, UI helpers (status indicators)
scripts/             demo invoices, sample generator, demo reset/run
docs/DEMO.md         demo order, expected results, five-minute script
tests/               pytest suite (unit, API, workflow, frontend, invariants, demo samples)
data/                databases, uploads, data/samples/ demo PDFs + manifest.json
```
</details>

---

## Business rules and assumptions

All rules are plain Python in [app/validation/validator.py](app/validation/validator.py), [app/matching/po_matcher.py](app/matching/po_matcher.py) and [app/duplicates/detector.py](app/duplicates/detector.py). The same input always produces the same decision.

**Checks.** Each check returns a name, a status (`PASS` / `WARN` / `FAIL` / `SKIPPED`), a reason and evidence.

| Check | Mandatory | Fails when |
|---|---|---|
| `required_fields` | yes | vendor, invoice number, invoice date, PO number, currency or total is missing |
| `duplicate_invoice` | yes | confirmed duplicate of an approved invoice (→ REJECTED), or the same invoice is still awaiting review |
| `potential_duplicate` | no | same vendor, date and total under a different number, or the same number under a vendor-name variant |
| `po_match` | yes | the PO is not found, ambiguous, or not referenced |
| `po_status` | yes | the PO is CLOSED |
| `vendor_match` | yes | the vendor differs from the PO's vendor, or is missing |
| `currency_match` | yes | the currency differs from the PO's, or is missing |
| `amount_within_tolerance` | yes | \|invoice total − PO amount\| > 1% of the PO amount |
| `totals_arithmetic` | no | subtotal + tax ≠ total (only checked when all three are present) |
| `line_items_total` | no | line amounts ≠ subtotal (or ≠ total − tax) |
| `line_item_arithmetic` | no | quantity × unit price ≠ amount on any line |

**Decision priority: REJECTED > NEEDS_REVIEW > APPROVED.** The decision is the strongest effect of any check:
- a `FAIL` forces the check's failure decision;
- a **mandatory** check that is `SKIPPED` (it could not run, for example because no PO was found) forces NEEDS_REVIEW;
- `PASS` and `WARN` force nothing.

A passing check can therefore never override a duplicate or a failed mandatory check, and the order in which checks run does not matter. APPROVED requires positive evidence from every mandatory check.

**Assumptions** (illustrative choices; easy to change):
- **Tolerance applies both ways.** An invoice well *below* its PO also goes to review, so partial invoices against one PO are not auto-approved.
- **Confirmed duplicates.** A duplicate is confirmed only against an earlier **APPROVED** invoice with the same normalized vendor identity and invoice number, or the identical file. Matching the same invoice still under review gives NEEDS_REVIEW, because the earlier one may yet be rejected. Rejected and failed runs never count as earlier invoices.
- **Vendor names.** Vendor comparison ignores case, punctuation, a leading `M/s` and the spelling of the legal form (`Private Limited` = `Pvt. Ltd.`). A name that differs only by lacking the legal suffix matches, with a `WARN`. Different legal forms (`LLP` vs `Pvt Ltd`) are a mismatch.
- **PO references.** These are compared by a normalized key (`PO 5001`, `PO-5001` and `P.O. No. 5001` are the same). If several POs share the key, the match is **AMBIGUOUS** and none is chosen.
- **Unverifiable means review.** A missing currency or vendor fails its check: it cannot be verified, so it cannot pass.
- **Money.** Money is `Decimal` and is stored as integer paise. Floats and values with more than 2 decimal places are rejected, never rounded silently.
- **Extraction never guesses.** Missing, unreadable, ambiguous (a bare `$`) or contradictory values are left `null`, with a warning.
- **Dates.** Numeric dates are read as `DATE_ORDER` (default day-month-year, the Indian convention).

**Concurrency.** A partial unique index on `(vendor_key, invoice_key) WHERE decision = 'APPROVED'` makes the database itself refuse a second approval of the same invoice. When concurrent requests race, the loser rolls back, re-runs the duplicate check and records REJECTED, logging an `approval_conflict` event. This was verified with 8 simultaneous uploads against the running server: exactly one was approved.

---

## API reference

All paths are under `/api/v1`. Interactive docs are at http://localhost:8000/docs.

| Method & path | Purpose |
|---|---|
| `GET /health` | Liveness plus a database round trip (503 if the database is unreachable) |
| `POST /invoices/process` | Upload a PDF (multipart field `file`) and run the workflow synchronously |
| `GET /runs?status=&decision=&page=&page_size=` | History, newest first, filterable; `page_size` ≤ 100; returns `{items, total, page, page_size, has_next}` |
| `GET /runs/stats` | Counts by status and decision, plus average duration (feeds the dashboard) |
| `GET /runs/{run_id}` | Everything recorded: fields, extraction info, matched PO, stages, checks, events |
| `GET /runs/{run_id}/result` | The business result: decision, reasons, deciding checks, fields, matched PO, checks |
| `GET /purchase-orders` | The seeded POs |

| `POST /invoices/process` status | Meaning | Run created? |
|---|---|---|
| 201 | The workflow completed; `decision` is APPROVED, NEEDS_REVIEW or REJECTED. A `Location` header points to the run | yes |
| 400 `empty_file` · 413 `file_too_large` · 415 `unsupported_media_type` | Empty, over `MAX_UPLOAD_MB`, or not a PDF media type | no |
| 422 `invalid_pdf` · `too_many_pages` · `validation_error` | Not a readable PDF (corrupt, encrypted, not a PDF), too many pages, or no `file` field | no |
| 503 `ocr_unavailable` · 500 `internal_error` | A run was created but FAILED; the body's `run` holds its `run_id`, and `decision` is null | yes |

Other responses: 404 `run_not_found`; 422 for a malformed `run_id` or invalid query parameters; 409 `run_not_finished` from `/result` while a run is still processing.

Every error uses one envelope: `{"error": {"code", "message", "details"?}, "run"?}`. Responses never include stack traces, exception types, stored file paths or raw document text. Internal error messages are replaced with a generic one; the real message is kept in the database and the server log. The client's file name is sanitized and used only as a display label. The stored file is always `<run_id>.pdf`.

---

## Known limitations

- **No migrations.** If the models change, startup refuses an outdated database with a clear `SchemaMismatchError` (the fix: back the file up, delete it, then run `python -m app.db.seed`). Alembic would be the next step.
- **Synchronous processing.** Each upload is processed within its HTTP request. That is fine for single invoices; a production system would queue the work and stream progress. The UI therefore shows a *recorded* stage timeline, not a live one.
- **No reviewer actions yet.** NEEDS_REVIEW runs cannot be approved or rejected from the UI. Any such action would also need to handle the database refusing a second approval.
- **Two supported layouts.** Extraction handles the layouts described in [app/extraction/parser.py](app/extraction/parser.py): label/value headers, a description–qty–price–amount table, and common Indian GST totals. Other layouts may extract partially; missing fields then send the invoice to review rather than producing wrong data.
- **OCR accuracy is only checked on clean scans.** With Tesseract 5.5.3, the demo scan (200 dpi greyscale) extracts every field and line item identically to the text version and is approved. Poor-quality real-world scans (skewed, low resolution, stamps, handwriting) will extract less. Missing fields then go to review rather than being guessed, and OCR runs are flagged with a warning. OCR adds about 1 s per page.
- **Header-level PO matching only.** The invoice total is compared with the PO amount; PO line items, partial deliveries and cumulative billing against a PO are not tracked.
- **Uploads are buffered before the size check.** FastAPI receives the whole upload before `MAX_UPLOAD_MB` is enforced. In production, also enforce the limit at the reverse proxy.
- **Single-node SQLite.** Concurrency is safe (one writer at a time, plus the unique index), but it is not built for high write throughput.
- **No authentication.** Anyone who can reach the API can upload invoices and read results.
