"""Write the demo invoice PDFs and their manifest to data/samples/, and edge cases to data/samples/extra/.

Run with:  python -m scripts.generate_samples [target_dir]

The numeric prefix is the order in which the samples must be processed (see
docs/DEMO.md). manifest.json records each scenario's expected outcome. Only the
verification tooling (scripts/demo.py, tests) reads it; the application never does.
"""

import json
import sys
from pathlib import Path

from app.config import PROJECT_ROOT
from scripts import sample_invoices as s

SAMPLES_DIR = PROJECT_ROOT / "data" / "samples"
EXTRA_DIR = SAMPLES_DIR / "extra"

# (file name, builder, scenario, expected outcome). "expected_without_ocr" applies when
# the server has no Tesseract.
DEMO = [
    {
        "file": "01_happy_path_INV-1001.pdf",
        "build": lambda: s.build_pdf(s.happy_path()),
        "scenario": "Happy path: Acme INV-1001 against PO-5001 for INR 50,000.00.",
        "expected": {"status": "COMPLETED", "decision": "APPROVED", "deciding_checks": []},
    },
    {
        "file": "02_duplicate_resend_INV-1001.pdf",
        "build": lambda: s.build_pdf(s.duplicate_resend()),
        "scenario": "Duplicate: the vendor re-sends INV-1001 as a copy (different file, same vendor and number). "
                    "Must be processed after 01.",
        "expected": {"status": "COMPLETED", "decision": "REJECTED", "deciding_checks": ["duplicate_invoice"],
                     "duplicate_of": "01_happy_path_INV-1001.pdf"},
    },
    {
        "file": "03_amount_mismatch_INV-1002.pdf",
        "build": lambda: s.build_pdf(s.amount_mismatch()),
        "scenario": "Amount mismatch: Acme INV-1002 for INR 55,000.00 against PO-5001 (INR 50,000.00); "
                    "10% over the 1% tolerance.",
        "expected": {"status": "COMPLETED", "decision": "NEEDS_REVIEW", "deciding_checks": ["amount_within_tolerance"]},
    },
    {
        "file": "04_missing_invoice_number_PO-5002.pdf",
        "build": lambda: s.build_pdf(s.missing_invoice_number()),
        "scenario": "Missing field: Bluewave invoice against PO-5002 for INR 1,20,000.00 with the Bill No. left "
                    "blank, so it cannot be identified or checked for duplicates.",
        "expected": {"status": "COMPLETED", "decision": "NEEDS_REVIEW",
                     "deciding_checks": ["required_fields", "duplicate_invoice"]},
    },
    {
        "file": "05_scanned_INV-1003.pdf",
        "build": lambda: s.scan(s.build_pdf(s.scanned_original())),
        "scenario": "Scanned invoice: Acme INV-1003 against PO-5004 for INR 80,000.00, as an image-only PDF.",
        "expected": {"status": "COMPLETED", "decision": "APPROVED", "deciding_checks": []},
        "expected_without_ocr": {"status": "FAILED", "decision": None, "error_code": "ocr_unavailable",
                                 "failed_stage": "EXTRACTION", "http_status": 503},
    },
]


# Edge cases: each one is independent of the others and of the demo set (distinct invoice
# numbers, dates and amounts), so they can be processed in any order.
EXTRA = [
    {
        "file": "06_within_tolerance_INV-2001.pdf",
        "build": lambda: s.build_pdf(s.within_tolerance()),
        "scenario": "Acme INV-2001 for INR 50,400.00 against PO-5001 (INR 50,000.00): 0.8% over, inside the "
                    "1% tolerance.",
        "expected": {"status": "COMPLETED", "decision": "APPROVED", "deciding_checks": []},
    },
    {
        "file": "07_closed_po_NS-2026-118.pdf",
        "build": lambda: s.build_pdf(s.closed_po()),
        "scenario": "Northstar NS-2026-118 for INR 75,000.00 against PO-5003, which is CLOSED.",
        "expected": {"status": "COMPLETED", "decision": "NEEDS_REVIEW", "deciding_checks": ["po_status"]},
    },
    {
        "file": "08_vendor_mismatch_BW-2026-0611.pdf",
        "build": lambda: s.build_pdf(s.vendor_mismatch()),
        "scenario": "Bluewave BW/2026/0611 quotes PO-5001, which was raised for Acme Office Supplies.",
        "expected": {"status": "COMPLETED", "decision": "NEEDS_REVIEW", "deciding_checks": ["vendor_match"]},
    },
    {
        "file": "09_unknown_po_INV-2002.pdf",
        "build": lambda: s.build_pdf(s.unknown_po()),
        "scenario": "Acme INV-2002 quotes PO-9999, which does not exist; every PO-dependent check cannot run.",
        "expected": {"status": "COMPLETED", "decision": "NEEDS_REVIEW",
                     "deciding_checks": ["po_match", "po_status", "vendor_match", "currency_match",
                                         "amount_within_tolerance"]},
    },
    {
        "file": "10_currency_mismatch_INV-2003.pdf",
        "build": lambda: s.build_pdf(s.currency_mismatch()),
        "scenario": "Acme INV-2003 billed in USD 960.00 against PO-5004, which is in INR; the amounts "
                    "cannot be compared.",
        "expected": {"status": "COMPLETED", "decision": "NEEDS_REVIEW",
                     "deciding_checks": ["currency_match", "amount_within_tolerance"]},
    },
    {
        "file": "11_arithmetic_error_INV-2004.pdf",
        "build": lambda: s.build_pdf(s.arithmetic_error()),
        "scenario": "Acme INV-2004 against PO-5004: the IGST line is misprinted (12,000.00 instead of 12,203.39), "
                    "so subtotal + tax does not equal the total.",
        "expected": {"status": "COMPLETED", "decision": "NEEDS_REVIEW", "deciding_checks": ["totals_arithmetic"]},
    },
    {
        "file": "12_not_a_pdf.pdf",
        "build": lambda: b"From: accounts@acme-supplies.example\nSubject: Invoice INV-2005\n\n"
                         b"Please find our invoice attached.\n",
        "scenario": "An email saved with a .pdf extension. It is rejected before processing; no run is created.",
        "expected": {"status": None, "decision": None, "error_code": "invalid_pdf", "http_status": 422},
    },
]


def write_samples(directory: Path = SAMPLES_DIR, samples: list[dict] = DEMO) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for sample in samples:
        path = directory / sample["file"]
        path.write_bytes(sample["build"]())
        paths.append(path)
    manifest = [{"order": n, **{k: v for k, v in sample.items() if k != "build"}}
                for n, sample in enumerate(samples, 1)]
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return paths


def load_manifest(directory: Path = SAMPLES_DIR) -> list[dict]:
    return json.loads((directory / "manifest.json").read_text())


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else SAMPLES_DIR
    for directory, samples in ((target, DEMO), (target / "extra", EXTRA)):
        for path in write_samples(directory, samples):
            print(f"{path}  ({path.stat().st_size / 1024:,.1f} KB)")
        print(directory / "manifest.json")
