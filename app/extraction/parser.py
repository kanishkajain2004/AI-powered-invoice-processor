"""Deterministic invoice field parser: text lines -> ExtractedInvoice.

Supported layout
----------------
* Header fields as "Label: value" (or label and value in adjacent columns, or the
  value directly below the label). Recognised labels:
    invoice number  Invoice No / Number / # / ID, Bill No, Tax Invoice No
    invoice date    Invoice Date, Date of Issue, Issue Date, Bill Date, Dated, Date
    PO number       PO No / Number / #, P.O. No, Purchase Order (No); a bare
                    "PO-1234" token is used if no label is present
    vendor          Vendor, Supplier, Seller, Sold By, Bill From, From: or a FROM
                    column heading with the name below it; otherwise the
                    first line ending in a company suffix (Pvt Ltd, LLP, Inc, ...)
                    above the Bill To / Buyer block
    currency        Currency: XXX (or a Currency heading); otherwise ISO codes and symbols
* Totals as "Label  amount": Subtotal / Taxable Value; Total Tax / Tax Amount, or
  individual CGST / SGST / IGST / GST / VAT / Tax lines (summed); Grand Total /
  Total Amount / Amount Due / Total.
* A line-item table whose header names description, quantity and amount columns,
  with rows of the form "[#] description  qty  unit_price  amount".
* Amounts with Western (1,234,567.89) or Indian (12,34,567.89) digit grouping.
* Numeric dates in the configured order (DATE_ORDER, default DMY), ISO dates, and
  dates with month names.

Rules
-----
A value is set only when it is read unambiguously. Missing labels give None;
unreadable or conflicting values also give None and add a warning. Arithmetic and
business checks (do the lines add up, does the PO exist) are not done here.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.extraction.layout import Line, Segment
from app.schemas import ExtractedInvoice, ExtractedLineItem


@dataclass
class ParsedInvoice:
    invoice: ExtractedInvoice
    raw_fields: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# --- primitive value parsers ----------------------------------------------------

_CURRENCY_PREFIX = re.compile(r"^(?:US\$|INR|USD|EUR|GBP|Rs\.?|₹|\$|€|£)", re.I)
_CURRENCY_TOKEN = re.compile(r"^(?:US\$|INR|USD|EUR|GBP|Rs\.?|₹|\$|€|£)$", re.I)
_NUMBER = re.compile(r"(?P<int>\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})+,\d{3}|\d+)(?:\.(?P<frac>\d+))?")


def parse_number(token: str, max_places: int = 2) -> Decimal | None:
    """Parse one numeric token such as "1,20,000.00", "Rs.500", "(250.00)" or "-12".

    Returns None for anything that is not clearly a number with at most
    `max_places` decimals (percentages, dates, malformed grouping, ...).
    """
    text = token.strip().rstrip(":;,")
    if text.endswith("/-"):  # Indian style "5,000/-"
        text = text[:-2]
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    if text.startswith("-"):
        negative, text = True, text[1:]
    text = _CURRENCY_PREFIX.sub("", text).strip()
    match = _NUMBER.fullmatch(text)
    if not match:
        return None
    frac = match["frac"] or ""
    if len(frac) > max_places:
        return None
    value = Decimal(match["int"].replace(",", "") + (f".{frac}" if frac else ""))
    return -value if negative else value


def amounts_in(text: str) -> list[Decimal]:
    return [v for v in (parse_number(t) for t in text.split()) if v is not None]


_MONTHS = {
    name: i
    for i, names in enumerate(
        [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
         ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
         ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december")],
        start=1,
    )
    for name in names
}
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})([/.\-])(\d{1,2})\2(\d{4})\b")
_DAY_MONTH_YEAR = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-]+([A-Za-z]{3,9})\.?,?[\s\-]+(\d{4})\b")
_MONTH_DAY_YEAR = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")


def parse_date(text: str, date_order: str = "DMY") -> date | None:
    """Parse the first date in `text`. Invalid or unrecognised dates give None."""
    try:
        if m := _ISO_DATE.search(text):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := _NUMERIC_DATE.search(text):
            first, second, year = int(m[1]), int(m[3]), int(m[4])
            day, month = (first, second) if date_order == "DMY" else (second, first)
            return date(year, month, day)
        if (m := _DAY_MONTH_YEAR.search(text)) and m[2].lower() in _MONTHS:
            return date(int(m[3]), _MONTHS[m[2].lower()], int(m[1]))
        if (m := _MONTH_DAY_YEAR.search(text)) and m[1].lower() in _MONTHS:
            return date(int(m[3]), _MONTHS[m[1].lower()], int(m[2]))
    except ValueError:  # e.g. 31/02/2026, or month 15 under DMY
        return None
    return None


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/_.#]{0,39}")


def parse_identifier(text: str) -> str | None:
    """The first token of `text` if it looks like a document number (has a digit)."""
    tokens = text.split()
    if not tokens:
        return None
    token = tokens[0].rstrip(".,;:")
    if _IDENTIFIER.fullmatch(token) and any(ch.isdigit() for ch in token):
        return token
    return None


# --- labels ----------------------------------------------------------------------


def _label(pattern: str) -> re.Pattern:
    """Match `pattern` at the start of a segment, ending at a word boundary.

    A hyphen only ends a label when no letter or digit follows, so "PO-5002" is a
    value, not the label "PO".
    """
    return re.compile(rf"^\s*(?:{pattern})(?=$|[\s:#.(@%]|-(?![A-Za-z0-9]))", re.I)


INVOICE_NUMBER_LABELS = [_label(r"(?:tax\s+)?(?:invoice|inv|bill)\.?\s*(?:no|number|num|id|#)\.?|invoice(?=\s*[:#])")]
INVOICE_DATE_LABELS = [
    _label(r"invoice\s+date|date\s+of\s+(?:issue|invoice)|issue\s+date|bill\s+date"),
    _label(r"dated|date"),
]
PO_NUMBER_LABELS = [
    _label(r"(?:p\.?\s?o\.?|purchase\s+order)(?!\s*(?:box|date|dt\b))\s*(?:no|number|num|ref(?:erence)?|#)?\.?")
]
VENDOR_LABELS = [
    _label(
        r"(?:vendor|supplier|seller)(?:\s+name)?(?!\s*(?:code|id\b|no\b|number|gstin|pan\b|address))"
        # "From" counts only as "From:" or as a stand-alone column heading ("FROM | BILL TO"),
        # never as a word inside a sentence ("From 1 Sep to 30 Sep").
        r"|sold\s+by|bill(?:ed)?\s+from|from(?=\s*:|\s*$)"
    )
]
# "Currency:", "Currency (INR)" or a stand-alone heading; not a sentence that starts with the word.
CURRENCY_LABELS = [_label(r"currency(?=\s*[:(]|\s*$)")]
BUYER_LABELS = [_label(r"bill(?:ed)?\s+to|ship(?:ped)?\s+to|buyer|customer|invoice\s+to|sold\s+to|consignee|client")]

SUBTOTAL_LABELS = [
    _label(r"sub\s*-?\s*total"),
    _label(r"taxable\s+(?:value|amount)"),
    _label(r"total\s+before\s+tax|total\s+excl(?:\.|uding)?\s+tax|amount\s+before\s+tax"),
]
TAX_TOTAL_LABELS = [_label(r"total\s+(?:tax|gst|vat)(?:\s+amount)?|(?:tax|gst|vat)\s+amount")]
TAX_COMPONENT_LABEL = _label(
    r"(?P<name>[csiu]gst|gst|vat|sales\s+tax|service\s+tax|tax(?!\s*(?:invoice|id|no\b|number|registration|rate)))"
)
TOTAL_LABELS = [
    _label(r"grand\s+total"),
    _label(r"total\s+amount(?:\s+(?:due|payable))?|amount\s+(?:due|payable)|invoice\s+total|total\s+payable|balance\s+due|net\s+payable"),
    _label(r"total(?!\s*(?:tax|gst|vat|qty|quantity|items?|before|excl|discount|in\s+words|\(in\s+words))"),
]

_HEADER_LABELS = (
    INVOICE_NUMBER_LABELS + INVOICE_DATE_LABELS + PO_NUMBER_LABELS + VENDOR_LABELS
    + CURRENCY_LABELS + BUYER_LABELS + [_label(r"due\s+date|gstin|pan|terms|place\s+of\s+supply")]
)
_TOTALS_LABELS = SUBTOTAL_LABELS + TAX_TOTAL_LABELS + TOTAL_LABELS + [TAX_COMPONENT_LABEL]


def _is_label(text: str) -> bool:
    return any(p.match(text) for p in _HEADER_LABELS + _TOTALS_LABELS)


_SEPARATORS = re.compile(r"^[\s:#.\-–]+")


@dataclass
class _Hit:
    priority: int
    value_text: str
    source: str


def _find_labeled(lines: list[Line], patterns: list[re.Pattern]) -> list[_Hit]:
    """Find label occurrences and the text of their values.

    The value is the rest of the label's segment; if that is empty, the next segment on
    the same line; if there is none, the segment directly below the label.
    """
    hits = []
    for li, line in enumerate(lines):
        for si, seg in enumerate(line.segments):
            for priority, pattern in enumerate(patterns):
                if not (m := pattern.match(seg.text)):
                    continue
                value = _SEPARATORS.sub("", seg.text[m.end():]).strip()
                if not value:
                    value = _adjacent_value(lines, li, si)
                source = seg.text if value in seg.text else f"{seg.text} {value}"
                hits.append(_Hit(priority, value, source))
                break
    return hits


def _adjacent_value(lines: list[Line], li: int, si: int) -> str:
    segments = lines[li].segments
    if si + 1 < len(segments) and not _is_label(segments[si + 1].text):
        return segments[si + 1].text
    if li + 1 < len(lines):
        label_seg = segments[si]
        for below in lines[li + 1].segments:
            if below.overlaps(label_seg) and not _is_label(below.text):
                return below.text
    return ""


def _resolve(name, hits, parse, warnings, raw_fields):
    """Pick one value from the highest-priority label group; None if absent, unreadable or conflicting."""
    if not hits:
        return None
    top = min(h.priority for h in hits)
    group = [h for h in hits if h.priority == top]
    parsed = [(parse(h.value_text), h) for h in group]
    unreadable = [h for v, h in parsed if v is None]
    values = {v for v, _ in parsed if v is not None}
    if unreadable:
        shown = ", ".join(repr(h.source) for h in unreadable)
        warnings.append(f"{name}: found a label but could not read its value ({shown}); left empty.")
        return None
    if len(values) > 1:
        warnings.append(f"{name}: conflicting values {sorted(map(str, values))}; left empty.")
        return None
    raw_fields[name] = group[0].source
    return values.pop()


# --- amounts in the totals block -------------------------------------------------


def _amount_hits(lines: list[Line], pattern: re.Pattern, priority: int = 0) -> list[tuple[_Hit, re.Match]]:
    """Label occurrences followed (in the same segment or later segments of the line) by an amount."""
    hits = []
    for line in lines:
        for si, seg in enumerate(line.segments):
            if not (m := pattern.match(seg.text)):
                continue
            rest = " ".join([seg.text[m.end():]] + [s.text for s in line.segments[si + 1:]])
            if amounts := amounts_in(rest):
                hits.append((_Hit(priority, str(amounts[-1]), line.text), m))
            break
    return hits


def _labeled_amount(name, lines, patterns, warnings, raw_fields) -> Decimal | None:
    hits = [h for priority, p in enumerate(patterns) for h, _ in _amount_hits(lines, p, priority)]
    return _resolve(name, hits, Decimal, warnings, raw_fields)


def _tax_amount(lines, warnings, raw_fields) -> Decimal | None:
    if any(_amount_hits(lines, p) for p in TAX_TOTAL_LABELS):
        return _labeled_amount("tax_amount", lines, TAX_TOTAL_LABELS, warnings, raw_fields)

    by_component: dict[str, list[_Hit]] = {}
    for hit, m in _amount_hits(lines, TAX_COMPONENT_LABEL):
        by_component.setdefault(" ".join(m["name"].lower().split()), []).append(hit)
    if not by_component:
        return None
    if "gst" in by_component and by_component.keys() & {"cgst", "sgst", "igst", "ugst"}:
        warnings.append("tax_amount: both a GST line and a CGST/SGST/IGST breakdown found; left empty.")
        return None

    total, sources = Decimal(0), []
    for component, hits in by_component.items():
        values = {h.value_text for h in hits}
        if len(values) > 1:
            warnings.append(f"tax_amount: conflicting {component.upper()} values {sorted(values)}; left empty.")
            return None
        total += Decimal(values.pop())
        sources.append(hits[0].source)
    raw_fields["tax_amount"] = " | ".join(sources)
    return total


# --- currency --------------------------------------------------------------------

ISO_CURRENCIES = {"INR", "USD", "EUR", "GBP", "AED", "SGD", "AUD", "CAD", "JPY", "CHF", "CNY", "HKD", "NZD", "ZAR"}
_CURRENCY_MARKERS = [
    (re.compile(rf"\b({'|'.join(sorted(ISO_CURRENCIES))})\b"), None),
    (re.compile(r"US\$"), "USD"),
    (re.compile(r"₹|\bRs\.?(?=\s*\d)|\bRupees?\b", re.I), "INR"),
    (re.compile(r"€"), "EUR"),
    (re.compile(r"£"), "GBP"),
]


def _currency(lines, warnings, raw_fields) -> str | None:
    def parse_code(text: str) -> str | None:
        token = text.split()[0].upper() if text.split() else ""
        return token if token in ISO_CURRENCIES else None

    if hits := _find_labeled(lines, CURRENCY_LABELS):
        return _resolve("currency", hits, parse_code, warnings, raw_fields)

    text = "\n".join(line.text for line in lines)
    found: dict[str, str] = {}
    for pattern, code in _CURRENCY_MARKERS:
        for m in pattern.finditer(text):
            found.setdefault(code or m[1], m[0])
    if len(found) == 1:
        code, marker = next(iter(found.items()))
        raw_fields["currency"] = marker
        return code
    if len(found) > 1:
        warnings.append(f"currency: several currencies found {sorted(found)}; left empty.")
    elif re.search(r"\$\s*\d", text):
        warnings.append("currency: '$' found but the dollar currency (USD, SGD, ...) is not stated; left empty.")
    return None


# --- vendor ----------------------------------------------------------------------

_COMPANY_SUFFIX = re.compile(
    r"\b(?:pvt\.?\s*ltd\.?|private\s+limited|limited|ltd\.?|llp|inc\.?|llc|corp(?:oration)?\.?|gmbh|plc|co\.)\s*$",
    re.I,
)
_HEADER_SCAN_LINES = 12


def _parse_vendor(text: str) -> str | None:
    name = " ".join(text.split())
    if not name or len(name) > 120 or not any(ch.isalpha() for ch in name) or _is_label(name):
        return None
    return name


def _vendor(lines, warnings, raw_fields) -> str | None:
    if hits := _find_labeled(lines, VENDOR_LABELS):
        return _resolve("vendor_name", hits, _parse_vendor, warnings, raw_fields)

    # Letterhead: the first company-like name above the buyer block.
    end = next(
        (i for i, line in enumerate(lines) if any(p.match(s.text) for p in BUYER_LABELS for s in line.segments)),
        min(len(lines), _HEADER_SCAN_LINES),
    )
    for line in lines[:end]:
        for seg in line.segments:
            if _COMPANY_SUFFIX.search(seg.text) and not _is_label(seg.text):
                raw_fields["vendor_name"] = seg.text
                return _parse_vendor(seg.text)
    return None


# --- line items ------------------------------------------------------------------

_TABLE_HEADER = (
    re.compile(r"\b(?:description|item|particulars|product|services?)\b", re.I),
    re.compile(r"\b(?:qty|quantity|units?)\b", re.I),
    re.compile(r"\b(?:amount|total|value)\b", re.I),
)
_SERIAL_COLUMN = re.compile(r"^(?:#|s\.?\s*no\.?|sr\.?\s*no\.?|sl\.?\s*no\.?|no\.?)(?=\s)", re.I)
_SERIAL_PREFIX = re.compile(r"^\d{1,3}[.)]?\s+")
_ROW = re.compile(r"^(?P<desc>.*?[A-Za-z].*?)\s+(?P<qty>\S+)\s+(?P<unit>\S+)\s+(?P<amount>\S+)$")
_CONTINUATION_X_TOLERANCE = 6.0


def _is_table_header(line: Line) -> bool:
    return all(p.search(line.flat) for p in _TABLE_HEADER)


def _parse_row(line: Line, has_serial: bool) -> ExtractedLineItem | None:
    flat = " ".join(t for t in line.flat.split() if not _CURRENCY_TOKEN.match(t))
    if not (m := _ROW.match(flat)):
        return None
    qty, unit, amount = parse_number(m["qty"], 4), parse_number(m["unit"], 4), parse_number(m["amount"])
    if qty is None or unit is None or amount is None:
        return None
    desc = _SERIAL_PREFIX.sub("", m["desc"]) if has_serial else m["desc"]
    return ExtractedLineItem(description=desc.strip(), quantity=qty, unit_price=unit, amount=amount)


def _line_items(lines, warnings) -> tuple[list[ExtractedLineItem], set[int]]:
    """Parse rows under each table header. Returns the items and the indexes of lines consumed."""
    items: list[ExtractedLineItem] = []
    consumed: set[int] = set()
    i = 0
    while i < len(lines):
        if not _is_table_header(lines[i]):
            i += 1
            continue
        header = lines[i]
        has_serial = bool(_SERIAL_COLUMN.match(header.flat))
        desc_seg = next((s for s in header.segments if _TABLE_HEADER[0].search(s.text)), header.segments[0])
        consumed.add(i)
        i += 1
        while i < len(lines):
            line = lines[i]
            if _is_table_header(line):
                break
            if any(p.match(s.text) for p in _TOTALS_LABELS for s in line.segments):
                break
            if item := _parse_row(line, has_serial):
                items.append(item)
            elif items and _is_continuation(line, desc_seg):
                items[-1].description += " " + line.flat
            else:
                warnings.append(f"line_items: could not read table row {line.flat!r}; skipped.")
            consumed.add(i)
            i += 1
    return items, consumed


def _is_continuation(line: Line, desc_header: Segment) -> bool:
    return (
        len(line.segments) == 1
        and abs(line.segments[0].x0 - desc_header.x0) <= _CONTINUATION_X_TOLERANCE
        and ":" not in line.flat
    )


# --- entry point -----------------------------------------------------------------

_PO_TOKEN = re.compile(r"\bPO[-/]?\d{3,}\b")


def _po_number(lines, warnings, raw_fields) -> str | None:
    if hits := _find_labeled(lines, PO_NUMBER_LABELS):
        return _resolve("po_number", hits, parse_identifier, warnings, raw_fields)
    tokens = {m[0]: line.text for line in lines for m in _PO_TOKEN.finditer(line.text)}
    if len(tokens) == 1:
        token, source = next(iter(tokens.items()))
        raw_fields["po_number"] = source
        return token
    if len(tokens) > 1:
        warnings.append(f"po_number: several PO references {sorted(tokens)}; left empty.")
    return None


def parse_invoice(lines: list[Line], date_order: str = "DMY") -> ParsedInvoice:
    warnings: list[str] = []
    raw: dict[str, str] = {}

    items, item_lines = _line_items(lines, warnings)
    header_lines = [line for i, line in enumerate(lines) if i not in item_lines]

    invoice = ExtractedInvoice(
        vendor_name=_vendor(header_lines, warnings, raw),
        invoice_number=_resolve(
            "invoice_number", _find_labeled(header_lines, INVOICE_NUMBER_LABELS), parse_identifier, warnings, raw
        ),
        invoice_date=_resolve(
            "invoice_date",
            _find_labeled(header_lines, INVOICE_DATE_LABELS),
            lambda text: parse_date(text, date_order),
            warnings,
            raw,
        ),
        po_number=_po_number(header_lines, warnings, raw),
        currency=_currency(lines, warnings, raw),
        subtotal=_labeled_amount("subtotal", header_lines, SUBTOTAL_LABELS, warnings, raw),
        tax_amount=_tax_amount(header_lines, warnings, raw),
        total_amount=_labeled_amount("total_amount", header_lines, TOTAL_LABELS, warnings, raw),
        line_items=items,
    )
    return ParsedInvoice(invoice, raw, warnings)
