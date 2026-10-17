"""Dashboard: live counts from /runs/stats and the run history from /runs."""

import pandas as pd
import streamlit as st

from frontend import nav, ui
from frontend.api_client import ApiClient, BackendError

STATUS_FILTERS = {"All statuses": None, "Completed": "COMPLETED", "Processing failed": "FAILED",
                  "Processing": "PROCESSING", "Received": "RECEIVED"}
DECISION_FILTERS = {"All decisions": None, "Approved": "APPROVED", "Needs review": "NEEDS_REVIEW",
                    "Rejected": "REJECTED"}
PAGE_SIZES = [10, 25, 50, 100]
PAGE_KEY = "history_page"


def _share(part: int, whole: int) -> str:
    return f"{part / whole:.0%} of completed" if whole else "no completed runs"


def _tiles(stats: dict) -> None:
    completed = stats["completed"]
    tiles = [
        ui.kpi("Total runs", f"{stats['total_runs']:,}",
               f"last run {ui.timestamp(stats['last_run_at'])}" if stats["last_run_at"] else "no runs yet"),
        ui.kpi("Approved", f"{stats['approved']:,}", _share(stats["approved"], completed), ui.DECISIONS["APPROVED"]),
        ui.kpi("Needs review", f"{stats['needs_review']:,}", _share(stats["needs_review"], completed),
               ui.DECISIONS["NEEDS_REVIEW"]),
        ui.kpi("Rejected", f"{stats['rejected']:,}", _share(stats["rejected"], completed), ui.DECISIONS["REJECTED"]),
        ui.kpi("Processing failed", f"{stats['failed']:,}", "no decision recorded", ui.RUN_STATUSES["FAILED"]),
        ui.kpi("Avg processing time", ui.duration(stats["average_duration_ms"]), "finished runs, end to end"),
    ]
    ui.show(ui.kpi_row(tiles))
    if stats["in_progress"]:
        st.caption(f"{stats['in_progress']} run(s) are still in progress and are not counted above.")


def _reset_page() -> None:
    st.session_state[PAGE_KEY] = 1


def _history(client: ApiClient) -> None:
    st.subheader("Run history")
    f1, f2, f3, f4 = st.columns([2, 2, 1, 1])
    status = STATUS_FILTERS[f1.selectbox("Processing status", list(STATUS_FILTERS), on_change=_reset_page)]
    decision = DECISION_FILTERS[f2.selectbox("Decision", list(DECISION_FILTERS), on_change=_reset_page)]
    page_size = f3.selectbox("Rows per page", PAGE_SIZES, index=1, on_change=_reset_page)
    page = f4.number_input("Page", min_value=1, step=1, key=PAGE_KEY)

    data = client.list_runs(status=status, decision=decision, page=int(page), page_size=page_size)
    items, total = data["items"], data["total"]
    if not items:
        st.info("No runs match these filters." if total or status or decision else
                "No runs recorded yet. Process an invoice to see it here.")
        if total and page > 1:
            st.caption(f"Page {page} is past the end ({total} matching runs).")
        return

    frame = pd.DataFrame([{
        "Started (UTC)": ui.timestamp(r["created_at"]).removesuffix(" UTC"),
        "Outcome": ui.outcome(r).text,
        "Vendor": r["vendor_name"] or "—",
        "Invoice no.": r["invoice_number"] or "—",
        "Total": ui.money(r["total_amount"], r["currency"]),
        "File": r["file_name"],
        "Duration": ui.duration(r["duration_ms"]),
        "Run": ui.short_id(r["run_id"]),
    } for r in items])
    event = st.dataframe(frame, hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key="history_table",
                         column_config={
                             "Outcome": st.column_config.TextColumn(width="medium"),
                             "Total": st.column_config.TextColumn(help="Invoice total as extracted"),
                         })
    first = (data["page"] - 1) * data["page_size"] + 1
    st.caption(f"Showing {first}–{first + len(items) - 1} of {total} run(s), newest first. "
               "Select a row to inspect it.")

    rows = event.selection.rows if event and event.selection else []
    if rows:
        run = items[rows[0]]
        if st.button(f"Inspect run {ui.short_id(run['run_id'])} · {run['file_name']} →", type="primary"):
            nav.open_run(run["run_id"])


def render(client: ApiClient) -> None:
    head, action = st.columns([5, 1])
    head.title("Dashboard")
    action.write("")
    action.button("Refresh", width="stretch")  # any interaction re-reads the backend
    ui.show('<p class="subtle">Live figures from the backend database. Nothing on this page is cached or estimated.</p>')
    try:
        _tiles(client.stats())
        st.write("")
        _history(client)
    except BackendError as exc:
        st.error(f"**Could not load dashboard data.** {exc.message}")
