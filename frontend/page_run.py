"""Run details: everything the backend recorded for one run, selectable from history."""

import json

import pandas as pd
import streamlit as st

from frontend import nav, ui
from frontend.api_client import ApiClient, BackendError

RECENT_RUNS = 100


def _summarize(output: dict | None, limit: int = 140) -> str:
    if not output:
        return ""
    parts = []
    for key, value in output.items():
        if isinstance(value, list):
            value = ", ".join(map(str, value)) if value and len(value) <= 4 else f"{len(value)} item(s)"
        elif isinstance(value, dict):
            value = ", ".join(f"{k}={v}" for k, v in value.items())
        parts.append(f"{key}: {value}")
    text = " · ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"


PICKER, PASTE = "run_picker", "run_paste"


def _on_pick() -> None:
    st.session_state[PASTE] = ""
    st.session_state[nav.SELECTED_RUN] = st.session_state[PICKER]


def _on_paste() -> None:
    if value := st.session_state[PASTE].strip().lower():
        st.session_state[nav.SELECTED_RUN] = value


def _pick_run(client: ApiClient) -> str | None:
    """The picker and the paste box always agree: either one sets the selected run."""
    recent = client.list_runs(page_size=RECENT_RUNS)["items"]
    by_id = {r["run_id"]: r for r in recent}
    options = list(by_id)
    selected = st.session_state.get(nav.SELECTED_RUN) or st.query_params.get("run") or (options[0] if options else None)
    if selected is None:
        st.info("No runs recorded yet. Process an invoice first.")
        return None
    if selected not in by_id:
        options.insert(0, selected)  # older than the recent list, or pasted

    def label(run_id: str) -> str:
        run = by_id.get(run_id)
        if run is None:
            return f"{run_id} (not in the latest {RECENT_RUNS})"
        return f"{ui.timestamp(run['created_at'])} · {run['file_name']} · {ui.outcome(run).text} · {ui.short_id(run_id)}"

    st.session_state[PICKER] = selected
    left, right = st.columns([3, 2])
    left.selectbox("Run", options, key=PICKER, on_change=_on_pick, format_func=label,
                   help=f"The {RECENT_RUNS} most recent runs, newest first.")
    right.text_input("…or paste a run ID", key=PASTE, on_change=_on_paste, placeholder="32-character run ID")
    st.session_state[nav.SELECTED_RUN] = selected
    st.query_params["run"] = selected
    return selected


def _header(run: dict) -> None:
    indicator = ui.outcome(run)
    ui.show(ui.meta_grid([
        ("Outcome", ui.pill(indicator)),
        ("Processing status", ui.pill(ui.RUN_STATUSES.get(run["status"], ui.UNKNOWN))),
        ("Run ID", f"<code>{ui.esc(run['run_id'])}</code>"),
        ("File", ui.esc(run["file_name"])),
        ("Started", ui.esc(ui.timestamp(run["created_at"]))),
        ("Finished", ui.esc(ui.timestamp(run["completed_at"]))),
        ("End-to-end duration", ui.esc(ui.duration(run["duration_ms"]))),
        ("File size", ui.esc(ui.filesize(run.get("file_size")))),
    ]))

    if run["status"] == "FAILED":
        error = run.get("error") or {}
        ui.show(ui.banner(indicator, f"Processing failed at {ui.humanize(error.get('stage'))}",
                          f"{error.get('message') or ''} (code: `{error.get('code')}`)"))
        st.caption("A processing failure is not a business decision: this run has no decision, "
                   "and it is ignored by duplicate detection.")
    elif run.get("decision"):
        ui.show(ui.banner(indicator, "Decision", ui.decision_summary(
            run["decision"], run.get("decision_reason"), run.get("deciding_checks") or [])))
        for name in run.get("deciding_checks") or []:
            check = next(c for c in run["checks"] if c["name"] == name)
            ui.show(f'<div style="margin:.3rem 0">{ui.pill(ui.CHECKS[check["status"]])} '
                    f'<b>{ui.esc(ui.humanize(name))}</b> — {ui.rich(check["reason"])}</div>')


def _timeline(run: dict) -> None:
    ui.show('<div class="note"><b>Recorded timeline.</b> The backend processes an upload synchronously, in one '
            "request. These are the start times and durations it <i>recorded</i> for each stage as it ran. "
            "This is an audit record, not a live progress view. Stages that never ran are not listed.</div>")
    stages = run["stages"]
    total = sum(s["duration_ms"] or 0 for s in stages) or None
    frame = pd.DataFrame([{
        "#": s["sequence"],
        "Stage": ui.humanize(s["stage"]),
        "Status": ui.STAGES.get(s["status"], ui.UNKNOWN).text,
        "Started": ui.timestamp(s["started_at"], with_ms=True),
        "Duration (ms)": round(s["duration_ms"], 3) if s["duration_ms"] is not None else None,
        "Share of run": (s["duration_ms"] or 0) / total if total else 0.0,
        "Recorded output": _summarize(s["output"]) or (s["error_message"] or ""),
    } for s in stages])
    st.dataframe(
        frame, hide_index=True, width="stretch",
        column_config={
            "#": st.column_config.NumberColumn(width="small"),
            "Duration (ms)": st.column_config.NumberColumn(format="%.3f"),
            "Share of run": st.column_config.ProgressColumn(min_value=0.0, max_value=1.0, format="percent",
                                                            help="Share of the summed stage durations"),
            "Recorded output": st.column_config.TextColumn(width="large"),
        },
    )
    with st.expander("Stage outputs (as recorded)"):
        for s in stages:
            st.markdown(f"**{s['sequence']}. {ui.humanize(s['stage'])}** — {ui.STAGES.get(s['status'], ui.UNKNOWN).text}")
            st.json(s["output"] or {}, expanded=False)


def _invoice(run: dict) -> None:
    invoice = run.get("invoice")
    if invoice is None:
        st.info("No invoice fields: processing stopped before normalization.")
    else:
        currency = invoice.get("currency")
        left, right = st.columns([3, 2])
        with left:
            st.markdown("**Extracted fields** (normalized)")
            st.dataframe(pd.DataFrame([
                {"Field": "Vendor", "Value": invoice.get("vendor_name") or "—"},
                {"Field": "Invoice number", "Value": invoice.get("invoice_number") or "—"},
                {"Field": "Invoice date", "Value": invoice.get("invoice_date") or "—"},
                {"Field": "PO number", "Value": invoice.get("po_number") or "—"},
                {"Field": "Currency", "Value": currency or "—"},
                {"Field": "Subtotal", "Value": ui.money(invoice.get("subtotal"), currency)},
                {"Field": "Tax", "Value": ui.money(invoice.get("tax_amount"), currency)},
                {"Field": "Total", "Value": ui.money(invoice.get("total_amount"), currency)},
            ]), hide_index=True, width="stretch")
        with right:
            st.markdown("**Matched purchase order**")
            po = run.get("purchase_order")
            po_check = next((c for c in run["checks"] if c["name"] == "po_match"), None)
            if po:
                st.dataframe(pd.DataFrame([
                    {"Field": "PO number", "Value": po["po_number"]},
                    {"Field": "Vendor", "Value": po["vendor_name"]},
                    {"Field": "Approved amount", "Value": ui.money(po["total_amount"], po["currency"])},
                    {"Field": "PO status", "Value": ui.humanize(po["status"])},
                ]), hide_index=True, width="stretch")
            else:
                ui.show(ui.pill(ui.CHECKS["FAIL"]) + " &nbsp;No unique PO matched")
            if po_check:
                st.caption(po_check["reason"])
            extraction = run.get("extraction") or {}
            st.markdown("**Extraction**")
            st.caption(f"Method: {extraction.get('method') or '—'} · pages: {extraction.get('page_count') or '—'}"
                       + (f" · OCR pages: {extraction['ocr_pages']}" if extraction.get("ocr_pages") else ""))

        st.markdown("**Line items**")
        if invoice["line_items"]:
            st.dataframe(pd.DataFrame([{"Description": li["description"], "Quantity": li.get("quantity"),
                                        "Unit price": ui.money(li.get("unit_price"), currency),
                                        "Amount": ui.money(li.get("amount"), currency)}
                                       for li in invoice["line_items"]]), hide_index=True, width="stretch")
        else:
            st.caption("No line items were found in the document.")

    extraction = run.get("extraction") or {}
    if extraction.get("raw_fields"):
        with st.expander("Source text each field was read from"):
            st.dataframe(pd.DataFrame([{"Field": ui.humanize(k), "Source text": v}
                                       for k, v in extraction["raw_fields"].items()]),
                         hide_index=True, width="stretch")
    warnings = extraction.get("warnings", []) + run.get("normalization_warnings", [])
    if warnings:
        st.markdown("**Warnings**")
        for warning in warnings:
            st.warning(warning)


def _validation(run: dict) -> None:
    checks = run["checks"]
    if not checks:
        st.info("No validation checks were recorded (processing did not reach persistence).")
        return
    deciding = set(run.get("deciding_checks") or [])
    counts = {status: sum(c["status"] == status for c in checks) for status in ui.CHECKS}
    ui.show(" ".join(f"{ui.pill(ui.CHECKS[s])} <b>{n}</b>&nbsp;&nbsp;" for s, n in counts.items()))
    st.write("")
    st.dataframe(
        pd.DataFrame([{
            "Check": ui.humanize(c["name"]),
            "Result": ui.CHECKS.get(c["status"], ui.UNKNOWN).text,
            "Mandatory": "Yes" if c["mandatory"] else "No",
            "Forces": ui.DECISIONS[c["effect"]].text if c["effect"] != "APPROVED" else "—",
            "Deciding": "★" if c["name"] in deciding else "",
            "Reason": c["reason"],
            "Category": ui.humanize(c["category"]),
        } for c in checks]),
        hide_index=True, width="stretch",
        column_config={
            "Forces": st.column_config.TextColumn(width="medium", help="The decision this check alone would force"),
            "Deciding": st.column_config.TextColumn(width="small", help="One of the checks that set the decision"),
            "Reason": st.column_config.TextColumn(width="large"),
        },
    )
    st.markdown("**Evidence**")
    for c in checks:
        title = f"{ui.CHECKS.get(c['status'], ui.UNKNOWN).text} · {ui.humanize(c['name'])}"
        with st.expander(title, expanded=c["name"] in deciding):
            st.caption(c["reason"])
            st.json(c["evidence"] or {})


def _events(run: dict) -> None:
    st.dataframe(pd.DataFrame([{
        "Time": ui.timestamp(e["created_at"], with_ms=True),
        "Event": ui.humanize(e["event_type"]),
        "Stage": ui.humanize(e["stage"]),
        "Message": e["message"] or "",
    } for e in run["events"]]), hide_index=True, width="stretch",
        column_config={"Message": st.column_config.TextColumn(width="large")})


def render(client: ApiClient) -> None:
    st.title("Run details")
    run_id = _pick_run(client)
    if run_id is None:
        return
    try:
        run = client.get_run(run_id)
    except BackendError as exc:
        st.error(f"**Could not load run `{run_id}`.** {exc.message}")
        return

    _header(run)
    timeline, invoice, validation, events = st.tabs(
        ["Execution timeline", "Invoice & PO", f"Validation ({len(run['checks'])})", f"Audit events ({len(run['events'])})"]
    )
    with timeline:
        _timeline(run)
    with invoice:
        _invoice(run)
    with validation:
        _validation(run)
    with events:
        _events(run)
    with st.expander("Raw API response"):
        st.code(json.dumps(run, indent=2), language="json")
