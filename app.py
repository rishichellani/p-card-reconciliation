"""P-Card reconciliation app.  Start with:  streamlit run app.py   (see README for cloud deployment)"""
from __future__ import annotations

import hmac
import os
from pathlib import Path

import streamlit as st

from ui.theme import inject
from utils.env import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / ".env")

# Streamlit Cloud keeps secrets in st.secrets; expose the ones the pipeline reads as environment variables.
try:
    for _key in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "LLM_PROVIDERS", "LLM_MIN_INTERVAL_SECONDS",
                 "GEMINI_MODEL", "GROQ_MODEL", "OPENROUTER_MODEL", "APP_PASSCODE", "LIVE_LLM_PASSCODE"):
        if _key in st.secrets:
            os.environ.setdefault(_key, str(st.secrets[_key]))
except Exception:  # no secrets file locally: that is fine
    pass

st.set_page_config(page_title="P-Card Reconciliation", page_icon=":material/receipt_long:", layout="wide")
inject()


def gate() -> None:
    """Optional shared passcode (set APP_PASSCODE). Strongly recommended for any URL that other people can reach."""
    passcode = os.environ.get("APP_PASSCODE", "")
    if not passcode or st.session_state.get("authed"):
        return
    st.title("P-Card & Expense Reconciliation")
    with st.form("gate"):
        entered = st.text_input("Passcode", type="password")
        if st.form_submit_button("Enter", type="primary"):
            if hmac.compare_digest(entered.encode(), passcode.encode()):
                st.session_state["authed"] = True
                st.rerun()
            st.error("That passcode is not correct.")
    st.stop()


gate()
st.navigation([
    st.Page("ui/workflow.py", title="Live workflow", icon=":material/edit_note:", default=True),
    st.Page("ui/results.py", title="Results", icon=":material/fact_check:"),
]).run()
