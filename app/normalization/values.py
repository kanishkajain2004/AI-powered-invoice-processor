"""Normalizers for individual values.

Each kind of value has a display form, which keeps every meaningful character and
only tidies whitespace, case and dash variants, and, where values are compared, a
separate comparison key. Keys are used only to find matches. They are never shown as
the value or stored in its place.

Functions return None for absent input and raise ValueError for input that is
present but cannot be normalized without guessing.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.extraction.parser import ISO_CURRENCIES, parse_date, parse_number

# --- whitespace ------------------------------------------------------------------

_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))  # zero-width chars, soft hyphen


def normalize_whitespace(value: str | None) -> str | None:
    """NFC-normalize, drop invisible characters and collapse all Unicode whitespace to single spaces."""
    if value is None:
        return None
    text = unicodedata.normalize("NFC", value).translate(_INVISIBLE)
    return " ".join(text.split()) or None


# --- references (invoice numbers, PO numbers) ------------------------------------

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−﹘﹣－"), "-")
_SPACED_SEPARATOR = re.compile(r"\s*([-/_.#:])\s*")
_EDGE_PUNCTUATION = " .,;:'\"()[]{}"
_KEY_TOKENS = re.compile(r"[^\W\d_]+|\d+")


def normalize_reference(value: str | None) -> str | None:
    """Display form of a document reference: "inv – 1001." -> "INV-1001".

    Uppercases, unifies dash characters, removes spaces around separators and trims
    punctuation from the ends. Separators themselves ("-", "/", "_", ".") are kept.
    """
    text = normalize_whitespace(value)
    if text is None:
        return None
    text = _SPACED_SEPARATOR.sub(r"\1", text.translate(_DASHES).upper())
    return text.strip(_EDGE_PUNCTUATION) or None


def _reference_tokens(value: str | None) -> list[str]:
    text = normalize_reference(value)
    return _KEY_TOKENS.findall(text) if text else []


def reference_key(value: str | None) -> str | None:
    """Comparison key for a reference: its runs of letters and of digits, joined by "-".

    "INV-1001", "INV/1001", "INV 1001" and "INV1001" all give "INV-1001", but
    "INV-10-01" gives "INV-10-01": the separator character is ignored, its position is not.
    Leading zeros are kept ("0457" stays "0457").
    """
    tokens = _reference_tokens(value)
    return "-".join(tokens) or None


_PO_PREFIXES = (["P", "O"], ["PO"], ["PURCHASE", "ORDER"])
_NUMBER_WORDS = {"NO", "NUMBER", "NUM", "NR"}


def po_reference_key(value: str | None) -> str | None:
    """Comparison key for a PO reference, without its "PO" prefix.

    "PO-5001", "PO 5001", "po#5001", "P.O. No. 5001" and "5001" all give "5001".
    """
    tokens = _reference_tokens(value)
    for prefix in _PO_PREFIXES:
        if tokens[: len(prefix)] == prefix:
            tokens = tokens[len(prefix):]
            break
    if tokens and tokens[0] in _NUMBER_WORDS:
        tokens = tokens[1:]
    return "-".join(tokens) or None


# --- vendors ---------------------------------------------------------------------

# Legal-form suffixes, as token sequences, mapped to a canonical form. Longest first.
_LEGAL_SUFFIXES: list[tuple[list[str], str]] = sorted(
    [
        (["private", "limited"], "pvt ltd"),
        (["pvt", "limited"], "pvt ltd"),
        (["pvt", "ltd"], "pvt ltd"),
        (["private", "ltd"], "pvt ltd"),
        (["p", "ltd"], "pvt ltd"),
        (["limited"], "ltd"),
        (["ltd"], "ltd"),
        (["l", "l", "p"], "llp"),
        (["llp"], "llp"),
        (["incorporated"], "inc"),
        (["inc"], "inc"),
        (["l", "l", "c"], "llc"),
        (["llc"], "llc"),
        (["corporation"], "corp"),
        (["corp"], "corp"),
        (["company"], "co"),
        (["co"], "co"),
        (["gmbh"], "gmbh"),
        (["plc"], "plc"),
    ],
    key=lambda item: -len(item[0]),
)
_HONORIFIC_PREFIXES = (["m", "s"], ["messrs"])  # "M/s Acme ..."


@dataclass(frozen=True)
class VendorIdentity:
    """A vendor name reduced for comparison: the distinctive part and the legal form."""

    core: str
    legal_suffix: str | None

    @property
    def key(self) -> str:
        return f"{self.core} {self.legal_suffix}" if self.legal_suffix else self.core


def normalize_vendor_name(value: str | None) -> str | None:
    """Display form: whitespace tidied, everything else as written."""
    return normalize_whitespace(value)


def vendor_identity(value: str | None) -> VendorIdentity | None:
    """Case-, accent- and punctuation-insensitive identity of a vendor name.

    "M/s ACME Office Supplies Private Limited" and "Acme Office Supplies Pvt. Ltd."
    both give core "acme office supplies" with legal suffix "pvt ltd".
    """
    text = normalize_whitespace(value)
    if text is None or not any(ch.isalnum() for ch in text):
        return None
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).replace("&", " and ")
    tokens = re.sub(r"[^\w\s]", " ", text).split()

    for prefix in _HONORIFIC_PREFIXES:
        if tokens[: len(prefix)] == prefix and len(tokens) > len(prefix):
            tokens = tokens[len(prefix):]
            break

    suffix = None
    for pattern, canonical in _LEGAL_SUFFIXES:
        if len(tokens) > len(pattern) and tokens[-len(pattern):] == pattern:
            tokens, suffix = tokens[: -len(pattern)], canonical
            break
    return VendorIdentity(" ".join(tokens), suffix) if tokens else None


# --- currency --------------------------------------------------------------------

_CURRENCY_ALIASES = {"₹": "INR", "RS": "INR", "RS.": "INR", "RUPEE": "INR", "RUPEES": "INR", "US$": "USD", "€": "EUR", "£": "GBP"}


def normalize_currency(value: str | None) -> str | None:
    """ISO 4217 code for a code, symbol or name. "$" alone is ambiguous and rejected."""
    text = normalize_whitespace(value)
    if text is None:
        return None
    upper = text.upper()
    if upper in ISO_CURRENCIES:
        return upper
    if upper in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[upper]
    if upper == "$":
        raise ValueError("'$' does not identify a currency (USD, SGD, AUD, ...)")
    raise ValueError(f"unknown currency {text!r}")


# --- money and quantities ----------------------------------------------------------

_CENT = Decimal("0.01")


def _to_decimal(value: Decimal | int | str, max_places: int) -> Decimal:
    if isinstance(value, bool | float) or not isinstance(value, Decimal | int | str):
        raise TypeError(f"expected Decimal, int or str, got {type(value).__name__}")
    if isinstance(value, str):
        parsed = parse_number(value, max_places=max_places)
        if parsed is None:
            raise ValueError(f"not a number: {value!r}")
        return parsed
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError(f"not a finite number: {value}")
    return number


def normalize_amount(value: Decimal | int | str | None) -> Decimal | None:
    """A money amount as a Decimal with exactly 2 decimal places: "1,20,000" -> Decimal("120000.00").

    Values with more than 2 decimal places raise ValueError rather than being rounded.
    """
    if value is None:
        return None
    number = _to_decimal(value, max_places=2)
    quantized = number.quantize(_CENT)
    if quantized != number:
        raise ValueError(f"{value} has more than 2 decimal places")
    return quantized


def normalize_quantity(value: Decimal | int | str | None, max_places: int = 4) -> Decimal | None:
    """A quantity or unit price as a Decimal, keeping its precision (up to `max_places`)."""
    if value is None:
        return None
    number = _to_decimal(value, max_places=max_places)
    exponent = number.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > max_places:
        raise ValueError(f"{value} has more than {max_places} decimal places")
    return number


# --- dates -----------------------------------------------------------------------


def normalize_date(value: date | datetime | str | None, date_order: str = "DMY") -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = normalize_whitespace(value)
    if text is None:
        return None
    parsed = parse_date(text, date_order)
    if parsed is None:
        raise ValueError(f"not a valid date: {value!r}")
    return parsed
