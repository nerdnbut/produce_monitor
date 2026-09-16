"""Streamlit multipage entry for the standalone Production Status view."""
import sys
from pathlib import Path

import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from produce_monitor.production_status.status_page import render_status_page


st.set_page_config(
    page_title="Production Status",
    page_icon="🟢",
    layout="wide",
)

if hasattr(st, "fragment"):
    st.fragment(run_every="60s")(render_status_page)()
else:
    render_status_page()
