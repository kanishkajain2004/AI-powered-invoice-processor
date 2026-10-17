"""Cross-page navigation. The app registers its pages here at start-up."""

import streamlit as st

PAGES: dict[str, "st.navigation.page.StreamlitPage"] = {}
SELECTED_RUN = "selected_run_id"


def open_run(run_id: str) -> None:
    """Show `run_id` on the Run details page."""
    st.session_state[SELECTED_RUN] = run_id
    st.switch_page(PAGES["run"])
