"""Streamlit frontend. Run with: streamlit run frontend/streamlit_app.py

Talks to the FastAPI backend over HTTP only (frontend/api_client.py); it holds no
business rules and no data of its own.
"""

import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make the `frontend` package importable

from frontend import nav, page_dashboard, page_process, page_run, ui  # noqa: E402
from frontend.api_client import ApiClient, BackendError  # noqa: E402

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")

st.set_page_config(page_title="Invoice Operations", page_icon=":material/receipt_long:", layout="wide")
ui.inject_css()
client = ApiClient(API_BASE_URL)

nav.PAGES.update({
    "process": st.Page(lambda: page_process.render(client), title="Invoice processing",
                       icon=":material/upload_file:", url_path="process", default=True),
    "run": st.Page(lambda: page_run.render(client), title="Run details",
                   icon=":material/fact_check:", url_path="run"),
    "dashboard": st.Page(lambda: page_dashboard.render(client), title="Dashboard",
                         icon=":material/dashboard:", url_path="dashboard"),
})
current = st.navigation(list(nav.PAGES.values()))

with st.sidebar:
    st.markdown("### Invoice Operations")
    try:
        health = client.health()
        ui.show(f'{ui.pill(ui.Indicator("✓", "Backend connected", ui.GOOD))}'
                f'<div class="subtle" style="margin-top:.4rem">API v{ui.esc(health["version"])} · database '
                f'{ui.esc(health["database"])}</div>')
    except BackendError as exc:
        ui.show(ui.pill(ui.Indicator("✕", "Backend unavailable", ui.CRITICAL)))
        st.caption(exc.message)
        st.caption("If the API was idle it may still be starting (about a minute on a free cloud plan). "
                   "Refresh the page shortly.")
    st.caption(f"API: {client.base_url}")

current.run()
