"""Invoice processing: upload a PDF, run it through the backend, show the decision."""

import pandas as pd
import streamlit as st

from frontend import nav, ui
from frontend.api_client import ApiClient, BackendError, BackendUnavailable

OUTCOME = "process_outcome"


def _process(client: ApiClient, uploaded) -> dict:
    """Call the backend once and keep what came back (so reruns do not reprocess)."""
    try:
        result = client.process(uploaded.name, uploaded.getvalue(), uploaded.type)
        return {"kind": "result", "file_id": uploaded.file_id, "result": result}
    except BackendUnavailable as exc:
        return {"kind": "unavailable", "file_id": uploaded.file_id, "error": exc}
    except BackendError as exc:
        kind = "failed_run" if exc.run else "rejected_upload"
        return {"kind": kind, "file_id": uploaded.file_id, "error": exc}


def _show_result(client: ApiClient, result: dict) -> None:
    indicator = ui.outcome(result)
    ui.show(ui.banner(indicator, "Decision", ui.decision_summary(
        result.get("decision"), result.get("decision_reason"), result.get("deciding_checks") or [])))

    invoice = result.get("invoice") or {}
    po = result.get("purchase_order")
    ui.show(ui.meta_grid([
        ("Run ID", f"<code>{ui.esc(result['run_id'])}</code>"),
        ("Vendor", ui.esc(invoice.get("vendor_name") or "—")),
        ("Invoice number", ui.esc(invoice.get("invoice_number") or "—")),
        ("Invoice date", ui.esc(invoice.get("invoice_date") or "—")),
        ("Total", ui.esc(ui.money(invoice.get("total_amount"), invoice.get("currency")))),
        ("Matched PO", ui.esc(po["po_number"] if po else "None")),
    ]))

    deciding = set(result.get("deciding_checks") or [])
    if deciding:
        st.markdown("**Why:** the checks that determined this decision")
        for check in result["checks"]:
            if check["name"] in deciding:
                ui.show(f'<div style="margin:.35rem 0">{ui.pill(ui.CHECKS[check["status"]])} '
                        f'<b>{ui.esc(ui.humanize(check["name"]))}</b> — {ui.rich(check["reason"])}</div>')

    with st.expander(f"All {len(result['checks'])} validation checks"):
        st.dataframe(
            pd.DataFrame([{"Check": ui.humanize(c["name"]), "Result": ui.CHECKS[c["status"]].text,
                           "Mandatory": "Yes" if c["mandatory"] else "No", "Reason": c["reason"]}
                          for c in result["checks"]]),
            hide_index=True, width="stretch",
        )

    _recorded_timeline(client, result["run_id"])
    if st.button("Open full run details →", key="open_result_run"):
        nav.open_run(result["run_id"])


def _recorded_timeline(client: ApiClient, run_id: str) -> None:
    try:
        detail = client.get_run(run_id)
    except BackendError as exc:
        st.caption(f"Could not load the recorded timeline: {exc.message}")
        return
    stages = detail["stages"]
    st.markdown(f"**Recorded execution** · {len(stages)} stages · {ui.duration(detail.get('duration_ms'))} end to end")
    st.caption("The backend ran these stages synchronously within the single request above. "
               "This list is what it recorded; it was not streamed live.")
    st.dataframe(
        pd.DataFrame([{"#": s["sequence"], "Stage": ui.humanize(s["stage"]),
                       "Status": ui.STAGES.get(s["status"], ui.UNKNOWN).text, "Duration": ui.duration(s["duration_ms"])}
                      for s in stages]),
        hide_index=True, width="stretch",
    )


def _show_error(outcome: dict) -> None:
    exc: BackendError = outcome["error"]
    if outcome["kind"] == "unavailable":
        st.error(f"**Backend unavailable.** {exc.message} Start it with `uvicorn app.main:app`.")
        return
    if outcome["kind"] == "rejected_upload":
        st.error(f"**Upload rejected — nothing was processed.** {exc.message}")
        st.caption(f"Error code `{exc.code}` · HTTP {exc.status_code}. No run was created; fix the file and try again.")
        return
    run = exc.run
    stage = (run.get("error") or {}).get("stage")
    ui.show(ui.banner(ui.RUN_STATUSES["FAILED"], f"Processing failed at {ui.humanize(stage)}", exc.message))
    st.caption(f"Error code `{exc.code}` · HTTP {exc.status_code} · run `{run['run_id']}`. "
               "This is a processing failure, not a business decision: no decision was recorded.")
    if st.button("Inspect the failed run →", key="open_failed_run"):
        nav.open_run(run["run_id"])


def render(client: ApiClient) -> None:
    st.title("Invoice processing")
    ui.show('<p class="subtle">Upload an invoice PDF. The backend extracts and normalizes it, checks for duplicates, '
            "matches the purchase order, validates it and records a decision.</p>")

    uploaded = st.file_uploader("Invoice PDF", type=["pdf"], accept_multiple_files=False,
                                help="Text PDFs are read directly; scanned PDFs need Tesseract OCR on the server.")
    outcome = st.session_state.get(OUTCOME)
    if outcome and (uploaded is None or outcome["file_id"] != uploaded.file_id):
        outcome = st.session_state[OUTCOME] = None  # a different (or no) file is selected now

    if uploaded is not None:
        ui.show(ui.meta_grid([("Selected file", ui.esc(uploaded.name)),
                              ("Size", ui.filesize(uploaded.size)),
                              ("Type", ui.esc(uploaded.type or "unknown"))]))

    if st.button("Process invoice", type="primary", disabled=uploaded is None):
        with st.spinner("Processing… the backend runs every stage before it responds."):
            outcome = st.session_state[OUTCOME] = _process(client, uploaded)

    if outcome:
        st.divider()
        if outcome["kind"] == "result":
            _show_result(client, outcome["result"])
        else:
            _show_error(outcome)
