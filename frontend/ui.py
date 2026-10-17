"""Presentation helpers: one definition of every status indicator, formatting, and styles.

Indicators always pair an icon and a label with the reserved status colours (good,
warning, serious, critical), so colour never carries meaning alone. Text stays in the
normal ink colour; only the small icon mark is coloured. Any user-supplied text placed
in HTML goes through `esc`.
"""

import html
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

import streamlit as st

# Reserved status palette (also legible on dark surfaces).
GOOD, WARNING, SERIOUS, CRITICAL, NEUTRAL = "#0ca30c", "#fab219", "#ec835a", "#d03b3b", "#898781"


@dataclass(frozen=True)
class Indicator:
    icon: str
    label: str
    color: str
    glyph: str = "#ffffff"  # colour of the icon on its coloured disc

    @property
    def text(self) -> str:
        """Plain-text form for tables, where HTML is not available."""
        return f"{self.icon} {self.label}"


DECISIONS = {
    "APPROVED": Indicator("✓", "Approved", GOOD),
    "NEEDS_REVIEW": Indicator("!", "Needs review", WARNING, "#0b0b0b"),
    "REJECTED": Indicator("✕", "Rejected", CRITICAL),
}
RUN_STATUSES = {
    "FAILED": Indicator("⚠", "Processing failed", SERIOUS, "#0b0b0b"),
    "PROCESSING": Indicator("…", "Processing", NEUTRAL),
    "RECEIVED": Indicator("…", "Received", NEUTRAL),
    "COMPLETED": Indicator("●", "Completed", NEUTRAL),
}
CHECKS = {
    "PASS": Indicator("✓", "Pass", GOOD),
    "WARN": Indicator("!", "Warning", WARNING, "#0b0b0b"),
    "FAIL": Indicator("✕", "Fail", CRITICAL),
    "SKIPPED": Indicator("–", "Skipped", NEUTRAL),
}
STAGES = {
    "COMPLETED": Indicator("✓", "Completed", GOOD),
    "FAILED": Indicator("✕", "Failed", CRITICAL),
    "STARTED": Indicator("…", "Started, not finished", NEUTRAL),
}
UNKNOWN = Indicator("?", "Unknown", NEUTRAL)


def outcome(run: dict) -> Indicator:
    """The single headline indicator for a run: its decision, or processing failure / progress."""
    if run.get("status") == "FAILED":
        return RUN_STATUSES["FAILED"]
    if run.get("decision"):
        return DECISIONS.get(run["decision"], UNKNOWN)
    return RUN_STATUSES.get(run.get("status"), UNKNOWN)


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def pill(indicator: Indicator) -> str:
    return (
        f'<span class="pill" style="--c:{indicator.color};--g:{indicator.glyph}">'
        f'<span class="pill-icon">{esc(indicator.icon)}</span>{esc(indicator.label)}</span>'
    )


def show(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def kpi_row(tiles: list[str]) -> str:
    """Lay tiles out in one grid so every tile in a row has the same height."""
    return f'<div class="kpi-grid">{"".join(tiles)}</div>'


def kpi(label: str, value: str, caption: str = "", indicator: Indicator | None = None) -> str:
    mark = (f'<span class="pill-icon" style="--c:{indicator.color};--g:{indicator.glyph}">'
            f'{esc(indicator.icon)}</span>') if indicator else ""
    return (
        f'<div class="kpi"><div class="kpi-label">{mark}{esc(label)}</div>'
        f'<div class="kpi-value">{esc(value)}</div><div class="kpi-caption">{esc(caption) or "&nbsp;"}</div></div>'
    )


def rich(text: str | None) -> str:
    """Escape text, then show `backticked` spans (as in backend messages) as inline code."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(text))


def banner(indicator: Indicator, title: str, body: str = "") -> str:
    return (
        f'<div class="banner" style="--c:{indicator.color}"><div class="banner-title">{pill(indicator)}'
        f'<span>{esc(title)}</span></div>{f"<div class=banner-body>{rich(body)}</div>" if body else ""}</div>'
    )


def decision_summary(decision: str | None, reason: str | None, deciding: list[str]) -> str:
    """Banner text: the backend's reason when approved, otherwise which checks decided it."""
    if decision == "APPROVED" or not deciding:
        return reason or ""
    names = ", ".join(humanize(name) for name in deciding)
    return f"Determined by {len(deciding)} check{'s' if len(deciding) > 1 else ''}: {names}."


# --- formatting --------------------------------------------------------------------------------


_ACRONYMS = {"po": "PO", "pdf": "PDF", "ocr": "OCR", "id": "ID"}


def humanize(identifier: str | None) -> str:
    """PO_MATCHING -> PO matching; amount_within_tolerance -> Amount within tolerance."""
    if not identifier:
        return "—"
    words = [_ACRONYMS.get(w, w) for w in identifier.replace("_", " ").lower().split()]
    if words and words[0] == words[0].lower():
        words[0] = words[0][:1].upper() + words[0][1:]
    return " ".join(words)


def money(amount, currency: str | None = None) -> str:
    """'118000.00', 'INR' -> 'INR 118,000.00' (the same grouping the backend uses in its reasons)."""
    if amount in (None, ""):
        return "—"
    try:
        value = Decimal(str(amount))
    except InvalidOperation:
        return str(amount)
    return f"{currency + ' ' if currency else ''}{value:,.2f}"


def duration(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1:
        return "< 1 ms"
    if ms < 1000:
        return f"{ms:,.1f} ms"
    return f"{ms / 1000:,.2f} s"


def timestamp(value: str | None, with_ms: bool = False) -> str:
    """Backend timestamps are UTC without an offset."""
    if not value:
        return "—"
    moment = datetime.fromisoformat(value)
    text = moment.strftime("%d %b %Y, %H:%M:%S")
    if with_ms:
        text += f".{moment.microsecond // 1000:03d}"
    return f"{text} UTC"


def filesize(size: int | None) -> str:
    if size is None:
        return "—"
    if size < 1024 * 1024:
        return f"{size / 1024:,.1f} KB"
    return f"{size / (1024 * 1024):,.1f} MB"


def short_id(run_id: str | None) -> str:
    return f"{run_id[:8]}…" if run_id else "—"


def inject_css() -> None:
    st.html(
        """
        <style>
          .block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1320px; }
          h1 { font-weight: 650; letter-spacing: -0.01em; }
          .subtle { opacity: .72; font-size: .92rem; }
          .pill { display: inline-flex; align-items: center; gap: .4rem; padding: .12rem .65rem .12rem .2rem;
                  border-radius: 999px; font-size: .85rem; font-weight: 600; line-height: 1.5; white-space: nowrap;
                  background: color-mix(in srgb, var(--c) 13%, transparent);
                  border: 1px solid color-mix(in srgb, var(--c) 45%, transparent); }
          .pill-icon { display: inline-flex; align-items: center; justify-content: center; flex: none;
                       width: 1.15rem; height: 1.15rem; border-radius: 50%; font-size: .72rem; font-weight: 800;
                       background: var(--c); color: var(--g); }
          .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: .8rem;
                      align-items: stretch; margin: .3rem 0 .6rem; }
          .kpi { border: 1px solid rgba(127,127,127,.25); border-radius: 12px; padding: 1rem 1.1rem .9rem;
                 background: rgba(127,127,127,.04); display: flex; flex-direction: column; }
          .kpi-label { display: flex; align-items: center; gap: .45rem; font-size: .76rem; font-weight: 650;
                       text-transform: uppercase; letter-spacing: .04em; opacity: .78; line-height: 1.3; }
          .kpi-value { font-size: 2rem; font-weight: 650; margin-top: .3rem; line-height: 1.15; }
          .kpi-caption { font-size: .8rem; opacity: .68; margin-top: auto; padding-top: .2rem; }
          .banner { border: 1px solid color-mix(in srgb, var(--c) 40%, transparent); border-left: 5px solid var(--c);
                    border-radius: 10px; padding: .9rem 1.1rem; margin: .4rem 0 1rem;
                    background: color-mix(in srgb, var(--c) 7%, transparent); }
          .banner-title { display: flex; align-items: center; gap: .7rem; font-weight: 600; font-size: 1.05rem; }
          .banner-body { margin-top: .5rem; font-size: .93rem; line-height: 1.5; }
          .meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: .75rem 1.5rem;
                       margin: .6rem 0 1rem; }
          .meta-label { font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; opacity: .65; }
          .meta-value { font-size: .95rem; font-weight: 550; overflow-wrap: anywhere; }
          .note { border: 1px dashed rgba(127,127,127,.45); border-radius: 10px; padding: .7rem .95rem;
                  font-size: .88rem; opacity: .9; margin: .3rem 0 .9rem; }
        </style>
        """
    )


def meta_grid(items: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<div><div class="meta-label">{esc(label)}</div><div class="meta-value">{value}</div></div>'
        for label, value in items
    )
    return f'<div class="meta-grid">{cells}</div>'
