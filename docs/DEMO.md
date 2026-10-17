# Demo guide

Five invoice PDFs in `data/samples/` show each outcome of the workflow. They are real PDFs, processed by the real pipeline. Nothing in the application looks at file names or at `manifest.json`; every outcome comes from the document's contents and from what has already been processed.

## Order matters

Process the samples in their numbered order, on a fresh database.

| # | File | What it is | Expected result |
|---|---|---|---|
| 1 | `01_happy_path_INV-1001.pdf` | Acme Office Supplies, INV-1001, against PO-5001, INR 50,000.00 (CGST + SGST) | **APPROVED** |
| 2 | `02_duplicate_resend_INV-1001.pdf` | The vendor re-sends INV-1001 a week later as a "COPY - RESENT". It is a different file with the same vendor and invoice number. | **REJECTED**, a confirmed duplicate of run #1 |
| 3 | `03_amount_mismatch_INV-1002.pdf` | Acme INV-1002 against PO-5001, INR 55,000.00 (IGST), 10% over the PO amount | **NEEDS_REVIEW**: `amount_within_tolerance` (5,000.00 over; the allowance is 500.00) |
| 4 | `04_missing_invoice_number_PO-5002.pdf` | Bluewave Technologies against PO-5002, INR 1,20,000.00, with the *Bill No.* field left blank | **NEEDS_REVIEW**: `required_fields` (invoice_number), and `duplicate_invoice` cannot run |
| 5 | `05_scanned_INV-1003.pdf` | Acme INV-1003 against PO-5004, INR 80,000.00, as an image-only scan | With Tesseract: **APPROVED** (read by OCR). Without it: **processing FAILED** at EXTRACTION, `ocr_unavailable` (HTTP 503), with no decision |

**Why the order matters for #2.** A duplicate is only "confirmed" against an invoice that was already **approved**. Sample #2 is rejected because run #1 approved INV-1001 from the same vendor, and its `duplicate_invoice` evidence names run #1's `run_id`. Process #2 first and it is approved as the first INV-1001 seen; #1 then becomes the duplicate. Process either on a database that already holds an approved INV-1001 and both are rejected. That is why the demo starts from a fresh database.

Sample #2 is deliberately not byte-identical to #1: it has a banner and a "Resent on" note. The match is made on the normalized vendor identity plus invoice number (`vendor+invoice_number`), not on the file hash.

## More test files

`data/samples/extra/` adds seven independent edge cases: an amount inside tolerance (approved), a closed PO, a vendor mismatch, an unknown PO, a currency mismatch, an arithmetic error, and a non-PDF upload. Their expected results are listed in the README and in `data/samples/extra/manifest.json`. They can be processed in any order, before or after the demo set.

## Running the demo

Use a separate demo database, so the development database is never touched:

```bash
source .venv/bin/activate
export DATABASE_URL="sqlite:///$PWD/data/demo.db" UPLOAD_DIR="$PWD/data/demo_uploads"

python -m scripts.demo reset          # fresh data/demo.db with the seed POs (PO-5001 ... PO-5004)
uvicorn app.main:app                  # terminal 1, same environment
streamlit run frontend/streamlit_app.py   # terminal 2
```

Then, in the browser (http://localhost:8501):

1. **Invoice processing:** upload `01` → `05` one at a time and press *Process invoice*. Each shows its decision banner, the checks that decided it, the key fields, the matched PO and the recorded stage list.
2. **Run details:** open #2. The *Duplicate invoice* check shows the `run_id` of #1 as evidence. #5 (without OCR) shows only the stages it actually ran, ending in FAILED.
3. **Dashboard:** with OCR it shows 5 runs: 2 approved, 2 need review, 1 rejected and no failures. Without OCR, #5 becomes 1 processing failure and approvals drop to 1.

To check the same thing without the UI, and against the live API:

```bash
python -m scripts.demo run            # POSTs 01..05 in order and checks each response
```

`run` refuses to start if the backend already holds runs, because the happy path would then be a duplicate. Run `reset` and restart the backend first.

**Safety.** `reset` deletes only a SQLite file whose name starts with `demo`, and clears only an upload directory whose name starts with `demo`. Pointed at `data/invoices.db`, it refuses. Stop the backend before a reset, and start it again afterwards.

## Regenerating the samples

```bash
python -m scripts.generate_samples    # rewrites data/samples/*.pdf and manifest.json
```

The invoices are declared in `scripts/sample_invoices.py`. Totals are computed from the line items, so subtotal + tax = total holds on every sample. All companies, GSTINs and bank details are fictional.

## Enabling OCR for sample #5

```bash
brew install tesseract                # macOS
sudo apt-get install tesseract-ocr    # Debian/Ubuntu
```

Restart the backend afterwards. Verified with Tesseract 5.5.3: OCR of the 200 dpi greyscale scan yields the same vendor, invoice number, date, PO, currency, subtotal, tax, total and line items as the text version of the invoice. The run is APPROVED, with extraction method `ocr` and a warning that the text was read by OCR. OCR adds about 1 second to the run. Without Tesseract, the same file ends as a processing failure (`ocr_unavailable`, HTTP 503) with no decision.

## Five-minute presentation script

**Before you start** (not part of the five minutes):

```bash
source .venv/bin/activate
export DATABASE_URL="sqlite:///$PWD/data/demo.db" UPLOAD_DIR="$PWD/data/demo_uploads"
python -m scripts.demo reset
uvicorn app.main:app                          # terminal 1
streamlit run frontend/streamlit_app.py       # terminal 2 (same exports)
```

Open http://localhost:8501 and http://localhost:8000/docs in two browser tabs. Have `data/samples/` open in a file picker.

### 0:00 – 0:45 · The business problem

> "An AP team receives hundreds of invoice PDFs a week. For each one, someone has to answer: is this a real invoice for something we ordered, at the price we agreed, and have we already paid it? Doing that by eye is slow, and it misses duplicates. Paying the same invoice twice is one of the most common and expensive AP errors. This system makes that decision automatically when it safely can, and routes everything else to a person with the evidence attached."

### 0:45 – 1:30 · Architecture

Show `docs/architecture.png` (open it full screen), or the Swagger tab.

> "A Streamlit UI talks to a FastAPI backend over HTTP. Each upload becomes a run that goes through nine recorded stages: extraction with PyMuPDF (or Tesseract for scans), normalization, a required-field check, duplicate detection, PO matching, business validation, the decision and persistence, all in SQLite. Each stage's start time, duration and output is stored, so every decision can be audited. The UI holds no business rules; it only displays what the backend recorded."

### 1:30 – 2:15 · A valid invoice

*Invoice processing* → upload `01_happy_path_INV-1001.pdf` → **Process invoice**.

> "It extracted the vendor, invoice INV-1001, PO-5001 and the 50,000 rupee total. It found exactly one matching approved PO, from the same vendor, in the same currency, with the amount within 1%. All eleven checks passed, so it is **Approved**. Below is the recorded execution: ten stages in well under a second."

### 2:15 – 3:15 · Edge cases: duplicate and amount mismatch

Upload `02_duplicate_resend_INV-1001.pdf`.

> "The vendor has re-sent the same invoice. It's a different file (note the 'COPY – RESENT' banner), so a file-hash check would miss it. We match on the normalized vendor identity plus the invoice number. It is **Rejected**, and the reason names the run we approved a minute ago."

Upload `03_amount_mismatch_INV-1002.pdf`.

> "This one bills 55,000 against a 50,000 PO: 5,000 over, against a 500 allowance. Everything else is fine, so it is not rejected. It goes to **Needs review**, and the reason states the exact figures."

(Optionally upload `04_…` to show a missing invoice number, and `05_…` to show a scanned invoice read by OCR and approved; the *Invoice & PO* tab shows the extraction method `ocr`.)

### 3:15 – 4:15 · Validation evidence and the dashboard

Click **Open full run details →** on the mismatch, or open *Run details* and pick the duplicate.

> "Every run keeps its full audit trail. The *Validation* tab lists all eleven checks, with result, reason and evidence: the PO amount, the difference and the tolerance used. The *Execution timeline* is the recorded stage history. Processing is synchronous, so this is a record, not a live view. *Invoice & PO* shows the fields, the matched PO and the exact source text each value was read from."

Open **Dashboard**.

> "The dashboard reads straight from the database: totals per decision, processing failures kept separate from rejections, the average processing time, and the full history, which can be filtered and inspected. It survives restarts."

### 4:15 – 5:00 · Why deterministic rules make the decision

> "Whether to pay an invoice is a financial control, so the final decision is made by plain, deterministic rules, never by a language model. Three reasons. First, **auditability**: every decision traces to named checks with concrete evidence, and the same invoice always gets the same answer. Second, **safety**: there is an explicit priority (rejected beats needs review beats approved), so a passing check can never outvote a duplicate or a failed control, and approval requires positive evidence on every mandatory check. Third, **predictable failure**: when data is missing or ambiguous, the system says so and sends the invoice to a human, instead of guessing. AI is the right tool for reading messy documents, and an LLM extractor could slot into the extraction stage later. But it would only *propose* field values; these rules would still *decide*."

**If something goes wrong live:** `python -m scripts.demo reset` (with the backend stopped) gives a clean slate. `python -m scripts.demo run` checks all five outcomes against the API in a few seconds.
